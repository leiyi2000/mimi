---
status: implemented
owner: Mimi maintainers
updated: 2026-10-24
commits: []
---

# Pumpkin 安静参与设计

## 1. 问题

当前群聊会话一旦被显式唤醒，所有后续普通消息都会进入 Conversation Agent。模型回复后
又把会话 TTL 延长 15 分钟，同时模型倾向持续提交 `topic_state=active`。因此活跃群里会
形成自我维持循环：

```text
显式唤醒
  -> ACTIVE
  -> 任意群消息进入 Agent
  -> 模型回复
  -> 出站回复续期 15 分钟
  -> ACTIVE
```

最近一次样本中，50 条用户输入有 27 条不是直接对南瓜说的；23 条普通消息仍被回复。
46 个已完成聊天 Turn 全部选择 `topic_state=active`，没有一次结束话题。

这不是单纯的 Prompt 问题。Runtime 没有参与资格门禁，已存在的
`ParticipationPhase` 和 `cooling_message_count` 也没有参与入站决策。

## 2. 设计原则

1. Runtime 决定哪些消息有资格进入 Agent；模型决定如何处理已合格的消息。
2. 显式对南瓜说话必须可靠，不受安静策略影响。
3. 普通群聊默认只归档，不默认触发模型。
4. 南瓜自己的回复不能延长关注窗口，避免自激续期。
5. 模型的 `topic_state` 是语义信号，不是唯一的生命周期保护。
6. 安静策略使用明确状态和结构化原因，不散落文本启发式。

## 3. 消息参与类型

新增 `ParticipationIntent`：

| 类型 | 判断依据 | 行为 |
| --- | --- | --- |
| `DIRECT` | @南瓜、以南瓜开头、回复南瓜消息、私聊 | 必须入队 |
| `FOLLOW_UP` | 同一成员在直接 Turn 后短时间内继续当前话题 | 受注意力预算控制 |
| `EXPANSION` | 其他成员回复已接受的话题消息，或话题被模型显式标为 `group_open` | 与追问共享窗口和预算 |
| `AMBIENT` | 其他普通群消息 | 默认只归档 |
| `STOP` | 明确要求南瓜停止参与 | 立即休眠并确认一次 |

“回复南瓜消息”必须通过结构化 `reply_to_message_id` 与已发送消息 ID 判断，不做文本猜测。
“回复已接受的话题消息”同样只认结构化回复链；时间相邻或文本相似不能单独构成扩散。
停止意图由独立 `ConversationDirective.STOP` 解析，支持配置化别名；不能只识别一个硬编码
字符串。

## 4. 参与状态机

复用现有 `ParticipationPhase`：

```text
DIRECT
  -> ENGAGED (follow-up budget = 6)

ENGAGED + FOLLOW_UP
  -> 消耗 1 budget
  -> 刷新 5 分钟空闲窗口
  -> budget > 0: ENGAGED
  -> budget = 0: COOLING

ENGAGED + EXPANSION
  -> 仅结构化回复链或 group_open 可接受
  -> 消耗同一 budget 并刷新空闲窗口

COOLING + FOLLOW_UP/EXPANSION/AMBIENT
  -> 只归档，不入队
  -> QUIET

任意状态 + DIRECT
  -> ENGAGED，重置话题起点、空闲窗口和 budget

任意状态 + STOP/空闲超时/15 分钟硬上限/topic ended
  -> QUIET
```

默认参数：

- `direct_follow_up_window_seconds = 300`
- `topic_max_lifetime_seconds = 900`
- `follow_up_turn_budget = 6`
- `ambient_turn_budget = 0`
- `quiet_after_ignored_ambient = true`

预算以被接受的 Turn 为单位，不按消息数量或模型调用次数计算。

## 5. 生命周期所有权

### 5.1 TTL

`DIRECT` 写入 `activated_at` 作为话题硬起点，并把同一时间写入参与上下文中的
`last_activity_at`。被策略接受的 `FOLLOW_UP` 或 `EXPANSION` 只刷新
`last_activity_at`；决策同时校验 5 分钟空闲窗口和 15 分钟硬上限。旧会话上下文没有
`last_activity_at` 时回退到 `activated_at`，不需要数据库迁移。

以下动作不再续期：

- `send_napcat_reply` / `send_napcat_replies`；
- 失败回执；
- Task Event 通知；
- 普通 `AMBIENT` 消息。

因此南瓜不能通过自己的回复维持 ACTIVE，持续追问也不能让单次话题无限延长。

### 5.2 模型终止信号

`chat_turn_complete(topic_state=ended)` 仍立即结束话题。

`topic_state=active` 只表示模型认为语义话题尚未结束，不能增加 Runtime 注意力预算，也不能
推迟用户活动 TTL，也不能隐式开放给其他成员。模型只有在回复明确邀请群内其他成员参与时，
才通过 `topic_audience=group_open` 开放受众；该信号不补预算、不续期。这样保留 DeepSeek
Harness 的显式模型决策，但不把资源和参与边界完全交给模型。

## 6. 回复预算

按 `ParticipationIntent` 向 TurnPolicy 注入限制：

| 类型 | 最大回复条数 | 默认行为 |
| --- | --- | --- |
| `DIRECT` | 2 | 正常回答 |
| `FOLLOW_UP` | 1 | 只回答明确问题，不主动扩展 |
| `EXPANSION` | 1 | 回应当前扩散，不主动延伸新话题 |
| `AMBIENT` | 0 | 不创建 Turn |
| `STOP` | 1 | 固定确认后休眠 |

只有复杂且明确的直接请求允许 3 条回复。普通聊天不能默认连续发送 3 条。

Prompt 同步补充：

- 群聊中先判断是否真的在对南瓜说话；
- 没有被点名时，沉默优先于插话；
- 不抢答成员之间的对话；
- 仅在明确邀请群内其他成员继续时提交 `topic_audience=group_open`；
- 话题切换、明确拒绝或停止参与时提交 `ended`。

Prompt 只负责表达偏好，Runtime 继续执行硬边界。

## 7. 入站决策

`MessageIngress` 的目标流程：

```text
archive(message)
  -> classify intent
  -> load participation state
  -> ParticipationPolicy.decide(...)
       ACCEPT: enqueue + consume/reset budget
       ARCHIVE_ONLY: stop
       SLEEP: persist QUIET/SLEEPING + one receipt
```

`ParticipationDecision` 必须包含：

```text
action: ACCEPT | ARCHIVE_ONLY | SLEEP
intent: DIRECT | FOLLOW_UP | EXPANSION | AMBIENT | STOP
reason: explicit semantic enum
remaining_budget: int
```

决策原因写入结构化日志和 Inbox payload，便于审计，不记录群聊原文。

## 8. 数据模型

会话需要补充：

- `last_direct_sender_id`
- `last_activity_at`
- `follow_up_budget_remaining`
- `topic_audience`
- 有界的 `accepted_message_ids` 回复链

现有字段继续使用：

- `participation_phase`
- `cooling_message_count` 可迁移为预算字段，或在兼容迁移后移除。

当前实现将这些参与元数据编码在既有 session summary 中，并继续使用
`cooling_message_count` 记录共享预算，不需要数据库迁移。

## 9. 可观测性

新增结构化指标：

- `pumpkin_ingress_decision_total{intent,action,reason}`
- `pumpkin_chat_turn_total{intent,outcome,topic_state,topic_audience}`
- `pumpkin_reply_message_count{intent}`
- `pumpkin_attention_budget_remaining`

默认不得记录消息正文。排查时用 `scope_id`、`message_id`、`turn_id` 关联。

目标指标：

- 非直接群消息进入 Agent 的比例低于 10%；
- `AMBIENT` 回复率为 0；
- `FOLLOW_UP` 和 `EXPANSION` 每 Turn 最多一条回复；
- 显式 DIRECT 接受率为 100%。

## 10. 验收用例

1. @南瓜后同一成员最多 6 个自然追问 Turn 可进入 Agent，第 7 个只归档。
2. @南瓜后其他成员讨论无关话题，不进入 Agent。
3. 回复南瓜上一条消息视为 DIRECT；回复已接受的用户话题消息视为 EXPANSION。
4. 模型显式提交 `group_open` 后，其他成员可在剩余共享预算内加入。
5. 被接受的 FOLLOW_UP 或 EXPANSION 刷新 5 分钟空闲窗口，普通消息不刷新。
6. 话题持续活跃超过 15 分钟后仍停止自然接续，明确唤醒可重置。
7. 仅提交 `topic_state=active` 不开放话题、不补预算、不续期。
8. 南瓜连续发送回复不会延长 `expires_at`。
9. “退下南瓜”等 STOP 指令立即进入安静状态。
10. 安静后普通消息不唤醒；@南瓜可重新唤醒。
11. FOLLOW_UP 和 EXPANSION 最多发送一条，DIRECT 默认最多两条。
12. 私聊行为保持不变。
13. 决策日志包含 intent/action/reason，不包含消息正文。

## 11. 非目标

- 不引入第二个 LLM 作为入站分类器；
- 不对每条普通群消息做语义相似度请求；
- 不按群成员永久屏蔽；
- 不删除显式 `chat_turn_complete`；
- 不依赖 Prompt 单独保证安静。

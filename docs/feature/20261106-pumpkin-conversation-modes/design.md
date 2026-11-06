---
status: implemented
owner: Mimi maintainers
updated: 2026-11-06
commits: []
---

# Pumpkin 话题边界与对话模式设计

## 1. 背景

现有安静参与策略已经把群消息分成 `DIRECT`、`FOLLOW_UP`、`EXPANSION` 和
`AMBIENT`，并由 Runtime 在消息进入 Conversation Agent 前执行结构化门禁。但线上记录
仍暴露出三个边界问题：

1. 同一成员在直接 Turn 后发送的普通消息，仅凭发送者和时间窗口就会成为
   `FOLLOW_UP`，即使已经切换到与南瓜无关的话题。
2. Conversation Agent 创建后台任务后，任务确认结束了当前 Turn，却没有结束群聊话题；
   同一成员随后与其他群友聊天时仍可继承回复资格。
3. Conversation Session 固定保留最近四个已结束 Turn。新明确唤醒虽然重置参与窗口，
   模型上下文仍可能带入旧话题，导致普通聊天被旧的专业讨论带偏。

回复风格还存在独立问题：当前 Prompt 要求“能一句说清就不要展开”，但回复工具没有表达
本轮是在闲聊还是回答专业问题。模型容易把群友的普通陈述扩写成科普、建议或结构化分析。

这些问题不能通过缩短时间窗口、增加业务关键词或按参与类型限制字数解决。参与类型描述
“南瓜是否有资格说话”，回复模式描述“南瓜应该以什么方式说话”，两者必须分开。

## 2. 目标

1. 普通群聊默认一问一答；只有南瓜明确等待输入时才继续接受自然追问。
2. 任务创建成功后立即结束当前聊天延续，不让后台任务授权后续普通群聊。
3. 群聊模型上下文只保留当前话题，不让旧话题影响新明确唤醒。
4. Conversation Agent 明确区分闲聊与专业回答，并把选择写入结构化事件。
5. 保留专业问题的完整表达能力，不按 `DIRECT`、`FOLLOW_UP` 或 `EXPANSION` 设置字数上限。
6. 延续现有 Outbox、Task、Artifact、Session Event 和作用域隔离，不引入第二个分类模型。

## 3. 非目标

- 不使用问题关键词、标点、业务名词或具体群聊样本判断回复模式。
- 不对每条群消息增加一次额外模型请求或向量相似度请求。
- 不把所有普通陈述强制判为沉默；自然闲聊仍由 Conversation Agent 决定。
- 不自动截断模型回复，也不按参与类型设置字符数限制。
- 不修改私聊的连续对话体验。
- 不改变任务状态机、Artifact 验收或终态交付协议。

## 4. 两个正交维度

### 4.1 参与类型

`ParticipationIntent` 继续由 Runtime 根据消息结构和当前话题状态决定：

| 类型 | 含义 |
| --- | --- |
| `DIRECT` | 用户明确对南瓜说话 |
| `FOLLOW_UP` | 当前话题明确允许请求人继续补充 |
| `EXPANSION` | 当前话题明确允许群成员加入 |
| `AMBIENT` | 普通群聊，只归档 |
| `STOP` | 停止参与 |

参与类型只决定是否创建 Turn、可以回复几条及可以使用哪些 capability，不表达回复应长还是
短，也不直接表达回复风格。

### 4.2 回复模式

新增强类型 `ResponseMode`：

| 模式 | 使用条件 | 表达原则 |
| --- | --- | --- |
| `CASUAL_CHAT` | 问候、情绪、玩笑、普通陈述和日常交流 | 像群友聊天，可沉默，不主动扩写 |
| `PROFESSIONAL_ANSWER` | 用户明确要求分析、解释、建议、判断依据或专业资料 | 深度与问题匹配，可使用结构化表达 |
| `TASK_STATUS` | Conversation Agent 表达等待、失败、暂停等任务事件 | 只陈述必要状态和下一步 |

任务启动确认继续由 Runtime 生成并发送，终态 Artifact 继续由任务交付工具处理，不通过
`ResponseMode` 重定义。

`DIRECT` 不等于专业回答，`FOLLOW_UP` 也不等于闲聊。明确唤醒可以只是打招呼，连续追问也
可以要求专业解释。模式由 Conversation Agent 根据当前消息和当前话题语义选择。

## 5. 话题模型

### 5.1 TopicContext

在现有 `ParticipationContext` 中增加以下字段：

```text
topic_id: str
continuation: closed | requester_input | group_open
```

字段继续编码在 `ChatSessionRow.summary` 的 JSON 中，不新增数据库列：

- `topic_id` 使用不可推断的随机 ID，只用于关联 Session Event 和参与决策。
- `continuation` 表达当前 Turn 结束后谁还能自然继续。
- 旧 summary 缺少字段时按 `closed` 处理；下一次 `DIRECT` 创建新的 `topic_id`。

现有 `audience` 在兼容读取后由 `continuation` 取代：

| 旧状态 | 新状态 |
| --- | --- |
| topic ended | `closed` |
| active + requester only | `requester_input` |
| active + group open | `group_open` |

### 5.2 新话题

群聊中的每个 `DIRECT` 创建新的 `topic_id`。结构化回复仍携带引用消息，当前消息本身应提供
继续所需的显式关系；新话题不再隐式继承最近四个无关 Turn。

私聊不创建话题隔离边界，继续使用现有最近 Turn 窗口。

### 5.3 默认关闭

直接 Turn 被接受时，可以在该 Turn 执行期间接收同一请求人的快速补充；Turn 结束后默认把
`continuation` 写为 `closed`。只有 Conversation Agent 的完成结果明确声明以下状态时才
继续开放：

- `requester_input`：南瓜明确向当前请求人提出需要回答的问题或请求必要补充。
- `group_open`：南瓜在当前可见回复中明确邀请群成员参与。

普通回答、普通闲聊、沉默、拒绝和失败默认关闭。关闭时不发送告别消息。

### 5.4 任务创建

`create_task` 成功持久化任务后，Runtime 必须幂等地关闭当前 `topic_id`：

```text
create task
  -> persist task
  -> submit acknowledgement
  -> close conversation topic
  -> register and schedule task
```

只要任务已经创建，确认消息发送失败或结果不确定也不能继续开放普通聊天话题。任务后续输入
只通过以下结构化路径进入：

- 回复任务源消息；
- 回复任务确认或任务通知；
- 明确唤醒并询问任务；
- Task Event 要求用户补充。

后台任务继续运行和发送必要终态，不因聊天话题关闭而取消。

### 5.5 排队消息

每个用户消息 Inbox item 携带入队时的 `topic_id`。领取 `FOLLOW_UP` 或 `EXPANSION`
消息时再次校验：

1. `topic_id` 仍是当前话题；
2. 当前 `continuation` 仍允许该参与者；
3. 话题没有被任务创建、STOP、超时或前一个 Turn 关闭。

校验失败的 Inbox item 标记为 discarded，不执行模型请求。这样可以处理消息已入队、前一
Turn 随后关闭话题的竞态。

## 6. 完成协议

用单一强类型字段取代容易组合出模糊状态的 `topic_state + topic_audience`：

```text
ConversationContinuation:
  closed
  requester_input
  group_open
```

`chat_turn_complete` 接收：

```text
outcome: replied | ignored | failed
sent_message_ids: [...]
continuation: closed | requester_input | group_open
```

Runtime 执行以下校验：

- `ignored` 和 `failed` 只能使用 `closed`。
- 没有成功发送可见回复时不能开放 `requester_input` 或 `group_open`。
- `requester_input` 只允许当前直接请求人自然继续。
- `group_open` 继续受共享预算、空闲窗口和话题硬上限约束。
- Task 创建 Turn 不依赖模型完成调用，由 Runtime 强制关闭。

该协议不根据回复文字、问号或关键词推断延续状态。

## 7. 回复工具契约

`send_napcat_reply` 和 `send_napcat_replies` 增加必填 `response_mode`，schema 只接受
`ResponseMode` 枚举值。Handler 同时校验 Inbox 来源：

| Inbox 来源 | 允许模式 |
| --- | --- |
| `user.message` | `casual_chat`、`professional_answer` |
| `task.event` | `task_status` |

`response_mode` 写入 Tool Event 和 Outbox payload，发送文本和媒体行为保持不变。普通聊天
超过渲染阈值时仍可按现有机制转换为文字图片；本设计不把渲染阈值当作人设长度规则。

Prompt 和工具描述共同表达以下语义：

- 群友陈述不等于向南瓜提问。
- `CASUAL_CHAT` 可以沉默或自然回应，不主动切换成科普、风险提示、投资建议或清单。
- `PROFESSIONAL_ANSWER` 只在用户明确需要专业内容时使用。
- 专业回答的长度取决于问题复杂度和用户要求，不由参与类型决定。
- 不确定是否需要专业展开时，默认按闲聊处理。

Runtime 不尝试用文本启发式验证模式是否选择正确；模式质量通过黑盒评测和运行指标审计。

## 8. 上下文投影

### 8.1 Event 元数据

群聊用户消息对应的 `TURN_STARTED` 和 `USER_MESSAGE` Event 增加顶层元数据：

```text
topic_id
participation_intent
```

元数据不包含额外消息正文。Task Event 保持现有 task、revision 和 source 元数据。

### 8.2 Projection 规则

`SessionProjection` 仍从完整追加式 Event 重建，但模型可见窗口按来源收敛：

- 当前是群聊 `user.message`：只保留当前 `topic_id` 的已结束 Turn 和当前 Turn，最多四个。
- 当前是 `task.event`：使用当前 Task Event 的结构化上下文，不引入无关聊天话题。
- 当前是私聊：保持最近四个已结束 Turn 加当前 Turn。
- 没有 `topic_id` 的旧群聊 Turn 不进入新话题窗口。

完整 Event 不删除，恢复和审计能力不变。

### 8.3 动态上下文

Ingress 继续提供当前消息、参与原因、任务关联和能力目录，同时加入：

```text
topic_id
continuation
```

普通非任务消息继续不注入活跃任务索引。创建任务后的普通群聊也不能通过 Session 历史重新
获得任务目标或任务工具结果。

## 9. Capability 边界

本设计不通过回复模式动态增删工具。Capability 仍由 Inbox 来源和结构化上下文决定：

- `DIRECT` 用户消息保留现有聊天和任务能力。
- 已开放的 `FOLLOW_UP` 可以继续聊天，也可以在用户明确提出新执行请求时创建任务。
- `EXPANSION` 保持现有单条回复预算和 scope 校验。
- `AMBIENT` 不创建 Turn。
- 地址为空、Task Event、终态交付等既有特殊 loadout 保持不变。

话题关闭解决“旧请求持续授权普通群聊”，回复模式解决“已授权 Turn 应如何表达”；不把两者
合并成新的关键词路由。

## 10. 可观测性

在现有结构化事件和日志中增加：

- `topic_id`
- `continuation`
- `response_mode`
- `participation_intent`
- `outcome`
- `reply_character_count`

默认不新增群聊原文、完整 Prompt、模型思考或工具敏感参数。建议从持久化事件计算：

- `CASUAL_CHAT` 中被评为不必要专业展开的比例；
- 话题关闭后非直接消息进入 Agent 的比例；
- Task 创建后普通消息进入 Agent 的比例；
- `requester_input` 和 `group_open` 的实际后续参与率；
- 被要求停止参与前的无关回复密度。

字符数只用于观察异常分布，不作为参与类型的硬限制。

## 11. 兼容与失败行为

1. 不新增数据库表或列；旧 Session summary 按关闭状态读取。
2. 旧 Event 保持可审计，但不会跨入新的 `topic_id` 模型窗口。
3. Topic 关闭失败属于 Runtime 状态错误，任务仍保留；错误必须记录且不能静默继续开放。
4. 重复任务创建、恢复和 Outbox 重放必须复用同一个幂等关闭结果。
5. 私聊、任务执行、Artifact 验收、任务终态通知和媒体交付保持现状。
6. 部署时不需要迁移运行数据；进程重启后首次明确唤醒建立新话题。

## 12. 测试计划

### 12.1 参与状态

1. 直接消息完成普通回答后，后续同成员普通消息只归档。
2. 完成结果为 `requester_input` 时，同成员下一条消息成为 `FOLLOW_UP`。
3. 完成结果为 `group_open` 时，其他成员可在共享预算内加入。
4. `ignored`、`failed` 或无可见回复不能开放延续。
5. 新 `DIRECT` 创建新的 `topic_id`，旧话题排队消息被 discarded。
6. STOP、空闲超时和硬上限继续关闭话题。

### 12.2 任务边界

1. `create_task` 成功后立即关闭当前话题。
2. 任务确认发送失败或结果不确定时仍关闭话题。
3. 任务源消息、确认消息和 Task Event 的结构化回复仍可进入正确任务上下文。
4. 后台任务关闭聊天话题后继续执行并正常发送终态。

### 12.3 回复模式

1. 普通问候、玩笑和日常陈述使用 `CASUAL_CHAT`。
2. 明确分析、解释和专业建议请求使用 `PROFESSIONAL_ANSWER`。
3. Task Event 只允许 `TASK_STATUS`。
4. 非法枚举或来源不匹配返回明确工具错误。
5. 闲聊模式允许自然长短变化，不按参与类型校验字符数。

### 12.4 上下文

1. 新群聊话题不包含前一话题的模型消息和工具结果。
2. 同一 `topic_id` 最多保留最近四个已结束 Turn。
3. 私聊继续保留原最近四 Turn 行为。
4. 旧 Event 和缺少 `topic_id` 的 Session 可兼容恢复。

### 12.5 黑盒评测

使用合成身份和 Fake NapCat 覆盖：

- 创建专业任务后，发起人转回与群友的普通聊天；
- 日常闲聊中出现与旧专业话题相似的词；
- 南瓜明确追问必要信息后，用户补充；
- 新明确唤醒切换到不同话题；
- 普通陈述保持沉默或自然短聊；
- 明确专业问题得到与复杂度匹配的完整回答。

评审只依据群内可见记录判断相关性、人设、简洁度和是否出现未经请求的专业展开。

## 13. 实施顺序

1. 先增加领域枚举、TopicContext 兼容解析和完成协议测试。
2. 实现任务创建后的幂等话题关闭及排队消息二次校验。
3. 为 Event 和 Projection 增加 `topic_id` 元数据与过滤。
4. 为回复工具增加 `response_mode` 并更新 Prompt。
5. 补 Runtime 集成测试和黑盒评测场景。
6. 实现完成后同步更新 `ARCHITECTURE.md`、Pumpkin README 和本特性状态。
7. 执行 Pumpkin 全量测试及根目录 `git diff --check`。

## 14. 验收标准

1. 后台任务创建后，同成员的非直接普通群聊不会进入 Conversation Agent。
2. 普通回答结束后不会仅因同发送者和时间相邻继续接话。
3. 只有明确等待输入或邀请群成员时才开放后续参与。
4. 新话题的模型上下文不包含旧话题 Turn。
5. 每条普通文本回复具有合法且来源匹配的 `ResponseMode`。
6. 闲聊和专业回答没有按参与类型设置字符数限制。
7. 私聊、Task Event、任务交付和现有媒体能力无回归。
8. 所有新增运行记录不包含额外群聊原文或敏感信息。

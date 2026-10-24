---
status: implemented
owner: Mimi maintainers
updated: 2026-10-23
commits: []
---

# Pumpkin Turn 失败恢复设计

## 1. 背景

Conversation Agent 已观察到两类失败：

1. 本轮已经成功发送回复，后续模型请求超时，Turn 被正确记录为失败，但可靠性层又发送
   “处理失败”回执，形成相互矛盾的用户体验。
2. 模型调用 `chat_turn_complete` 时混入上一轮消息 ID，工具返回
   `SEND_NOT_CONFIRMED`；`AgentLoop` 没有把该工具结果交给下一 Step，而是立即结束为
   `turn.failed`。

第二类行为违背 DeepSeek Harness 的工具循环：工具结果必须先进入追加式 Session Event，
下一 Step 从日志投影中看到结果并决定修正、替代或停止。第一类行为的问题不在
`turn.failed` 本身，而在用户回执策略忽略了本轮已经确认送达的事实。

本设计基于：

- [DeepSeek Harness architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)
- [DeepSeek Harness Agent Loop](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent-loop/README.md)
- [Pumpkin 完全对齐 DeepSeek Harness](../20261023-pumpkin-harness-alignment/design.md)

## 2. 必须保留的不变量

1. `chat_turn_complete` 仍由模型显式调用；普通文本和发送工具不能代替终止工具。
2. `sent_message_ids` 仍由模型根据上一 Step 的结构化 ToolResult 提交。
3. Runtime 只校验消息是否确由当前 Turn 发送，不替模型决定 `outcome` 或
   `topic_state`。
4. 工具调用、工具结果、Step 和 Turn 终态继续写入 append-only Session Event。
5. 已确认发送的副作用不能因后续模型或工具失败被改写为未发送。
6. 真实模型超时必须保留为失败事实，不能自动伪造成 `turn.completed`。

因此不采用以下方案：

- 删除 `sent_message_ids` 并由 Runtime 自动填充；
- 把发送工具改成隐式终止工具；
- 已发送回复后把任意后续异常自动改写为成功；
- 仅提高超时以掩盖状态机问题。

## 3. 修复一：完成工具错误回灌

### 3.1 当前问题

`core/agent_loop.py` 对 `chat_turn_complete` 的任意错误设置
`completion_error`，随后立即调用 `_complete_step_and_turn(..., outcome="failed")`。
模型看不到这次失败结果，也没有机会使用上一轮发送工具返回的当前 Turn 消息 ID 修正。

### 3.2 目标行为

```text
send_napcat_replies
  -> tool.completed(message_ids=[current-turn ids])
  -> next model request
  -> chat_turn_complete(wrong ids)
  -> tool.failed(SEND_NOT_CONFIRMED, confirmed ids)
  -> next model request
  -> chat_turn_complete(exact confirmed ids)
  -> tool.completed
  -> turn.completed
```

`AgentLoop` 删除 `chat_turn_complete` 首次失败即终止的特殊分支。失败结果按普通 ToolResult
写入 `tool.failed`，当前 Step 正常闭合，并进入下一 Step。现有“同一不可重试错误重复两次
才终止 Turn”的保护继续生效，避免模型无限重复错误参数。

### 3.3 ToolResult 契约

`SEND_NOT_CONFIRMED` 返回结构化内容：

```text
submitted_message_ids
confirmed_message_ids
unknown_message_ids
missing_message_ids
```

其中 `confirmed_message_ids` 来自当前 Turn 的 `TurnReplyLedger`。模型下一 Step 必须使用
该列表重试终止工具。

`outcome="replied"` 使用顺序一致的完整列表校验，而不是当前的子集校验：

- 完全一致：允许完成；
- 引用了非当前 Turn ID：`unknown_message_ids`；
- 漏掉当前 Turn 已发送 ID：`missing_message_ids`；
- Ledger 为空：不能声明 `replied`。

`ignored` 和 `failed` 仍由模型显式选择；它们不能清除或否认已经存在的发送事实。

## 4. 修复二：失败回执尊重已发送事实

### 4.1 当前问题

`TurnReceiptDriver` 只检查新事件中是否存在 `turn.failed`。只要 Turn 失败，就提交通用失败
回执；它没有检查同一 Turn 是否已经产生 `outbox.sent`。

### 4.2 目标行为

可靠性层从本轮新增的持久化 Session Event 判断是否已有用户可见输出：

| Turn 终态 | 本轮 `outbox.sent` | 用户行为 |
| --- | --- | --- |
| failed | 无 | 发送一次幂等失败回执 |
| failed | 有 | 不再发送失败回执；保留内部失败事件 |
| completed | 任意 | 不发送失败回执 |

判断必须基于 Session Event，而不是只读进程内 `TurnReplyLedger`。这样重启恢复、回执对账
和运行时内存清理使用同一份持久化事实。

模型超时仍记录：

```text
step.completed(status=failed, error_code=MODEL_TIMEOUT)
turn.failed(error_code=MODEL_TIMEOUT)
```

如果超时前已有 `outbox.sent`，用户已经收到有效回复，不再追加误导性的“处理失败”。
如果超时前没有任何发送事实，维持当前明确失败回执。

Turn 进入任意终态后清理对应的内存 Reply Ledger，避免失败路径长期保留 run ID。

## 5. 与 DeepSeek Harness 的对应

| DeepSeek Harness 原则 | Pumpkin 修复 |
| --- | --- |
| Tool call/result 先持久化，再决定下一 Step | 完成工具首次失败不再直接终止 Turn |
| 模型从 ToolResult 修正后续调用 | 错误结果返回当前 Turn 的确认、未知和缺失 ID |
| Session log 是模型和恢复的事实源 | 失败回执读取持久化 `outbox.sent` 事件 |
| 未处理的模型请求错误可以真实终止 Turn | `MODEL_TIMEOUT` 继续记录为 `turn.failed` |
| 已交付内容不会因后续失败被撤销 | 已发送回复后不再发送矛盾的失败回执 |

本次不新增自动模型请求重试。若后续需要重试，应单独设计等价于
`agent/request-error` 的恢复接缝，由策略明确返回 retry，并保证同一 Step 不重复提交用户
输入；不能在 Provider 或业务工具中散落隐式重试。

聊天模型的单次请求超时由 15 秒调整为 60 秒，为低思考档位下偶发的长响应保留余量。
该配置只改变 Provider 请求边界，不改变 Turn/Step、工具回灌或终止语义；超时后仍按上述
真实失败路径处理，不自动重试。

## 6. 修改范围

实现修改：

- `plugins/pumpkin/core/config.py`、`.env.example`
  - 将 `PUMPKIN_CHAT_TIMEOUT_SECONDS` 默认值调整为 60 秒。
- `plugins/pumpkin/core/agent_loop.py`
  - 删除完成工具首次失败即终止的特殊分支；
  - 保留重复不可重试错误上限。
- `plugins/pumpkin/runtime/chat_tools.py`
  - 完整校验当前 Turn 消息 ID；
  - 返回结构化纠错事实。
- `plugins/pumpkin/runtime/reliability.py`
  - 根据同一 Turn 的 `outbox.sent` 决定是否发送失败回执；
  - 在 Turn 终态清理 Reply Ledger。
- `plugins/pumpkin/tests/`
  - 增加工具纠正、超时回执和 Ledger 清理回归测试。

本次不修改模型 Prompt，不增加中文关键词规则，不改变群聊唤醒、任务状态机、Outbox
发送重试或模型超时配置。

## 7. 验收标准

1. 首次错误提交历史消息 ID 时，模型能在下一 Step 读取 ToolResult 并以当前 Turn 的完整
   ID 列表完成 Turn。
2. 连续两次提交同一错误参数时，Turn 明确失败且不会继续产生副作用回复。
3. 已成功发送至少一条消息后发生 `MODEL_TIMEOUT`，Session 保留失败终态，但群内不出现
   通用失败回执。
4. 尚未发送任何消息即发生 `MODEL_TIMEOUT`，仍发送一次幂等失败回执。
5. `replied` 使用空列表、遗漏 ID、额外 ID 或乱序 ID 时均返回可纠正的结构化错误。
6. 成功、失败、超时和取消路径结束后，Reply Ledger 不保留对应 Turn。
7. 未配置覆盖值时，聊天模型 Provider 使用 60 秒单次请求超时。
8. `uv run pytest` 在 `plugins/pumpkin/` 全量通过，根目录 `git diff --check` 通过。

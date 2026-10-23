---
status: implemented
owner: Mimi maintainers
updated: 2026-10-22
---

# Pumpkin 消息归属与任务事件

## 背景

当前 Pumpkin 的作用域 Router 对被唤醒消息先执行 Task Manager intake。Task Manager
完成模型轮次后，Runtime 才能把消息路由到 Chat Agent。这样会产生三个问题：

1. 普通的“南瓜在吗”会等待任务识别，Chat 首响被阻塞。
2. 任务原始消息、任务补充消息和普通聊天没有稳定的持久化所有权。
3. 任务开始确认通过 ChatReplyRequest 直接写入 Chat 窗口，未来增加任务事件时容易重复发送。

`_is_presence_check()` 只能作为临时止血规则，不能作为长期的意图路由架构。

## 目标

- 一条入站消息只能拥有一个持久化路由所有者。
- Task Manager 不直接发送用户消息，只负责任务判断、创建和更新。
- 任务接受、补充、等待和失败等用户可见状态通过幂等事件进入 Chat。
- Chat Agent 只表达已确认事实，不重新判断任务归属或任务状态。
- 普通聊天不等待 Task Manager；任务补充不被 Chat Agent 当成普通聊天。
- 进程重启、模型重试和发送超时不会产生可确认的重复回复。

## 非目标

- 本次不改变 Agent Prompt 的人格和语言风格。
- 本次不改变 Task Agent 的工具权限和任务执行逻辑。
- 本次不保证 NapCat 在“请求已发出但响应丢失”时做到外部严格 exactly-once；
  该场景只能进入 `delivery_unknown`，不能盲目重试。

## 核心概念

### 消息所有权

`MessageDispatchRow.route` 是入站消息的唯一所有权：

| 所有者 | 含义 |
| --- | --- |
| `chat` | Chat Consumer 可以处理原始消息 |
| `provisional` | 已确认需要即时 Chat 响应，同时等待 Task Manager 完成语义归属 |
| `task` | Task Consumer 可以处理原始消息 |
| `background` | 只进入上下文，不产生回复 |
| `waiting` | 已属于任务，但当前等待任务状态处理 |

消息一旦从 `pending` 变成其他路由，其他 Consumer 不得再领取原文。

### 任务来源与补充

任务持久化维护两类消息 ID：

- `source_message_ids`：创建任务的原始消息。
- `supplement_message_ids`：后续被任务吸收的补充消息。

补充消息必须满足以下任一关联条件：

1. 回复任务来源消息；
2. 回复任务开始确认或任务状态消息；
3. 当前任务为 `waiting_for_input`，且消息经过 Task Manager 确认；
4. 用户明确引用任务标识或任务对象。

“当前作用域存在活跃任务”不能单独作为关联依据。

## 目标链路

### 普通聊天

```text
NapCat message
  -> archive
  -> structural router
  -> route=chat
  -> Chat Consumer
  -> send_chat_reply
```

### 新任务

```text
NapCat message
  -> archive
  -> route original message=provisional
  -> Chat Consumer 立即消费原文
  -> Task Manager 独立消费候选批次
  -> create Task + TaskAccepted event in the same idempotent creation flow
  -> provisional 原子晋级为 task
  -> Chat Consumer renders TaskAccepted once when no earlier Chat reply exists
  -> Task Agent executes independently
```

### 任务补充

```text
supplement message
  -> archive
  -> route=task
  -> Task Manager validates task_id + revision
  -> update Task revision and supplement IDs
  -> TaskUpdated / TaskClarification event
  -> Chat Consumer renders the confirmed fact
```

原始补充消息不再进入 Chat Agent 的普通聊天窗口。需要用户可见反馈时，Chat Agent
只消费内部事件。

## Consumer 边界

Runtime 仍然只归档一次消息。Router 游标只负责归档批次前进，Chat 和 Task Manager
使用独立的持久化状态：

- Chat Consumer：消费已经归属 `chat/provisional` 的入站消息，以及内部
  ChatReplyRequest。
- Task Manager：消费 `provisional/task` 候选批次、任务补充和任务恢复消息；模型调用不
  回写旧 Router 游标。

Task Manager 的模型调用不能阻塞 Chat Consumer。一个作用域内的任务执行也不能阻塞
后续普通聊天；任务判定完成后，`provisional` 消息才会晋级到最终 `chat` 或 `task`
所有权。

路由器只使用结构化关系做第一层判断：

- 是否 @ Pumpkin；
- 是否回复 Pumpkin 或任务事件；
- `reply_to_message_id` 是否属于任务来源/补充；
- 当前消息是否来自已确认的任务等待输入状态。

语义上的“这是新任务还是普通聊天”由 Task Manager 的独立 intake 完成，不在
`application.py` 中维护中文短语列表。

## 任务事件与 Outbox

新增持久化任务通知 Outbox，建议字段：

| 字段 | 说明 |
| --- | --- |
| `event_id` | 唯一事件 ID |
| `task_id` | 任务 ID |
| `scope_id` | QQ 作用域 |
| `event_type` | `accepted`、`updated`、`clarification` 等 |
| `source_message_ids` | 触发本事件的消息 |
| `revision` | 事件对应的任务 revision |
| `payload` | 已确认事实和表达意图 |
| `idempotency_key` | 事件幂等键 |
| `status` | `pending`、`sending`、`sent`、`delivery_unknown`、`failed` |
| `sent_message_ids` | NapCat 返回的消息 ID |

事件唯一约束：

```text
UNIQUE(task_id, event_type, revision, source_message_ids)
```

开始确认使用：

```text
task-accepted:{task_id}:1
```

任务补充使用：

```text
task-updated:{task_id}:{revision}:{source_message_id}
```

现有 `_request_task_started_reply()` 应改为创建 `TaskAccepted` Outbox 事件，
不能继续直接追加 Chat 窗口。`create_task()` 和 Runtime 自动确认只能保留一个入口。

## 职责划分

### Runtime

- 归档消息；
- 原子领取消息所有权；
- 维护任务事件和 Outbox 状态；
- 处理 revision、幂等键和发送结果；
- 不生成自然语言，不根据中文短语判断任务。

### Task Manager

- 判断任务候选；
- 创建任务；
- 将消息标记为任务来源或补充；
- 更新任务 revision；
- 产生任务事件；
- 不直接调用 NapCat。

### Chat Agent

- 消费 `chat` 原始消息；
- 消费已确认的任务事件；
- 组织自然语言；
- 调用唯一的 Chat 发送工具；
- 不重新处理已经属于 `task` 的原始消息。

### Task Agent

- 只执行指定任务；
- 交付验证后的结果；
- 产生任务状态和交付事件；
- 不发送开始确认。

## 重复回复防护

必须同时满足以下条件：

1. 原始消息只能成功领取一次；
2. `TaskAccepted` 等任务事件使用数据库唯一幂等键；
3. `ChatReplyRequest` 与 Outbox 事件一一对应；
4. Chat Agent 不能同时消费任务原文和同一任务事件；
5. 已发送消息写入 `sent_message_ids` 后，事件才进入 `sent`；
6. NapCat 响应超时时进入 `delivery_unknown`，禁止自动盲重试；
7. 进程启动时只恢复 `pending/sending` 且有明确重试策略的事件。

## 迁移策略

1. 保留现有 `ReplyRequestRow` 作为兼容读取层。
2. 新建 TaskNotification/Outbox 表，并为已有开始确认生成兼容事件。
3. 先让 TaskAccepted 使用 Outbox，验证无重复后移除
   `_request_task_started_reply()` 的直接窗口写入。
4. 切换 Chat/Task 独立游标和路由领取。
5. 删除 `_is_presence_check()` 及其他语义短语旁路。
6. 清理已完成的旧 ReplyRequest，不删除原始消息归档。

## 验证用例

- “南瓜在吗”不等待 Task Manager，Chat 能独立回复。
- 新任务只产生一条 TaskAccepted 回复。
- Task Manager 模型重试不会产生第二条开始确认。
- 任务补充更新 revision，Chat 不重复回复原始补充消息。
- 回复任务开始确认时，补充消息归属 Task 而不是 Chat。
- Chat Consumer 正常工作时，Task Manager 超时不影响普通聊天。
- Outbox 发送成功但进程在状态更新前重启，不产生可确认的重复消息。
- NapCat heartbeat 不进入消息归档，也不打印业务 Traceback。
- 进程重启后，未发送事件可以恢复，已发送事件不会恢复。

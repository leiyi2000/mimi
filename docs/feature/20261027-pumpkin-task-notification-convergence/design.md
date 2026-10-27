---
status: implemented
owner: Mimi maintainers
updated: 2026-10-27
commits: []
---

# Pumpkin 任务通知与音频交付收敛

## 1. 背景

Pumpkin 将每个持久化 `TaskEvent` 都投递到 Conversation Inbox。任务生成候选 Artifact
时，`task.artifact_ready` 会启动一个 Conversation Turn；该轮没有
`TASK_DELIVERY` capability，但仍可调用普通聊天回复。因此模型可能先发送“已经做好，
正在发送”，随后在 `task.completed` 轮次再次执行真正交付。

音频交付还有第二层放大：`NapCatSender` 为保证 QQ 语音消息可发送，会先单独发送
`record`，再发送引用、@ 和完成文字。一次简单语音请求因此可能产生：

1. 任务开始确认；
2. 候选 Artifact 的中间播报；
3. 语音消息；
4. 引用、@ 和完成文字。

候选 Artifact 不代表任务已经通过验收，内部进度也不等于用户需要采取行动。通知边界应由
Runtime 根据事件语义确定，不能依赖模型自行保持安静。

## 2. 目标

- 简单的单音频任务只产生一条开始确认和一条最终语音。
- `task.artifact_ready` 等内部事件继续完整持久化，但不打扰用户。
- 等待输入、失败、部分完成和完成仍有明确的用户可见结果。
- 用户侧完成以 Outbox 确认发送成功为准，不能只依据任务状态。
- 进程恢复不重复投递内部事件，也不漏投需要用户感知的终态事件。

## 3. TaskEvent 可见性

新增统一的 TaskEvent 通知分类，供实时发布与启动恢复共同使用：

| 事件 | Conversation Inbox | 用户行为 |
| --- | --- | --- |
| `task.accepted` | 否 | 创建任务时已有确定性确认 |
| `task.started` | 否 | 仅持久化 |
| `task.progress` | 否 | 仅持久化，包括交付回执 |
| `task.revision_changed` | 否 | 修改任务的当前用户 Turn 负责确认 |
| `task.description_updated` | 否 | 仅持久化 |
| `task.artifact_ready` | 否 | 候选产物尚未通过终态验收 |
| `task.waiting_for_input` | 是 | 只询问完成任务所需的信息 |
| `task.completed` | 是 | 交付终态结果或说明部分完成 |
| `task.failed` | 是 | 发送简短事实性失败说明 |
| `task.paused` | 是 | 说明任务暂停及恢复条件 |
| `task.deleted` | 否 | 删除命令所在 Turn 已负责确认 |

分类器是领域策略，不由 Prompt 或中文关键词决定。`TaskEventNotifier` 对内部事件直接返回，
不创建 Conversation Inbox 条目。恢复查询使用同一分类器，只对用户可见事件检查缺失的
Inbox 条目，避免内部事件在每次启动时被反复识别为“未通知”。

所有事件仍保留在 `pumpkin_task_events`，任务查询、审计、纠正和恢复任务状态不受影响。

## 4. TaskEvent Turn 权限

用户可见 TaskEvent 按目的分配最小能力：

- `task.completed` 且状态为 `completed` 或 `partially_completed`：允许
  `send_task_delivery`，不允许普通聊天发送工具；
- `task.waiting_for_input`、`task.failed`、`task.paused`：允许最多一条普通文字回复，
  不允许 Artifact 交付；
- 其他 TaskEvent 即使因旧数据或恢复路径进入 Inbox，也不允许发送消息。

`chat_turn_complete` 继续核对本 Turn 的完整发送回执。终态交付成功后，模型只能提交 Turn
完成，不能再追加“已经发给你”等第二条文字。

## 5. 单音频交付

`DeliveryService` 在以下条件同时满足时使用音频单消息模式：

1. 当前任务状态为 `completed`；
2. 本次交付恰好包含一个已验证、`delivery_ready` 的音频 Artifact；
3. 没有其他图片、文件或文本 Artifact；
4. 当前 revision 没有未完成事项。

该模式忽略模型提供的重复完成文案，不附加 `reply_to_message_id` 和 `mention_user_id`，
向 Outbox 提交一个仅包含 `record` 的载荷。`NapCatSender` 因此只调用一次
`send_group_msg`，Outbox 必须得到一个 `message_id` 才算交付成功。

以下情况不使用单消息模式：

- `partially_completed`：必须保留未完成说明；
- 同时交付多个 Artifact 或非音频 Artifact；
- 等待、失败或发送结果异常；
- 交付内容包含必须独立呈现的文本 Artifact。

任务开始确认已经引用原请求并指明任务归属。最终语音紧随该任务到达，不再用第二条引用和
@ 文本重复建立归属。

## 6. 状态与失败语义

任务终态和用户侧交付状态保持分离：

```text
Task completed
  -> task.completed enters Conversation Inbox
  -> DeliveryService validates terminal revision and Artifact
  -> Outbox sends record
     -> sent: record delivery receipt and finish Turn
     -> failed: send one factual failure receipt when safe
     -> unknown: do not retry automatically; report uncertain outcome
```

语音生成成功但 Outbox 为 `failed` 或 `unknown` 时，不得发送“任务已经交付”。`unknown`
保持不自动重发，避免 QQ 已收到语音但 Runtime 因响应丢失再次发送。

纯文本任务仍在 `task.completed` Turn 发送最终结论；本设计只消除内部事件播报和单音频
交付中的重复说明，不引入静默完成。

## 7. 调用边界

```text
Task Executor
  -> TaskService persists every TaskEvent
  -> TaskEvent visibility policy
     -> internal event: stop
     -> user-visible event: Conversation Inbox
        -> waiting/failed/paused: one text reply
        -> completed: DeliveryService
           -> single completed audio: record-only Outbox
           -> other delivery: existing typed Artifact delivery
```

预计涉及：

- `runtime/task_events.py`：统一事件可见性；
- `runtime/recovery.py`：恢复查询复用同一可见性；
- `runtime/turn_policy.py`：TaskEvent 的回复与交付能力分离；
- `runtime/delivery.py`：单音频单消息交付模式；
- `runtime/tools.py`：明确终态交付不得追加过程播报；
- `tests/`：覆盖实时发布、恢复、权限和 Outbox 消息数。

不修改 Task Executor 的 Artifact 生成流程，不删除 `task.artifact_ready`，不改变 Artifact
验收与纠正门禁。

## 8. 验收标准

1. `task.artifact_ready`、`task.progress` 和 `task.description_updated` 会持久化，但不会
   创建 Conversation Inbox 条目或发送消息。
2. 启动恢复不会补投内部事件；未通知的等待、失败和完成事件仍会补投一次。
3. `task.completed` Turn 看不到普通聊天发送工具，只能交付并结束。
4. 完成且只含一个音频 Artifact 的交付只产生一个 Outbox `message_id`。
5. `partially_completed` 音频任务保留必要文字说明。
6. Outbox `failed` 和 `unknown` 不会被表述为已交付，也不会自动重复发送。
7. 既有图片、文件、长文本、纠正和任务状态查询行为不回归。
8. Pumpkin 全量测试、Ruff 和 `git diff --check` 通过。

---
status: implemented
owner: Mimi maintainers
updated: 2026-10-23
commits: []
---

> 当前实现已在 2026-10-23 由
> [Pumpkin 完全对齐 DeepSeek Harness](../20261023-pumpkin-harness-alignment/design.md)
> 收敛。本文保留原始设计决策；模块边界、数据库状态和当前代码以新档案及
> [`ARCHITECTURE.md`](../../ARCHITECTURE.md) 为准。

# Pumpkin Harness Agent 重构

## 1. 目标

Pumpkin 参考 DeepSeek Harness 的 Agent、Session、Turn、Step、Inbox 和能力接缝设计，
重构为两个职责清晰的运行单元：

- `ConversationAgent`：每个 QQ scope 一个长期 Agent，负责理解消息、自然回复、创建或
  管理任务，以及表达任务事件。
- `TaskExecutor`：每个运行中任务一个短期 Agent，只执行已登记任务并产生结构化事件，
  不直接发送 QQ 消息。

删除独立 `Task Manager Agent`。任务状态迁移由确定性的 `TaskService` 负责，模型不直接
修改数据库。网页、代码、图片、音频和 HTML 等能力继续保留，通过插件向 Tool Runtime
注册。

本次重构借鉴上游不变量，不直接引入 TypeScript Cordis，也不复制 DeepSeek Harness 的
Web、Desktop、SDK、配置 Profile 和完整事件体系。

## 2. 上游设计映射

| DeepSeek Harness | Pumpkin |
| --- | --- |
| `Agent` live handle | `ConversationAgent` / `TaskExecutor` |
| durable Inbox | `conversation_inbox` |
| append-only `SessionEvent` | `agent_event` |
| Turn | 一次唤醒到 Agent 回到 idle |
| Step | 一次模型请求及其工具调用 |
| Session projection | `ConversationProjection` / `TaskProjection` |
| `ctx.tools` | `ToolRegistry` |
| scoped tool restriction | Agent profile + task `allowed_skills` |
| Cordis plugin/effect | Python `PluginManager` + reversible registration |

必须保持：

1. Agent 是长期对象，AgentLoop 是可替换驱动。
2. 一个 Turn 包含零到多个 Step；一个 Step 只包含一次模型请求及其工具执行。
3. 模型可见事实必须先形成可持久化事件，模型上下文从事件投影生成。
4. Inbox 是唯一唤醒入口；同一 scope 串行，不以多个消费者争抢同一消息。
5. 工具调用和结果具有稳定 call ID，结果状态区分未开始、已完成和结果未知。
6. Durable Event 与实时状态通知分离。
7. 工具权限在代码层校验，不能只写在 Prompt。

不引入：

- Cordis 运行时、YAML Profile 和热重载。
- 完整流式 UI 事件、Session fork、compaction 和远程 SDK。
- 为单进程 QQ 插件不需要的分布式中间件。

## 3. 领域对象

### 3.1 Agent

```text
Agent
  agent_id
  scope_id
  profile: conversation | task
  session_id
  inbox
  status: idle | running
  send(message, target, wakeup)
  cancel(cause, keep_inbox)
  when_idle()
```

`ConversationAgent.agent_id` 由 scope 稳定派生。`TaskExecutor.agent_id` 由
`task_id + revision` 派生。Agent 本身不包含模型厂商逻辑和 QQ 发送实现。

### 3.2 Inbox

Inbox 持久化两类输入：

- `next_turn`：用户消息或任务事件，各自开启新 Turn。
- `next_step`：当前 Turn 的补充上下文或控制事件，在下一个 Step 生效。

每条 Inbox item 有稳定 ID、来源类型、载荷、插入序号和状态：

```text
pending -> claimed -> completed
pending -> discarded
```

相同 item ID 不能重复进入 pending。进程启动时将崩溃前遗留的 `claimed` 重排为
`pending`；如果同一 Inbox item 已存在 completed/failed Turn 终态事件，则直接校准为
`completed`，不重放。开放 Turn 先记录 interrupted，已完成的副作用通过 Tool/Outbox
幂等键去重。

### 3.3 Session Event

`agent_event` 是追加式事实日志，每个 Session 内 `sequence` 单调递增：

```text
turn.started
step.started
user.message
assistant.message
tool.called
tool.started
tool.completed
tool.failed
step.completed
turn.completed
turn.failed
turn.cancelled
task.event
outbox.requested
outbox.sent
outbox.failed
outbox.unknown
```

事件载荷必须是 JSON 可序列化值。历史事件不原地修改；当前状态由 Projection 折叠。
模型消息由 `user.message`、`assistant.message`、`tool.called` 和工具终态投影，不使用
Runtime 内存 transcript 作为恢复依据。

### 3.4 Turn 与 Step

一个 Turn 在 Agent 认领 `next_turn` 输入后开启，在没有待处理工具结果或 `next_step`
输入时结束。每个 Step：

1. 从 Inbox 认领输入。
2. 追加 `step.started` 和模型可见输入事件。
3. 从事件日志投影模型历史。
4. 请求模型。
5. 追加 `assistant.message`。
6. 记录并执行工具调用。
7. 按模型顺序追加工具终态。
8. 追加 `step.completed`。

只读且显式标记并发安全的工具可以并行；其他工具形成顺序屏障。写工具的结果必须先落
事件，再允许下一 Step 使用。

## 4. 对话与任务

### 4.1 ConversationAgent

所有通过适配器校验且符合参与边界的消息只进入一个 Conversation Inbox，不再同时送给
Chat 和 Task Manager 两个消费者。Conversation Agent 根据上下文决定：

- 回复或保持安静；
- 创建任务；
- 查询、补充、暂停、恢复或取消任务。

群聊参与边界由 Runtime 先确定，再决定是否写入 Inbox：

- 未激活群聊只接受 `@` 或助手名前缀，不因普通群消息自动激活。
- 激活后的参与边界是当前群话题，不是某个发言人。话题中的其他成员可以继续参与；
  Conversation Agent 每轮必须通过 `chat_turn_complete.topic_state` 声明话题继续或结束。
  Runtime 只执行该结构化判断，不用固定消息数或发言人 ID 代替语义判断；TTL 仅作兜底。
- `闭嘴` 是确定性控制命令。Runtime 将当前作用域写入持久休眠状态并停止
  普通消息入队；只有下一次明确唤醒能够解除，进程重启不会隐式恢复。
- 普通聊天发送工具不接受 QQ 引用目标。图片消息必须在当前 Turn 留下媒体读取证据后才
  能发送回复；读取失败也保留尝试事实，以便明确告知用户失败原因。

任务生命周期工具是 Conversation Profile 的内建协议，不进入 Skill 目录或 Task Executor
的 `allowed_skills`。创建任务成功后，Runtime 使用稳定幂等键立即写入并发送 Conversation
Agent 提供的简短自然确认，且不得把内部任务 `goal` 作为用户文案，然后结束当前
Conversation Turn；不等待 TaskExecutor，也不允许模型在同一 Turn 再更新任务或重复确认。

`task.event` 输入只用于表达已经持久化的任务事实。该 Turn 的工具面由程序移除
`create_task` 和 `update_task`，避免失败、完成或发送结果未知事件被模型解释成新任务或
旧任务改写。

### 4.2 TaskExecutor

TaskExecutor 只读取：

- 当前 `task_id + revision` 的任务投影；
- 任务来源消息和已归属的补充消息；
- 当前任务事件；
- 创建任务时授予的能力。

TaskExecutor 不能调用 QQ 发送工具。Artifact 就绪、等待输入、完成和失败均写成 TaskEvent。
等待、失败和完成事件使用固定 `task-event:<event_id>` 写入 Conversation Agent 的持久化
Inbox；启动恢复扫描 TaskEvent 与 Inbox 的差集并补投。Conversation Agent 只依据事件中的
持久化事实表达状态，并通过 `send_task_delivery` 交付已验证 Artifact。事件载荷保留原请求
消息和 `requester`，不允许把完成话术广播给后续发言者。

### 4.3 TaskService

状态机允许：

```text
queued -> running
running -> waiting | completed | partially_completed | failed | paused
waiting -> queued | cancelled
failed -> queued | cancelled
paused -> queued | cancelled
```

目标、约束或交付要求变化时 revision 增加。旧 revision 的 Executor 可记录过期结果，但
不得更新当前任务投影或交付 Artifact。

### 4.4 用户纠正与交付验收

首次交付可能被用户否定，但否定后的同类结果不能继续交付。Conversation Agent 将用户
纠正通过结构化工具关联到当前作用域内的原任务；Runtime 负责以下确定性约束：

- 纠正记录保存来源消息、原 revision、新 revision、纠正说明和验收条件。
- 纠正会创建同一任务的新 revision，并继承原目标、交付要求和此前所有有效纠正。
- 被否定 Artifact 的内容哈希、来源指纹和生成策略指纹进入拒绝集合；新 revision 产生的
  Artifact 只要命中任一拒绝指纹，就不能完成或发送。
- Artifact 创建后默认为候选状态。只有通过 `complete_task` 验收门的 Artifact 才标记为
  `delivery_ready`；失败通知不得扫描或发送普通候选 Artifact。
- `send_task_delivery` 只接受当前 revision、`delivery_ready` 且未被任何有效纠正否定的
  Artifact。
- 第一次违反纠正约束时返回结构化错误，允许 Executor 改用其他来源或策略；同一纠正累计
  两次违规时任务确定性进入失败状态，当前 Turn 立即停止。

纠正识别仍由 Conversation Agent 根据语义完成，不在消息入口新增关键词分支。Prompt 只
说明工具契约；持久化、revision、Artifact 失效和重复违规停止均由 Runtime 强制执行。

## 5. Outbox

所有 QQ 发送通过持久化 Outbox：

```text
pending -> sending -> sent
pending -> sending -> failed
pending -> sending -> unknown
```

`unknown` 表示请求可能已到达 NapCat，但本地未获得可确认结果，不自动重发。业务幂等键
全局唯一。只有 `sent` 事件可以成为“已回复/已交付”的事实。

长文本图片化、Artifact 转换和 NapCat 消息段构造位于 Outbox sender，不在 AgentLoop。
普通聊天默认不生成引用段；任务开始、交付和终态通知由任务所有权补齐引用与请求人定向。

## 6. 能力插件

Pumpkin 使用精简的 Python 插件上下文：

- 插件声明具名依赖。
- 插件注册服务或 Tool，并获得可逆 disposer。
- 卸载按挂载和 effect 的逆序执行。
- ToolRegistry 是稳定基础服务。

现有能力映射：

| 插件 | Provider | Tool |
| --- | --- | --- |
| web | `WebSearchProvider` | `search_web`、`fetch_url`、`download_web_image` |
| code | `CodeRunnerProvider` | `execute_code` |
| image | `ImageProvider` | `generate_image` |
| audio | `AudioProvider` | `generate_audio` |
| html | `HtmlRenderer` | `render_html` |

能力插件不能直接写任务状态或发送 QQ 消息。Provider-backed Tool 通过 Artifact Service
直接登记生成结果并返回 Artifact ID；不再向模型暴露基于本地相对路径的二次登记工具。

## 7. 失败与恢复

- 模型失败：关闭当前 Step 和 Turn，Inbox 后续输入保留。
- 工具参数非法：确定性失败，不重试。
- 工具在调用前取消：`not_started`，可由新 Step 决定是否重试。
- 工具调用后连接中断：`outcome_unknown`，禁止自动重复有副作用的调用。
- 进程崩溃留下开放 Turn：恢复时追加 `turn.interrupted`，不伪造成功。
- Executor 失败：任务进入 `failed`，只有用户明确请求恢复才重新排队。
- 用户纠正：当前 Executor 在安全边界取消，新 revision 重新排队；旧 revision 的结果不能
  进入交付链路。
- 进程重启时，原 `running` 任务转为 `waiting_for_input` 并产生中断事件，不自动重放
  结果未知的副作用；用户明确确认后才创建新的执行 attempt。
- Outbox `unknown`：等待人工或外部证据确认，不盲目重发。
- SQLite 初始化固定使用 `journal_mode=DELETE` 和 `synchronous=FULL`，避免宿主机
  bind mount 下依赖 WAL/SHM sidecar 完成持久化。

## 8. 数据迁移

保留现有 `message_archive`、`task`、`task_event`、`artifact` 和会话表。新增表使用
`generate_schemas(safe=True)` 创建：

- `pumpkin_agent_events`
- `pumpkin_agent_inbox`
- `pumpkin_outbox`

旧 `MessageDispatchRow`、Window 和 ReplyRequest 已从活动模型、Store 和运行路径移除；
维护者授权的旧表清理也已完成。新消息只进入 Archive 和 Agent Inbox。

## 9. 模块边界

```text
adapters/napcat.py       OneBot -> InboundMessage
core/session.py          append-only event log + projections
core/agent.py            Agent + Inbox + lifecycle
core/agent_loop.py       generic Turn/Step driver
core/tools.py            registry + guarded execution
core/tasking.py          deterministic task state machine
core/plugins.py          capability composition
runtime/application.py   ingress and worker lifecycle only
runtime/ingress.py       NapCat message adaptation and Inbox admission
runtime/task_runtime.py  task scheduling, recovery and executor lifecycle
runtime/artifacts.py     artifact validation, atomic persistence and correction gates
runtime/delivery.py      verified task result to Outbox conversion
runtime/outbox.py        reliable QQ sender
capabilities/*           provider-backed tools
```

`runtime/application.py` 不再包含具体能力 Handler、Prompt 语义分支或三套 Agent 调度状态。

## 10. 验收

1. 普通聊天和任务识别由同一 Conversation Agent 完成，无 Task Manager Agent。
2. TaskExecutor 运行期间 Conversation Agent 仍可处理新消息。
3. 重启后 pending Inbox、任务和 Outbox 可恢复；已完成 Tool 不重复执行。
4. 模型上下文可从 Session Event 重建，不依赖内存 transcript；投影只保留当前 Turn 和
   最近 4 个已结束 Turn，原始事件不裁剪。
5. TaskExecutor 无 QQ 发送工具，所有用户可见表达经过 Runtime 策略或 Conversation Agent
   写入 Outbox。

实现验证：

```bash
cd plugins/pumpkin
uv run pytest
uv run ruff check .
uv run python -m compileall -q .
```

当前完整回归结果为 `127 passed`。
6. 网页、代码、图片、音频和 HTML 能力保持可用，未配置能力在 Catalog 中不可加载。
7. 插件卸载会移除自身 Tool 和服务。
8. 全部 Pumpkin 单元测试通过，新增事件重放、Inbox 恢复、Turn/Step、权限和 Outbox
   未知结果测试。

---
status: implemented
owner: Mimi maintainers
updated: 2026-10-23
commits: []
---

# Pumpkin 完全对齐 DeepSeek Harness

## 1. 背景与结论

Pumpkin 当前采用了 DeepSeek Harness 的核心不变量：长期 `Agent`、持久化 Inbox、
append-only Session Event、Turn/Step、明确的 Tool Registry 和能力插件。早期迁移曾
存在两条并存的数据路径：

```text
旧路径（已废弃）：
NapCat -> MessageDispatch -> WindowCoordinator -> Chat/Task Manager

当前主路径：
NapCat -> archive -> Agent Inbox -> AgentLoop -> SessionEvent -> Tool/Task
```

旧路径曾导致以下问题，本次对齐已清理代码和活动消费者：

- 同一消息同时拥有 Archive、Dispatch、Window、Inbox 多种生命周期；
- `PumpkinRuntime` 同时承担应用启动、消息接入、任务调度、工具实现、Artifact 验收和
  交付策略；
- 能力插件虽然可以注册 Tool，但核心 Agent、Session、Store、Task 和 Outbox 仍由
  Runtime 直接持有；
- 语义路由通过 `_is_task_status_query()` 等关键词规则绕过 Agent；
- 旧设计文档仍把 `MessageRouter`、`TaskIntakeBatch` 和 `Task Manager Agent` 描述为
  当前架构。

本次目标是**完全对齐 DeepSeek Harness 的运行时原则**，而不是把 Cordis 或 DeepSeek
Harness 的 TypeScript/Web 产品直接嵌入 QQ 插件。Pumpkin 保留 NapCat、QQ scope、群聊
唤醒、任务 revision、Artifact 验收和 Outbox 这些产品边界；这些能力必须通过等价的
服务接缝接入，而不能继续堆进一个 God Object。

上游基线：

- <https://github.com/deepseek-ai/deepseek-harness>
- <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md>

## 2. 目标不变量

### 2.1 运行时组合

运行时由可替换服务和插件组合而成。没有一个业务类同时拥有所有服务的具体实现。

```text
PumpkinApplication
  -> PluginManager
       -> ingress / napcat adapter
       -> session / agent / agent-loop
       -> llm
       -> tools / skills
       -> task-jobs
       -> artifacts-deliverables
       -> outbox / qq sender
       -> persistence
```

每个可替换能力至少明确三类角色：

1. Service Definition：稳定的 Python `Protocol` 或领域接口；
2. Provider：当前实现，例如 SQLite Store、OpenAI-compatible Model Provider；
3. Consumer：AgentLoop、Tool 或 QQ Adapter。

插件只能依赖 Service Definition，不能依赖 `PumpkinRuntime` 或其他插件的具体实现。
插件注册返回 disposer，卸载按 effect 的逆序执行。

### 2.2 唯一消息入口

`AgentInbox` 是 Agent 的唯一唤醒入口。消息接入只做：

1. NapCat 事件适配；
2. 消息归档和去重；
3. scope/session 参与边界检查；
4. 向 Conversation Agent 写入一个 `next_turn` Inbox item。

不再维护 Message Dispatch Ledger、作用域游标、Chat Pending Window 或 Task Intake
Batch。消息是否是任务、是否查询任务、是否需要创建 Artifact，由 Conversation Agent
根据结构化上下文和工具契约判断。

确定性 Runtime 只校验以下事实：

- 消息和 scope 的身份；
- Agent Inbox 的幂等性；
- 工具角色和 Skill 权限；
- Task ID、revision、Artifact 所属关系；
- Tool/Outbox 的幂等键和未知结果；
- 任务状态机的合法迁移。

Runtime 不通过中文关键词判断用户语义。`_is_task_status_query()` 以及同类特例分支
必须删除。任务状态查询由 Agent 调用 `get_task_status` 表达，创建任务由
`create_task` 的结构化参数和当前消息幂等约束校验。

`direct_to_assistant` 只表示当前消息正在和助手对话，不代表用户要继续最近的任务。
普通聊天和只包含助手名称的地址消息默认使用被动任务上下文：不注入活跃任务列表和最近
任务播报；用户明确提到任务、回复任务消息或收到 `task.event` 时，Conversation Agent
才可以读取和引用任务上下文。群友仍然可以查询群内任务，Runtime 不按任务请求人限制
查询能力；这里隔离的是当前 Turn 的意图，不是成员权限。

### 2.3 Session Event 是模型上下文唯一来源

模型可见的内容必须能从 Session Event 重建。模型请求不得直接读取 Runtime 内存中的
transcript、窗口快照或未落盘的任务说明。

任务、Artifact 和 Outbox 可以有独立的领域表以支持事务和索引，但进入模型上下文时必须
通过结构化 `task.event`、Artifact 引用或 Outbox 事实事件注入 Session。

系统提示按稳定前缀组装：角色人格在前，角色决策原则在后，变化的 Inbox、任务事实和能力
目录作为 user-role 动态上下文进入 Session。主提示不复制工具参数、权限屏蔽或持久化
校验；具体操作指导由对应工具描述拥有，确定性约束由 Runtime 强制执行。

### 2.4 统一执行预算和回复交付

Conversation Agent 和 Task Executor 共用一个 `PUMPKIN_MAX_STEPS`，默认值为 36，不按
任务类型分级。AgentLoop 在达到预算的 70% 和 85% 时注入不消耗 Step 的 Runtime 收敛
提醒；达到上限时优先整理已有结果并允许部分完成，禁止继续无目的搜索。

任务交付和普通聊天回复属于不同的消息意图：

- `user.message` 轮次可以创建任务、查询状态或发送普通聊天回复，但不能直接调用
  `send_task_delivery` 交付既有 Artifact；
- `task.event` 轮次由 Conversation Agent 消费任务事件，并负责调用
  `send_task_delivery`；
- 等待、失败和完成事件使用稳定 Inbox item ID 持久化；Runtime 启动时对账 TaskEvent
  与 Conversation Inbox 并补投缺失事件，避免状态提交后进程退出导致永久漏通知；
- 如需用户明确要求重新发送已有 Artifact，应走独立的、带明确重发意图的流程，不能
  将普通聊天或任务状态查询隐式升级为交付。

同一 Turn 内所有成功发送的消息必须登记到共享的 Turn Reply Ledger。普通聊天、
任务交付和后续 `chat_turn_complete` 读取同一份发送回执，不允许由不同服务分别维护
发送消息 ID。完成工具校验失败但本 Turn 已经存在成功发送时，AgentLoop 必须终止当前
Turn，不能继续生成新的副作用回复。

任务确认支持由 Conversation Agent 提供的短句。解释失败、进度和下一步时，使用
`send_napcat_replies` 一次发送 1-3 条短消息，每条只表达一个事实，避免长文本进入
图片渲染链路和无法确认的时间承诺。

长文本和 HTML 渲染统一加载完整 CJK 字体以及单色 Emoji fallback。测试样本覆盖长破折号、
中文标点和常用 Emoji；彩色 Emoji 字体不作为运行时依赖，避免当前渲染器对彩色字体支持
不稳定导致字符丢失。

## 3. 目标模块边界

```text
runtime/app.py
  只负责启动、关闭、插件装配和生命周期协调

runtime/ingress.py
  NapCat 事件过滤、适配、归档、唤醒 Conversation Agent

runtime/task_runtime.py
  Task Job 调度、并发限制、revision 过期和恢复，不发布 Conversation Inbox

runtime/task_events.py
  TaskEvent -> Conversation Inbox 的唯一发布服务

runtime/turn_policy.py
  Conversation Inbox 来源到 Turn capability/loadout 的业务策略

runtime/chat_tools.py
  消息搜索、媒体检查、聊天回复和 Conversation Turn 完成

runtime/task_commands.py
  Task 创建、查询、更新、纠正、事件、验收和 Task Turn 完成

runtime/delivery.py
  Artifact 验收、交付权限和 Task -> Outbox 事件转换

runtime/outbox.py
  持久化消息发送和 unknown 结果处理

core/agent.py
  Agent live handle、Inbox 和生命周期

core/agent_loop.py
  通用 Turn/Step 驱动，不包含 QQ、任务或 Artifact 业务

core/turns.py
  通用 TurnPolicy、TurnToolLoadout 和 ToolContext 构造契约

core/session.py
  Session Event append、Projection 和模型消息投影

core/tools.py
  Tool Definition、角色/capability 权限和调用流水线

persistence/repositories/protocols.py
  Agent、Inbox、Message、Conversation、Task、Artifact、Outbox 和
  Idempotency 的窄持久化契约

persistence/repositories/*
  按领域拆分的 SQLite Repository 实现，只负责事实读写、事务边界和对象映射

persistence/store.py
  兼容性的 Repository 组合入口和数据库生命周期，不承载业务策略

runtime/recovery.py
  启动时的 Inbox claim 和运行中 Task 恢复编排

core/services.py
  服务定义和运行时依赖解析

core/tasking.py
  任务领域状态机和事务校验，不负责调度和模型请求

capabilities/*
  Provider-backed Tool 插件
```

`PumpkinRuntime` 不能再实现所有 `ToolHost` 方法，也不能作为能力插件的
`CapabilityHost`。工具应直接依赖明确的 `MessageArchive`、`TaskService`、
`ArtifactService`、`Outbox` 等接口。

持久化同样遵循窄接口原则。`PumpkinStore` 仅用于装配各领域 Repository 和管理
Tortoise 生命周期；运行服务按需要依赖 `AgentStore`、`TaskStore`、
`TaskRuntimeStore`、`ArtifactStore`、`OutboxStore` 等 Protocol 组合，不再把完整
Store 作为业务服务的类型依赖。`RuntimeRecovery` 负责重启恢复，`ArtifactService`
负责 correction 和 delivery-ready 门禁，Repository 不判断执行策略或用户验收语义。

## 4. 领域模型收敛

### 4.1 保留的模型

以下模型属于目标架构：

- `InboundMessage`、`MessageRecord`：NapCat 适配后的消息事实；
- `AgentEvent`、`AgentInboxItem`：Harness Agent 数据平面；
- `TaskRow`、`TaskEventRow`、`TaskDescriptionRow`：任务领域状态和事件；
- `ArtifactRow`、`ArtifactAssessmentRow`、`TaskCorrectionRow`：交付验收和纠正约束；
- `OutboxRow`：可靠 QQ 发送；
- `ChatSessionRow`、`ConversationControlRow`：QQ scope 参与边界；
- `IdempotencyRow`：跨重启副作用去重。

### 4.2 删除的旧模型

以下对象不属于目标架构，应在兼容迁移完成后删除：

- `MessageDispatchRecord`、`MessageRoute`、`IntakeStage`、`ChatStage`；
- `WindowItem`、`WindowSnapshot`、`ChatInputBatch`；
- `TaskIntakeBatch`、`TaskManagerContext`、`TaskTurnContext`；
- `ChatReplyRequest` 和 `ReplyType`；
- `MessageDispatchRow`；
- `WindowRow`、`WindowStateRow`；
- `ReplyRequestRow` 及其 Store 方法；
- `BoundedWindow`、`WindowCoordinator`；
- `TaskManagerService.request_reply()`。

`ChatSessionService` 是仍然需要保留的 QQ 参与边界服务，应从 `core/windows.py` 移动到
独立的 `core/conversation.py`，避免让文件名继续暗示窗口架构。

### 4.3 任务和后台执行

`TaskManagerService` 改名为 `TaskService`，只保留：

- 创建任务；
- 校验状态迁移；
- revision 和 correction；
- Task Event 写入；
- 当前 Task Projection 查询。

调度、信号量、恢复、取消和 Executor 生命周期移动到 `TaskRuntime`。这对应
DeepSeek Harness 的 `jobs` 能力：领域状态不拥有后台协程，后台协程也不直接修改领域
表。

`ArtifactService` 已从 `PumpkinRuntime` 提取，负责：

- 安全文件名和内容格式验证；
- 原子写入；
- SHA-256、来源和策略指纹；
- correction rejection；
- `candidate -> delivery_ready -> rejected`；
- 当前 task/revision 所属关系。

`DeliveryService` 将已确认 Artifact 和事实状态转换为 Outbox 请求；Task Executor 仍然
不能发送 QQ。

## 5. 插件和服务装配

现有 `core/plugins.py` 可以保留为 Python 版本的最小插件内核，但需要从“插件获得
Runtime 自身”改为“插件获得服务接口”：

```text
禁止：
CapabilityPlugin -> CapabilityHost -> PumpkinRuntime

目标：
CapabilityPlugin -> ArtifactService / TaskContext / Store / Config
```

基础服务通过 `ServiceDefinition` 以 Plugin 形式装配：

- `persistence-sqlite`
- `session`
- `agent`
- `agent-loop`
- `llm`
- `tools`
- `skills`
- `task-jobs`
- `artifacts`
- `outbox`
- `napcat`

Profile 不需要复制 DeepSeek Harness 的 YAML 格式，但必须存在等价的明确组合配置。
`PumpkinConfig` 只负责解析配置，不能在业务方法中隐式创建 Provider 或服务。
普通聊天回复是 Conversation Profile 的内建协议，不是 Skill；`send_napcat_reply` 和
`send_napcat_replies` 无 Skill 所有权。任务创建、查询、更新、纠正和交付分别声明
`TurnCapability`，Tool Registry 在执行 handler 前强制校验，模型可见工具裁剪不是唯一
权限边界。

## 6. 迁移顺序

### Phase 1：冻结新数据路径

- 将本设计标记为当前迁移基线；
- 删除旧设计文档中把 MessageRouter/Task Manager 描述为当前实现的章节，或明确标记为
  superseded；
- 增加新路径的事件重放和服务接缝测试；
- 禁止新增 Window、Dispatch、ReplyRequest 代码。

### Phase 2：拆分运行时（已完成）

- 新建 `MessageIngress`、`TaskRuntime`、`ArtifactService` 和 `DeliveryService`；
- 将 `handle_event()` 移入 ingress；
- 将 `_run_task()`、任务恢复和并发限制移入 task runtime；
- 将 Artifact gate 和原子写入移入 Artifact Service；
- 将交付权限和 Task -> Outbox 转换移入 Delivery Service；
- 将 Chat/Message Tool Handler 移入 `ChatToolService`；
- 将 Task Command Handler 移入 `TaskCommandService`；
- 让 Tool Registry 直接绑定两个服务，不再绑定 `PumpkinRuntime`。

### Phase 3：移除语义硬编码（已完成）

- 删除 `_is_task_status_query()` 和 `ToolContext.task_status_query`；
- 删除其他仅针对单条对话样本的关键词路由；
- 将固定控制命令注册为明确的 Human Command/Control Tool，不在入口散落字符串；
- 用结构化模型决策、工具权限和回归评测覆盖任务创建、状态查询、补充和纠正。
- 使用强类型 `AgentInboxSource` 和 `ConversationTurnPolicy` 生成每轮 loadout；
- `AgentLoop` 不再识别 `task.event`、`address_only_message` 或具体任务工具名；
- `TaskEventNotifier` 成为 TaskEvent 写入 Conversation Inbox 的唯一所有者。

### Phase 4：清理旧代码和数据库兼容层

- 迁移 `ChatSessionService` 后删除 `core/windows.py` 中窗口实现；
- 删除旧 Dispatch、Window、ReplyRequest 领域对象和 Store 方法；
- 旧表清理已按维护者授权完成，运行时不再负责删除旧表；
- 任务、事件、Artifact、Outbox 和会话表不参与清理；
- SQLite schema integrity 已在回归验证中检查。

### Phase 5：文档和验证收敛（已完成）

- 将 `PumpkinStore` 拆为窄 Repository Protocol 和按领域 SQLite 实现；
- 将启动恢复移到 `runtime/recovery.py`，将 Artifact correction gate 保留在
  `ArtifactService`；
- 更新 `ARCHITECTURE.md` 为唯一当前架构描述；
- 更新 Pumpkin README 的模块和恢复路径；
- 删除已 superseded 的设计中的当前实现描述；
- 执行 Pumpkin 全量测试、Ruff、compileall、`git diff --check`；
- 已覆盖服务接缝、事件重放、Inbox 恢复、旧表迁移和 Outbox unknown；
- 完成 Pumpkin 全量测试、Ruff、compileall 和 `git diff --check`。

## 7. 兼容和失败策略

- 已存在的 Agent Event、Task Event、Artifact 和 Outbox 数据必须可恢复；
- 旧 Window/Dispatch/ReplyRequest 数据不再进入新 Agent Context；
- 旧任务如果已有 `source_message_ids`，启动时只按 TaskRow/TaskEventRow 恢复；
- 运行中的任务恢复为 `waiting_for_input`，结果未知的副作用不自动重放；
- Provider 未配置、插件依赖缺失和 schema 不兼容必须在最早可判断的位置显式失败；
- 不允许通过兼容别名继续保留两套活跃实现。

## 8. 验收标准

1. `runtime/app.py` 只包含应用装配和生命周期协调，不包含具体 Tool Handler、
   Artifact Gate 或 Task Executor 调度实现。
2. 主消息链路只有 `NapCat -> InboundMessage -> Archive -> AgentInbox`。
3. 代码中不存在活动的 Window、Dispatch、Task Manager Agent 或 ReplyRequest 消费者。
4. 所有模型可见上下文都能从 Session Event 和明确的领域事实重建。
5. 能力插件不依赖 `PumpkinRuntime`，可独立卸载并移除 Tool/Provider。
6. Task Executor 无 NapCat/Outbox 发送权限。
7. 状态查询不依赖中文关键词正则。
8. 旧数据库可以在不丢失任务、事件、Artifact 和 Outbox 的前提下完成兼容迁移。
9. 受影响测试全部通过，且新增测试覆盖每个服务接缝和迁移失败路径。

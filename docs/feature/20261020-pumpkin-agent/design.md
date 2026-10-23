---
status: implemented
owner: Mimi maintainers
updated: 2026-10-21
commits: []
---

# Pumpkin AI 虚拟人物设计

## 1. 目标与范围

Pumpkin 是 Mimi 中面向 QQ 群聊和私聊的 AI 虚拟人物插件。它以自然对话为主要能力，
以任务执行为辅助能力：

- Chat Agent 负责自然聊天、维持人格、决定普通聊天是否回复，以及执行其他 Agent 委托
  的用户回复；不负责任务识别、任务查询或任务编排。
- Task Manager Agent 独立读取群消息，识别群友下发给助手的任务，管理多个单一任务的
  创建、暂停、删除、状态查询和重新编排；需要回复群友时委托 Chat Agent。
- 单任务 Agent 负责在后台执行一个明确目标，并产出可验证的最终交付物。
- NapCat 负责 QQ 消息接入和发送；Pumpkin 不直接把渠道协议散落到业务逻辑中。
- 模型请求通过统一的模型适配层访问，业务层不绑定某一家模型供应商。

本文同时作为实现基线和验收依据。当前实现位于 `plugins/pumpkin/`：
领域契约和持久化在 `core/`，NapCat 协议适配在 `adapters/`，Agent Loop 和运行时编排在
`runtime/`，模型适配在 `providers/`，Skill 契约在 `skills/`。

## 2. 核心概念

### 2.1 Message

NapCat 的原始事件先经过适配器，转换成稳定的 `InboundMessage`。至少包含：

- `message_id`
- `conversation_type`：群聊或私聊
- `scope_id`：群号或私聊用户标识
- `sender_id`、`sender_name`
- 有序消息段：文本、@、回复、图片、文件等
- `received_at`
- 是否来自机器人自身

模型和业务服务只依赖领域消息，不直接读取 OneBot/NapCat 原始 JSON。

### 2.2 Chat Session

Chat Session 是一个按作用域维护的短期对话状态，不是永久会话：

- 一个作用域最多同时存在一个 Chat Session。
- Session 的键是 `scope_id`，不是某一个群成员。
- Session 有 `inactive`、`active` 和 `expired` 三种确定性状态。只有 `active` 状态才会
  接收无需 @ 或前缀的普通聊天消息。
- 群聊首次收到有效消息时默认创建 Session；Chat Agent 仍根据上下文决定是否回复。
  Session 进入 `quiet` 后，只有 @机器人或配置的助手名称前缀可以重新激活；私聊的首条
  有效消息可以激活。Session 激活后，在 `session_ttl_seconds` 内，同一作用域的后续
  有效消息无需再次唤醒即可进入处理流程。
- Session 在成功发送用户可见回复后续期；每次续期从最近一次成功发送时间重新计算，
  续期时间由配置控制。普通消息被接收或进入队列不自动续期，避免持续噪声让 Session
  永不过期。
- Session 过期后，新的普通消息仍可写入归档和预唤醒窗口，但不会进入 Chat Agent；
  下一条唤醒消息会带上有界窗口快照重新激活 Session。
- Session 只保存有限的聊天上下文和摘要；任务卡片、任务摘要和任务事件由 Task Manager
  持有，Chat Agent 不直接读取。
- Session 默认加载群消息查找 Skill 和 NapCat 回复 Skill。

作用域统一使用带类型前缀的字符串：

- 群聊使用 `group:<group_id>`，一个群最多一个 Chat Session，任务也只在本群作用域内
  可见和可操作。
- 私聊使用 `private:<user_id>`，一个用户最多一个 Chat Session；私聊首条有效消息可以
  激活 Session，后续消息不要求 @ 或特殊前缀。
- 群聊和私聊的 Session、任务、消息窗口相互隔离，不能通过猜测或手工传入 ID 跨作用域
  读取数据。

Session 不等同于永久历史。长期记忆如果未来需要引入，必须单独设计存储、隐私和过期
策略，不能把全部群聊原文无限累积在 Session 中。

### 2.3 消息归档与有界窗口

消息归档独立于 Chat Session 存在。所有通过适配器校验的入站消息都会先写入归档，
不因当前是否有 Session、Chat Agent 是否忙或任务是否正在执行而跳过。归档负责去重、
游标和审计；它不是直接提交给模型的无限历史。

运行时按作用域维护两类有界窗口：

- `PreSessionWindow`：Session 未激活或已过期时保留最近消息，用于下一次唤醒时提供有限
  背景；它不触发 Chat Agent。
- `ChatPendingWindow`：Session 激活后，Task Manager 已判定为普通聊天、但 Chat Agent
  当前忙时暂存的消息；也可临时承载过期 Session 的任务回复唤醒。窗口在 Chat Agent
  空闲后一次性生成一个 `ChatInputBatch`，不把每条消息分别启动为一次 Chat Agent 请求。

两个窗口都采用“滚动原文尾部 + 压缩摘要”的结构：

- 原文尾部保留最近消息的有序结构，包括发送者、时间、文本和媒体引用。
- 较早消息在达到时间或数量边界后从原文尾部移出，压缩进有界摘要。
- 摘要只保留对后续对话可能有用的事实、未完成话题、用户表达的偏好和上下文关系，
  不保留无限制的逐字记录。
- 摘要达到上下文上限时继续滚动压缩，不能通过追加字段让窗口无限增长。
- 图片、文件和回复消息保留结构化引用；无法长期保留的媒体不伪造内容。
- 唤醒时生成窗口快照，快照包含摘要、原文尾部和唤醒消息，按消息顺序提交给新建或
  重新激活的 Chat Session。
- `ChatPendingWindow` 刷新时生成 `ChatInputBatch`，包含窗口内按序消息、压缩摘要、
  `first_sequence`、`last_sequence` 和是否发生过窗口压缩；Chat Agent 一次消费整个批次。
- 队列窗口到达时间、数量或字符上限时，不能静默删除消息。已归档的原文仍可审计，提交
  给 Chat Agent 的批次通过摘要和消息引用表达被压缩部分，并显式标记 `window_truncated`。

窗口压缩复用 Chat Prompt 的上下文压缩模式，不新增独立的上下文压缩提示词。压缩失败时保留
可用的原文尾部并丢弃最旧摘要，不能阻塞群消息接收，也不能把未压缩的全部历史交给模型。

### 2.4 Task Manager Agent

Task Manager Agent 是任务域的生命周期管理者：

- 管理同一作用域内任意数量的单一任务。
- 通过消息归档和消息查找 Skill 主动获取群友消息，不依赖 Chat Agent 转交任务。
- Task Manager 的摄取和调度通道与 Chat Agent 的忙闲状态解耦；Chat Agent 忙时，任务
  仍可被识别、创建和后台执行。
- 从新消息批次中识别是否存在发给助手的任务，并提取任务原始内容。
- 为每个任务维护状态、目标、输入、运行引用和最终交付物索引。
- 根据任务状态启动、停止或恢复对应的单任务 Agent。
- 将需要用户可见回复的事实、问题和结果转换为 `ChatReplyRequest`，委托 Chat Agent
  组织语言并发送。

任务数量不设业务上限。运行并发可以通过执行器保护模型、Skill 和宿主资源，但不能
把并发保护误写成任务数量限制。任务操作必须校验 `scope_id`；群内默认按群共享任务，
但不能操作其他群或其他私聊用户的任务。

Task Manager Agent 不直接调用 NapCat，也不直接向群成员发送消息。它可以通过受限的
群消息查找 Skill 读取消息归档，但只读取游标之后的有界消息批次，不读取无限完整历史。
任务生命周期的确定性状态迁移由代码和状态机负责，任务识别与复杂编排由 Task Manager
Prompt 驱动。Task Manager 是任务相关消息的唯一解释者；Chat Agent 不参与任务判断。

### 2.5 单任务 Agent

单任务 Agent 是 Task Manager Agent 为一个目标创建的异步执行单元：

- 每个任务拥有独立的 `task_id`、目标、状态、上下文和交付物列表。
- 任务属于群作用域，默认不绑定创建者；群成员通过群消息补充或调整任务，由 Task
  Manager 从消息归档中解释。
- 单任务 Agent 只接收一个目标，不负责拆分或管理其他任务。
- 单任务 Agent 接收 Task Manager 提取的原始相关消息、结构化目标、约束和交付要求。
- 单任务 Agent 需要更多信息时，只能向 Task Manager 产生结构化的 `waiting_for_input`
  事件。Task Manager 决定是否需要向群友追问，并通过 `ChatReplyRequest` 委托 Chat
  Agent 回复；群友的后续消息仍由 Task Manager 从消息归档中主动获取。
- 单任务 Agent 不能读取 Chat Session、群消息窗口或其他任务的上下文。
- 单任务 Agent 维护一份当前任务的简要 `TaskDescription`，至少包含涉及人员、任务目标
  和执行结果，并通过结构化工具提交给 Task Manager 读取。
- 单任务 Agent 可以通过受限的 `task-delivery` Skill 向当前任务作用域交付已验证的
  文本、图片和附件；不能向其他作用域或其他业务 Agent 发送任意消息。

Task Agent 向 Task Manager 报告进度、阻塞和结果；Task Manager 需要用户可见回复时
只能通过 `ChatReplyRequest` 委托 Chat Agent。Task Agent 的任务结果交付属于受限的
NapCat 发送副作用，必须经过 Artifact、作用域、任务 revision 和幂等校验。Task Manager
与 Task Agent 之间的调度属于 Registry/Runtime 的结构化控制调用，不属于 Agent 间的
自然语言通信。

### 2.6 Task Description

`TaskDescription` 是 Task Agent 维护、Task Manager 可读取的任务说明投影，用于快速
理解任务“谁参与、要做什么、做到什么结果”。它不是任务状态的替代品，也不是完整事件
历史：

```text
TaskDescription
  task_id
  revision
  involved_people
    participant_ref
    role
    involvement
  goal
  execution_result
    status: not_started | in_progress | waiting_for_input | completed | partially_completed | failed
    summary
    artifact_refs
    delivery_message_ids
  updated_at
```

维护规则：

- `involved_people` 只记录从任务输入、群消息或工具结果中确认的参与者引用、角色和参与
  方式；不确定的人员不能猜测，必要时标记为待确认。
- `goal` 是当前 `revision` 的简短目标，不替代 Task Registry 中的结构化目标和约束。
- `execution_result` 记录当前执行结果的简短摘要、结果状态和已验证交付物引用；结果
  未确认前不能写成完成。
- Task Agent 在开始执行、关键进展、等待输入、交付或失败后更新说明。更新通过
  `update_task_description` 产生结构化事件，不能直接修改 Task Registry 的状态字段。
- Runtime 校验 `task_id + revision`、字段长度、Artifact 引用和作用域；过期 revision
  的说明只能记录为过期事件，不能覆盖当前说明。
- Task Manager 读取最新说明，将其放入 `active_task_index`、`focused_task_contexts`
  和任务查询结果；它可以据此组织 `ChatReplyRequest`，但必须以权威状态和事件为准。

## 3. 消息接入与触发

### 3.1 NapCat 适配器

建议拆分为以下责任：

```text
NapCat event
    -> NapCatMessageAdapter
    -> InboundMessage
    -> MessageIngress
    -> MessageArchive
    -> ScopeMessageCoordinator
       -> MessageRouter -> MessageDispatchRecord
            |-> CHAT -> Chat Worker -> ChatPendingWindow
            |-> TASK -> Task Worker -> Task Manager / Task Agent
            `-> BACKGROUND -> PreSessionWindow
```

`NapCatMessageAdapter` 负责：

- 校验事件类型和必要字段。
- 提取有序消息段，保留文本、@、回复和媒体引用。
- 识别群聊、私聊、机器人自身消息和重复事件。
- 将 NapCat 时间、用户和消息标识转换为领域字段。
- 对无法解析的消息返回明确的适配错误并记录关联 ID。

适配器不负责判断用户意图，不负责调用模型，也不负责发送回复。

`MessageArchive` 是原始消息的唯一写入点，`ScopeMessageCoordinator` 是同一作用域内
唯一的路由调度点：

- 一条 NapCat 原始消息只写入一次归档，并由 `message_id` 去重。
- 每个 `scope_id` 只有一个持久化归档游标。Router 串行读取归档，并通过条件更新一次性
  认领每条消息的 `route`；Chat Worker 和 Task Worker 不再直接读取原始归档。
- Router 只负责消息归属和批次边界，语义不明确的唤醒消息交给一次 Task Manager
  分类；分类结果中的普通消息进入 Chat Worker，任务消息进入 Task Worker。
- 在任务成功创建前不得生成任务开始确认。普通聊天唤醒只能进入 ChatPendingWindow，
  不得创建伪造的 `STARTED` 回复请求。
- Task Manager 判断为任务后，消息的 `route` 只能由 Router 的条件更新确认；已经被
  其他 Worker 认领的消息不能被覆盖。
- 普通聊天进入 ChatSession 后，群聊首次收到有效消息时自动创建 Session；Session 激活
  后普通消息无需再次唤醒。进入 `quiet` 后，仍需唤醒信号才能恢复参与。
- Task Manager 产生的 `ChatReplyRequest` 是内部回复请求，只进入 ChatPendingWindow，
  不写回群消息归档，也不再次进入 MessageRouter。它可以在 Session 过期后单独唤醒
  一个仅用于发送该任务回复的 Chat Agent 运行，但不会恢复完整聊天上下文。
- Chat Agent 正在处理批次时，新的普通聊天消息只追加到 `ChatPendingWindow`；当前运行
  完成后由 Router 一次性提交待处理窗口。Chat Worker 和 Task Worker 只消费已认领路由。
- 每条消息都有 `message_id + route` 的派发记录；重试只恢复未完成路由，不重复执行已
  成功的回复或任务操作。任务的 `source_message_ids` 持久化在任务实体中，不能只保存在
  Runtime 内存。

Coordinator 为每条原始消息维护一条派发记录：

```text
MessageDispatchRecord
  message_id
  sequence
  route: pending | task | chat | background | waiting | failed
  intake_stage: pending | running | completed | failed | skipped
  chat_stage: not_applicable | pending | queued | running | completed | failed
  chat_batch_id: optional
  chat_run_id: optional
  task_run_id: optional

Router 游标：

```text
ScopeCursor
  scope_id
  sequence
```

Router 更新 `route` 和 `intake_stage`；Chat Worker 只更新 `chat_stage` 和 Chat 批次
信息；Task Worker 只更新任务运行信息。所有更新都必须带上预期状态，避免并发覆盖。
```

只有 Router 才能最终设置 `route` 并派发任务链路。Task Manager 超时或失败时，未确认的
候选消息统一降级为普通聊天，任务状态不会因这条回应而改变。每个 `scope_id` 使用一个
串行 Router 队列；Chat Worker 和 Task Worker 可以在不同作用域及不同已认领路由上并行。

### 3.2 触发规则

触发配置不写死具体名称。`assistant.name` 同时作为群聊唤醒前缀，避免部署时为同一个
概念维护两份环境变量；内部仍保留 `ActivationConfig.prefixes` 这一领域字段，便于路由
逻辑保持独立。配置模型如下：

```toml
[activation]
mention_required = false
pre_session_window_seconds = 15
pre_session_max_messages = 12
session_ttl_seconds = 900
```

具体助手名称由部署环境提供，不能在代码中固定为某个中文名称。

群消息进入后的路由规则：

1. 机器人自身消息直接忽略。
2. 每条有效消息先写入 `MessageArchive`，并进入 Task Manager 的摄取游标；群消息同时
   进入 `PreSessionWindow` 或当前作用域的窗口状态。
3. Task Manager 负责最终解释消息。明确 @机器人或私聊消息在任务摄取期间可先进入
   ChatPendingWindow，由 Chat Agent 快速确认或回应；任务消息随后仍进入任务链路，任务
   Agent 由 Runtime 异步启动，不阻塞消息接收或后续摄取。
4. Task Manager 判定为普通消息后，若 Session 已激活，消息进入 `ChatPendingWindow`；
   若 Chat Agent 空闲，则立即刷新当前窗口，否则等待当前 Chat Agent 运行结束。
5. 群聊 Session 未激活或已过期时，普通消息会自动创建 Session 并进入
   `ChatPendingWindow`；进入 `quiet` 后普通消息仍保持安静，只有 @机器人或命中特殊
   前缀的消息可以重新激活。私聊仍由首条有效消息激活。
6. Chat Agent 忙时，普通聊天消息只追加到 `ChatPendingWindow`，不逐条调用 Chat Agent。
   当前运行完成后，Coordinator 按消息顺序把窗口全部作为一个 `ChatInputBatch` 交给
   Chat Agent；窗口上限通过时间、消息数和字符预算共同控制。
7. `ChatReplyRequest` 走同一 ChatPendingWindow，但标记为高优先级内部回复；任务回复
   不需要等待新的群消息唤醒。发送完成后只按实际成功发送结果续期 Session。
8. 窗口接收、任务分类和聊天处理是三个有序阶段。MessageIngress 和窗口压缩不能阻塞
   NapCat 接收；任务摄取与 Chat Agent 可以并行，不同作用域之间也可以并行。
9. 任务失败只产生事实性失败通知，不自动恢复或重跑；只有用户明确要求重试或继续时，
   Task Manager 才能将任务从 `failed` 放回 `queued` 并重新调度。

Chat Session 的是否回复由 Chat Agent 判断；消息是否是发给助手的任务由 Task Manager
Prompt 判断。两者都不通过针对单条样本的关键词列表硬编码。

## 4. Chat Agent

### 4.1 职责

Chat Agent 是普通聊天和用户可见表达的智能体。它负责：

- 按版本化 System Prompt 中的人格与行为边界进行自然对话。
- 判断消息是否与助手相关，并决定回复、追问或保持安静。
- 接收 Task Manager 的 `ChatReplyRequest`，把其中已经确认的事实和回复意图组织成
  自然语言。
- 默认使用群消息查找 Skill 获取必要的群上下文。
- 默认使用 NapCat 回复 Skill 向群友发送消息。

Chat Agent 的目标是与群友进行流畅、自然、简短且符合人设的对话。它不应随意接入
与助手无关的群聊，也不应为了显得活跃而回复每条消息。

Chat Agent 不负责任务识别、任务委托、任务查询、长时间执行代码、搜索或生成文件，也
不负责拆解任务目标、选择执行器、创建多个任务或维护任务状态。Task Manager 独立从消息
归档获取消息并处理任务；所有任务相关的用户回复都由 Task Manager 生成
`ChatReplyRequest` 后委托 Chat Agent。

### 4.2 Chat Session 内的处理

同一作用域的消息先由 Task Manager 串行分类；只有普通消息和内部
`ChatReplyRequest` 才进入 Chat Agent 的 `ChatPendingWindow`。Chat Agent 的输入单位是
`ChatInputBatch`，不是单条消息：

```text
ChatInputBatch
  scope_id
  session_id: optional
  wake_reason: normal | task_reply | none
  messages
  reply_requests
  window_summary
  first_sequence
  last_sequence
  window_truncated
```

处理规则：

- Chat Agent 空闲且窗口非空时，Coordinator 将当前窗口原子地封存为一个
  `ChatInputBatch`，再启动一次 Chat Agent 运行。
- Chat Agent 运行期间到达的普通消息和回复委托继续追加到新的待处理窗口，不打断当前
  运行，也不逐条启动新模型请求。
- 当前运行成功、忽略或失败后，Coordinator 按序检查待处理窗口；窗口非空时再次整体
  提交。任务摄取和单任务 Agent 不等待 Chat Agent 变为空闲。
- `ChatReplyRequest` 可以在窗口中标记为高优先级，但仍必须经过 Chat Agent 的人格表达
  和回复 Skill；不能由 Task Manager 直接发送。Session 已过期时，任务回复可以创建
  `wake_reason=task_reply` 的临时批次，但不会把普通消息从 `PreSessionWindow` 一并带入。
- 受时间、消息数量或字符预算限制时，窗口压缩最早的原文并保留摘要和消息引用，
  `window_truncated=true`；不能静默丢弃已归档消息。

必须保证：

- 同一条消息不会重复处理。
- Chat Agent 和 Task Manager 不会同时对同一条原始消息启动模型解释。
- 过期的模型结果不能覆盖更新后的 Session。
- Chat Agent 回复和 Task Manager 的委托请求不会造成自身消息再次触发；委托请求是
  独立的内部输入，不参与原始消息路由。
- 任务完成、失败、等待补充或需要澄清时，Task Manager 发送一次性
  `ChatReplyRequest`；Chat Agent 只负责把请求组织成回复，不读取任务状态。
- Chat Agent 不负责转交任务；任务模块从消息归档按游标读取原始消息。
- Task Manager 需要更多信息时，负责在 `ChatReplyRequest` 中给出追问意图；Chat Agent
  负责表达，Task Manager 再从后续消息中自行获取补充内容。

Chat Agent 不使用一次性 `ChatDecision` JSON。它通过 Agent Loop 工具调用推进：

```text
ChatInputBatch -> send_napcat_reply -> chat_turn_complete
无需回复 -> chat_turn_complete(outcome="ignored")
发送失败 -> ToolResult -> 重试/调整 -> chat_turn_complete(outcome="failed")
```

一次 `ChatInputBatch` 可以产生零个、一个或多个有序回复，但所有回复都必须引用当前
批次或 `ChatReplyRequest` 的幂等键。最终是否续期不由模型决定：只有 NapCat 回复 Skill
成功发送面向群友的回复后，Session 才续期。

交互首响和渠道长度由 Runtime 统一保证：

- `create_task` 成功后自动创建一次 `ReplyType.STARTED` 的 `ChatReplyRequest`，由
  Chat Agent 发送简短的开始处理确认；Task Manager 结束本轮时必须确认返回的
  `started_reply_id`。
- Chat Agent 的聊天正文和 Task Agent 的任务交付正文超过 80 个字符时，Runtime 将正文
  渲染成 PNG 图片发送，避免长文本在群聊中造成阅读负担。引用、@ 和其他已验证媒体段
  仍按原协议发送。

### 4.3 Chat Agent 的默认工具

Chat Session 默认加载以下两个 Skill：

- 群消息查找 Skill：按群作用域、时间范围、消息 ID 或发送者查询已归档消息，结果以
  结构化消息返回给 Chat Agent。
- NapCat 回复 Skill：向指定群发送文本、引用、@和媒体消息。

Chat Agent 不加载任务管理 Skill，也不查询 Task Registry。任务相关的事实必须由
Task Manager 放入 `ChatReplyRequest`；Chat Agent 只负责语言表达和发送。

NapCat 回复 Skill 建议提供：

- 目标群作用域
- 回复文本或结构化消息段
- 引用消息和 @对象
- 幂等键
- 发送结果和 NapCat 错误

Chat Agent 不应自行拼接 NapCat API 请求。所有对群友的回复都通过 Skill 工具完成，
这样可以统一权限、格式、媒体和错误处理。

## 5. 任务管理与执行

### 5.1 任务主动获取协议

Task Manager 不等待 Chat Agent 转交任务。Coordinator 完成某条消息的任务分类阶段后，
由 `MessageRouter` 按作用域维护唯一消息游标，生成 `TaskIntakeBatch` 后交给已认领的
Task 路由：

```text
TaskIntakeBatch
  scope_id
  message_ids
  cursor_before
  cursor_after
  messages
```

MessageRouter 和 Task Worker 的职责：

- Router 按唯一游标读取有界消息批次，在同一作用域内串行完成一次路由认领。
- 通过 `message_id` 和 dispatch ledger 去重，保证重试不会重复创建任务或追加聊天窗口。
- Router 只把尚未归属的唤醒候选提交给 Task Manager；普通聊天直接生成 `CHAT` 路由。
- Task Worker 只消费已经标记为 `TASK` 的 dispatch，不重新扫描消息归档。
- Chat Worker 忙、等待刷新、超时或失败时，任务路由仍可独立执行；两个 Worker 不共享原始
  消息游标。

Task Manager Prompt 收到 `TaskIntakeBatch` 后，自己判断哪些消息是群友明确发给助手的
任务，哪些只是普通群聊或给其他人的对话。它不依赖 Chat Prompt，也不需要 Chat Agent
先改写、筛选或转交消息。

Task Manager 识别出任务后，Router 将对应消息原子切换为 `TASK`，再生成内部使用的
`TaskHandoff`：

```text
TaskHandoff
  task_id
  revision
  scope_id
  source_messages
  involved_people
  goal
  constraints
  requested_deliverables
  allowed_skills
```

任务识别、任务拆解、任务优先级、任务之间的依赖和执行 Skill 选择全部属于 Task Manager
Agent，不属于 Chat Agent。

### 5.2 Task Manager Agent

Task Manager Agent 接收任务读取队列提交的消息批次；不接收 Chat Agent 的任务命令，
也不依赖 Chat Agent 识别任务。一个作用域内 Task Manager 的消息摄取、任务编排和
单任务 Agent 调度必须与 Chat Agent 的运行状态解耦：

```text
TaskManagerInput
  input: TaskIntakeBatch
```

当 Chat Agent 忙时，Task Manager 仍可以完成以下动作：

- 继续从归档游标读取后续消息并按批次解释。
- 创建、暂停、恢复、删除或调整任务。
- 异步启动或停止单任务 Agent。
- 生成 `ChatReplyRequest` 并把它放入 `ChatPendingWindow`，等待 Chat Agent 空闲后表达。

这些动作不要求当前 Chat Session 有可用的自然聊天输入；任务事件也不应因为聊天窗口
拥塞而丢失。若任务事件需要及时通知，系统可以唤醒一个只承载该
`ChatReplyRequest` 的 Chat Agent 运行，但仍不绕过 Chat Agent 的发送边界。

Task Manager 需要用户可见回复时，向 Chat Agent 发送独立的结构化委托：

```text
ChatReplyRequest
  scope_id
  source_message_ids
  reply_type: progress | clarification | started | completed | failed | paused | resumed
  reply_intent
  verified_facts
  mention_user_id: optional
  reply_idempotency_key
```

`ChatReplyRequest` 是语言表达委托，不是任务控制命令。它必须携带已经由 Task Manager
确认过的事实；Chat Agent 不得自行查询、推断或修改任务状态。对用户而言，任务相关回复
仍然来自同一个配置人格的助手，不能暴露内部 Agent 的分工。

Task Manager Agent 维护内部的 `TaskStatusSnapshot`，并以此决定是否生成
`ChatReplyRequest`：

```text
TaskStatusSnapshot
  task_id
  revision
  status
  task_description
  progress_summary
  waiting_reason: optional
  pending_update: optional
  artifact_refs
  updated_at
```

它负责：

- 从原始消息中判断创建、补充、暂停、恢复、删除和查询意图。
- 创建和登记多个彼此独立的单一任务。
- 把用户后续补充信息追加到指定任务。
- 判断一个消息是否应拆分为多个单一任务，并维护任务之间的依赖。
- 为任务创建或停止单任务 Agent。
- 根据新消息判断任务状态查询，并通过 `ChatReplyRequest` 委托 Chat Agent 回复进度、
  等待原因和最终交付物索引。
- 校验任务状态迁移和最终交付物索引。
- 把需要用户可见的任务事件转换为 `ChatReplyRequest` 发送给 Chat Agent。

Task Manager Agent 不直接向用户解释任务，不直接使用 NapCat 回复 Skill。它只提供
`ChatReplyRequest`，由 Chat Agent 负责自然语言表达和发送。Chat Agent 不能绕过
Task Manager Agent 直接读取 Task Registry。

### 5.3 任务补充与调整

群友的连续补充和任务调整都会作为新消息进入消息归档，由 MessageRouter 按唯一游标路由，
再由 Task Worker 按已认领的任务记录提交给 Task Manager。Chat Agent 不合并、改写或
直接覆盖任务目标。Task Manager
按作用域、消息引用、当前任务卡片和原始消息判断消息归属；无法确定归属时，通过
`ChatReplyRequest(reply_type=clarification)` 请求 Chat Agent 向群友澄清。群友的纠正、
补充或对澄清问题的回答都会重新进入 MessageRouter，由 Task Manager 决定是否递增
revision、暂停当前运行或重新编排任务。

每个任务采用递增的 `revision`：

- `task_id` 标识同一个长期任务，`revision` 标识该任务的一版目标和约束。
- 所有补充、调整、暂停和恢复事件按顺序追加，不能删除历史事件。
- 只补充事实、文件或上下文时，Task Manager 可以把消息合并到当前 revision，并在
  单任务 Agent 的安全检查点注入。
- 修改目标、约束、交付格式或范围时，Task Manager 必须递增 revision；旧运行立即标记
  为过期，必要时取消并从最新 revision 重新执行。
- 任务完成后收到明确的调整时，默认创建同一 `task_id` 的新 revision，保留旧版本结果；
  完全无关的新目标才创建新的 `task_id`。
- 短时间连续补充先在 Task Manager 的输入队列中按顺序合并，再触发一次调度，避免每条
  消息都重复启动单任务 Agent；这不新增面向用户的配置项。

单任务 Agent 的每次运行都绑定 `task_id + revision`。只有与当前 revision 完全一致的
进度、状态和交付物可以被接受；旧 revision 的迟到结果只能记录为过期事件，不能发送给
群友，也不能覆盖当前任务状态。

### 5.4 Task Manager 上下文

Task Manager Agent 不依赖模型上一轮请求留下的隐式记忆。每次处理新消息或任务事件时，
都从 Message Archive、Task Registry、Task Event Ledger 和当前输入重建
`TaskManagerContext`：

```text
TaskManagerContext
  scope_id
  input_sequence
  intake_batch
  active_task_index
  focused_task_contexts
  pending_questions
  recent_task_events
```

上下文按“索引 + 聚焦任务”组织：

- `active_task_index` 只包含群内任务的轻量索引：`task_id`、当前 `revision`、状态、最后
  更新时间和 `TaskDescription` 的简短摘要。
- `focused_task_contexts` 只展开与当前消息可能相关的任务，包含当前目标、约束、交付
  要求、依赖、待处理补充、最近事件、`TaskDescription` 和交付物引用。
- 已完成、已删除或长期不活跃的任务只保留归档索引；用户明确查询时再加载其摘要。
- 完整群聊历史不属于 Task Manager Context。任务模块只读取游标批次中的原始消息、按
  Skill 查询到的必要消息和任务自身的事件记录，不读取 Chat Agent 的上下文。

每条补充或调整消息按以下顺序处理：

1. 先以原文追加到 Message Archive 和 Intake Ledger，并分配递增的 `input_sequence`。
2. 按群作用域串行重建上下文；连续输入在队列中按顺序处理，不能并发修改同一任务。
3. Task Manager Prompt 基于消息批次、聚焦任务和任务索引生成管理决策。
4. Task Manager Service 校验决策，更新权威状态、revision、依赖和任务事件。
5. 生成新的任务摘要和状态快照，供下一次请求使用。

任务摘要是上下文压缩结果，不是权威状态。压缩必须保留当前目标、当前 revision、有效
约束、已确认决策、未解决问题、最近错误和最终交付物引用；原始事件仍保存在 Ledger 中，
不能因为摘要覆盖而丢失。压缩失败时使用结构化任务字段和最近事件尾部，不把全部历史
直接发送给模型，也不能覆盖 Registry 中的真实状态。

Task Manager 模型请求必须携带 `input_sequence`。如果处理期间收到更新，旧决策提交时
需要校验序号；序号过期的决策直接丢弃并基于最新上下文重试，避免旧模型结果覆盖群友
刚刚提交的补充。

Task Registry、Task Event Ledger、任务 revision、Session 元数据、消息派发记录和有界
群消息窗口以及每个任务的最新 `TaskDescription` 投影持久化在
`plugins/pumpkin/data/pumpkin.db`。Chat Session 的模型请求临时锁
和当前进行中的模型调用属于进程运行态；进程重启后不恢复正在执行的模型调用，但会从
归档和未完成派发记录重建未消费的 `ChatInputBatch`。仍在 TTL 内的 Session 可以恢复为
可接收状态；已过期的普通消息回到 `PreSessionWindow`，等待下一次唤醒。任务则从
Registry 和 Ledger 恢复为可查询状态，并由 Task Manager 决定是否重新调度。数据库只保存
有界窗口、任务必要原文和结构化摘要，不保存无限群聊历史。

### 5.5 单任务 Agent

每个单任务 Agent 只处理一个 `TaskHandoff`，以完成目标并交付结果为终点。推荐状态：

```text
queued -> running -> waiting_for_input -> running
                 -> completed
                 -> failed
                 -> paused
                 -> deleted
```

单任务 Agent 负责：

- 理解 Task Manager Agent 下发的目标、约束和验收条件。
- 使用 Task Manager 已授权、并在当前运行中按需加载的代码执行、网络搜索、图片生成等
  Skills。
- 收集工具证据，区分中间产物和最终交付物。
- 发现关键输入缺失时向 Task Manager 发出 `waiting_for_input`；是否追问和如何追问由
  Task Manager 决定。
- 在交付前验证最终交付物可读取、格式正确且满足目标。

任务状态迁移由 Task Manager Agent 和 Task Registry 负责，不由模型直接修改。任务完成
必须经过交付物校验。Task Agent 可以通过受限的 `task-delivery` Skill 直接向当前任务
作用域交付已验证的文本、图片和附件；中间文件、调试输出和未验证的 Artifact 不能发送。

### 5.6 任务侧通信边界

任务侧 Agent 之间只能通过结构化协议通信：

- Task Agent 向 Task Manager 报告进度、阻塞、产物和结果。
- Task Manager 只能通过 `ChatReplyRequest` 委托 Chat Agent 组织用户可见回复。
- Task Agent 可以通过 `task-delivery` Skill 直接向当前任务作用域发送已验证的任务结果；
  Task Manager 不能直接向群成员发送消息。
- Task Agent 不能使用通用 `napcat-reply` Skill，也不能向其他作用域发送消息。
- Task Agent 不读取 Chat Session 或群消息滚动窗口；Task Manager 通过消息查找 Skill
  获取任务输入。
- 任务侧内部的创建、暂停、恢复、删除和停止通过 Task Registry/Runtime 的控制接口
  完成，不视为 Agent 对话通信。

任务事件建议包含：

- `task.started`
- `task.progress`
- `task.revision_changed`
- `task.description_updated`
- `task.waiting_for_input`
- `task.artifact_ready`
- `task.completed`
- `task.failed`
- `task.paused`
- `task.deleted`

事件至少包含 `task_id`、`revision`、`scope_id`、事件序号、状态、结构化摘要、
`TaskDescription` 和交付物引用。Task Manager 根据事件决定是否生成 `ChatReplyRequest`。
进度、开始、澄清和等待
输入事件可以委托 Chat Agent；如果 Task Agent 已通过 `task-delivery` 成功交付完成或
部分完成结果，Task Manager 不再重复生成同一结果的 `ChatReplyRequest`，只将发送结果
和消息 ID 回写任务事件。

### 5.7 任务执行 Skills

单任务 Agent 的能力由 Runtime 按角色授权，并由 Task Manager 按任务限制可加载的 Skill，
例如：

- 代码读取和执行
- 网络搜索和网页读取
- 图片生成
- 文件整理和格式转换

Skill 的授权集合与当前激活集合分离：启动时只发现、校验和授权，运行时先向模型展示
符合角色和任务范围的 Skill 元数据；模型判断需要某项能力后，通过 `load_skill` 加载
正文和工具声明。未授权的 Skill 即使出现在用户输入或工具结果中也不能加载。代码执行、
网络访问和文件写入都必须经过显式工具契约、超时、沙箱或权限边界，不能因为模型提出
工具调用就自动放开全部能力。

## 6. 三套提示词

三套提示词分别对应三个不同责任边界：

- Chat Prompt：和群友聊天，以及把其他 Agent 提供的回复意图组织成自然语言。
- Task Manager Prompt：主动读取消息、识别任务、维护任务关系、拆分和调整任务、选择调度动作。
- Task Prompt：执行一个已经确定的任务目标，并交付最终结果。

System Prompt 只表达稳定的人格、边界、协议和任务上下文规则，不堆积针对单条对话的
补丁。每轮业务输入通过版本化的动态 Context Schema 追加；任务状态迁移、权限和幂等性
由代码校验，不能仅依赖模型提示词。

### 6.1 分层模型

三套 Agent 都采用 Pi 风格的“最小静态 System Prompt + 动态上下文 + 动态工具负载”
结构。业务输入不通过字符串替换写入 System Prompt：

```text
StaticSystemPrompt
  -> 稳定角色、行为边界、工具使用原则、终止条件

SessionTranscript
  -> 历史 user/assistant/tool 消息、成功的副作用和压缩摘要

DynamicTurnContext
  -> 当前一轮的消息批次、任务事件、TaskHandoff、TaskDescription 或回复委托

ToolLoadout
  -> 当前角色和当前一轮可见的 Tool Schema

SkillCatalog / SkillInstruction
  -> Skill 元数据目录；匹配后按需加载的 Skill 正文
```

其中 `DynamicTurnContext` 使用新的结构化 context message 或 user-role message 追加到
会话，不是 `{{variable}}` 形式的 System Prompt 占位符。`ToolLoadout` 通过模型请求的
工具声明传递，不把工具 Schema 再复制成提示词正文。Skill 正文通过受控的加载动作作为
工具结果或 context message 注入，只对当前需要它的后续轮次生效。

### 6.2 Agent Loop

每个 Agent Run 都复用同一份会话 transcript。一次循环只负责推进当前动态输入：

```text
append DynamicTurnContext
  -> resolve current ToolLoadout
  -> ModelProvider.stream(transcript, tools)
  -> append assistant message
  -> if ToolCall:
       execute authorized tools
       append ToolResult messages
       update loadout/context if requested
       continue
  -> if terminal tool succeeds:
       persist outcome
       settle run
  -> otherwise:
       treat text as intermediate output and continue or request correction
```

每轮必须遵循：

1. Runtime 先追加本轮动态输入，再构建当前工具负载；运行中的 Agent 不修改已经提交的
   当前输入。
2. 模型只能调用当前 ToolLoadout 中的工具。工具执行后，原始调用和结构化
   `ToolResult` 都追加到 transcript，下一轮以结果为事实来源。
3. Skill 加载、工具增删、待处理消息和外部任务事件都在轮次边界生效；不会通过修改
   固定 System Prompt 的占位文本来改变当前回合。
4. 工具失败、超时、截断和权限拒绝都必须作为 `ToolResult` 回灌；模型可以修正、替代、
   降级或进入等待，但不能把失败描述成成功。
5. 群消息或外部事件在 Agent 忙时进入 Runtime 队列；当前工具完成后作为下一条
   `DynamicTurnContext` 追加，不打断正在执行的副作用工具。
6. 三个 Agent 都使用显式终止工具，因为发送、任务变更和完成状态都有副作用。普通文本
   不能代替终止工具，也不能证明消息已发送或任务已完成。

| Agent | 静态 System Prompt 的职责 | 动态每轮输入 | 终止工具 |
| --- | --- | --- | --- |
| Chat Agent | 人格表达、普通聊天边界、已确认回复素材的表达规则 | 助手身份/人格、`ChatInputBatch`、`ChatReplyRequest[]`、上一轮工具结果 | `chat_turn_complete` |
| Task Manager Agent | 任务识别、任务编排、事实优先级和状态查询规则 | `TaskIntakeBatch`、任务索引、聚焦任务、`TaskDescription`、最近事件 | `task_manager_turn_complete` |
| Task Agent | 单目标执行、证据判断、任务说明维护和结果交付规则 | `TaskHandoff`、`TaskDescription`、任务事件、用户补充和上一轮工具结果 | `task_turn_complete` / `complete_task` |

### 6.3 Chat Agent System Prompt

这是稳定的职责与协议 Prompt，不包含当前消息、`ChatInputBatch`、`ChatReplyRequest`
或 Skill 正文。女友式人格由独立的 `CHAT_PERSONA` 常量作为单独 system message 注入，
不通过环境变量覆盖：

```text
你的职责是处理 Runtime 已经路由给你的普通聊天输入，或表达 Task Manager 已经确认的
用户可见回复。你使用当前独立人格 Prompt 中的表达规则，但不负责任务识别、任务查询、
任务编排或长时间任务执行。

每轮先阅读动态上下文和上一轮 ToolResult，再决定是否需要回复。需要发送时调用当前
ToolLoadout 中的回复工具，并等待发送结果；发送失败不能宣称已送达。一次聊天轮次可以
连续发送多条消息；确有必要分段时按自然顺序多次调用回复工具，每条消息保持简短，全部
发送成功后再调用 chat_turn_complete，并完整提交本轮成功发送的消息 ID。

只使用动态上下文中已经确认的任务事实。不要补充不存在的状态、进度、结果或承诺；
不要重新判断消息是否属于任务；不要向用户暴露内部 Agent、Skill 或工具协作。
```

**动态输入**

```text
ChatTurnContext
  assistant_identity
  recent_outbound_messages
  chat_input_batch
  reply_requests
  skill_catalog
  external_events
```

`chat_input_batch` 和 `reply_requests` 每次作为新的 `ChatTurnContext` 追加；它们不是
职责 Prompt 的变量。`assistant_identity` 是结构化的助手角色、名称和账号标识；
`recent_outbound_messages` 只包含发送工具成功确认后的消息，不把 NapCat 回显重新当作
用户输入。助手身份由动态上下文注入，人格由独立的 `CHAT_PERSONA` 常量注入。

### 6.4 Task Manager Agent System Prompt

这是稳定的 System Prompt，不包含当前消息批次、任务索引、TaskDescription 或 Skill 正文：

```text
你的职责是从当前动态输入中识别和维护异步任务，选择最小必要的任务操作，并协调单任务
Agent。默认不拆分任务；只有独立目标、不同生命周期、明确依赖或权限隔离确实必要时才
拆分。条件充分时直接执行，只有缺少完成目标所必需的信息时才请求澄清。

每轮先读取动态输入、当前任务上下文和上一轮 ToolResult。任务状态、revision、权限、
依赖和幂等性由工具与 Runtime 校验；你不能凭计划声称操作成功。需要用户可见的开始、
进度、澄清、等待或状态说明时提交 ChatReplyRequest，但不能直接发送 NapCat 消息。

当前批次没有更多任务动作时调用 task_manager_turn_complete。工具失败必须读取结果并
修正、替代、排队或请求必要信息；不能跳过失败结束为成功。
```

**动态输入**

```text
TaskManagerTurnContext
  assistant_identity
  recent_outbound_messages
  intake_batch
  active_task_index
  focused_task_contexts
  recent_task_events
  task_description_updates
  skill_catalog
  external_events
```

Task Manager 通过动态上下文读取最新 `TaskDescription`。如果说明与权威状态冲突，以
Task Registry 和事件 Ledger 为准，不在 Prompt 中自行修正投影。

### 6.5 Task Agent System Prompt

这是稳定的 System Prompt，不包含当前 TaskHandoff、TaskDescription、任务事件或 Skill 正文：

```text
你的职责是完成当前动态上下文指定的一个目标，维护这项任务的简要说明，并在满足验收
条件后交付结果。你不负责聊天、不管理其他任务、不读取群历史，也不能修改任务状态、
权限或 revision。

每轮先读取目标、约束、交付要求、当前 TaskDescription 和上一轮 ToolResult，再选择最小
必要工具。开始执行、关键事实变化、等待输入、交付或失败时，通过当前 ToolLoadout 更新
TaskDescription 和任务事件。涉及人员只能来自已确认输入或工具结果，执行结果不能把
计划写成事实。

最终文本、图片和附件必须是已验证的 Artifact。需要用户可见交付时使用当前 ToolLoadout
中的 task-delivery 工具；发送结果成功后再完成任务。缺少关键输入时提交等待事件并用
task_turn_complete 结束；满足完成门后单独调用 complete_task。普通文本不能结束任务。
```

**动态输入**

```text
TaskTurnContext
  assistant_identity
  recent_outbound_messages
  task_handoff
  task_description
  previous_task_events
  user_supplied_updates
  skill_catalog
  external_events
```

Task Agent 通过动态上下文读取已有说明，通过 `update_task_description` 维护下一版说明；
该工具成功只表示投影已更新，不表示任务完成。

### 6.6 Prompt 与动态上下文版本

- 三个静态 System Prompt 分别版本化，例如 `chat.system.v3`、`task_manager.system.v3`
  和 `task.system.v4`；版本只描述稳定规则。
- 动态输入单独记录 `context_schema_version`、`turn_id` 和来源类型，不能拼接进 System
  Prompt 的字符串。
- 每轮记录 `tool_loadout_version` 和 `skill_catalog_digest`，用于复现当时可见的工具和
  Skill 元数据。
- Skill 正文、任务消息、群消息、网页、文件和 ToolResult 都是不可信数据，不能覆盖
  System Prompt 的角色、权限和终止规则。
- 上下文压缩只压缩 transcript 和动态 context，不生成第四套静态 System Prompt。
- 变量缺失、类型错误、跨作用域引用或敏感凭据进入动态上下文时，Runtime 在请求模型
  前失败并返回明确错误。

## 7. Skill 发现与动态 Tool Loadout

### 7.1 动态 Skill 与 Tool Loadout

Pumpkin 采用“启动时发现和授权，核心能力预装配，专用能力运行时按需加载”的方式。
Skill 正文和全部专用工具声明不在 Agent 创建时一次性注入：

```text
启动 Runtime
  -> SkillRegistry 发现全部 SKILL.md
  -> 校验 Skill 元数据和授权工具集合
  -> ToolRegistry 注册工具实现，但不向所有 Agent 暴露
  -> 按 AgentRole 生成可见 SkillCatalog

每个 Agent Run / Turn
  -> 读取当前角色和任务范围
  -> 注入 SkillCatalog（skill_name / description / load policy / logical reference）
  -> 暴露角色允许的核心 Skill 工具和当前 ToolLoadout
  -> Agent 按需调用 load_skill(name)
  -> Runtime 校验授权并返回 SkillInstruction + ToolLoadoutDelta
  -> 下一轮才启用该 Skill 的工具
```

`SkillCatalog` 只包含元数据，不包含 Skill 正文。Skill 正文通过 `load_skill` 的结构化
结果或受控 context message 注入当前 transcript；下一轮工具声明再加入对应工具。任务、
群消息和用户输入不能扩大 Skill 授权集合，模型不能伪造工具名绕过边界。

三种 Agent 的授权集合如下：

- Chat Agent：聊天背景、普通回复和媒体发送相关 Skill。
- Task Manager Agent：消息查找、任务状态/编排、TaskDescription 读取和 ChatReply 委托
  相关 Skill。
- Task Agent：当前任务允许的代码、搜索、图片、工作区、Artifact、TaskDescription 更新
  和任务交付相关 Skill；不包含 Chat Session、群历史和通用 `napcat-reply`。

本节参考仓库中 Niu 的历史实现：

- `plugins/niu/core/skills.py`：Skill frontmatter、目录发现、能力目录和工具绑定校验。
- `plugins/niu/core/tools.py`：Tool、ToolContext、ToolResult、超时、幂等和执行模式。
- `plugins/niu/core/runtime.py`：Agent Runtime 的工具分组、Skill 能力边界和完成门。
- `plugins/niu/skills/group-history/SKILL.md` 与 `plugins/niu/providers/group_history.py`：
  当前群限定、成员解析、游标查询和图片上限。
- `plugins/niu/skills/reply/SKILL.md`、`plugins/niu/core/reply.py` 和
  `plugins/niu/adapters/napcat_context.py`：回复、附件、@、引用和 NapCat 上下文处理。

开发阶段可以借鉴 Docker Mimi 中实际运行的 Niu 代码，但不能完全照搬其实现、职责边界
或运行时耦合；Pumpkin 必须以本设计定义的 Agent、Skill 和状态所有权为准。

### 7.2 Skill 文件契约

每个 Skill 使用独立目录和 `SKILL.md`，格式参考 Niu 的 Skill Registry：

```yaml
---
name: napcat-reply
description: 将结构化回复发送到当前 QQ 会话。
allowed-tools: send_napcat_reply send_napcat_artifact
agent-roles:
  - chat
load-policy: on-demand
triggers: []
examples:
  - 回复群友
anti-triggers:
  - 执行任务
priority: 100
---
```

字段约束：

- `name` 必须与目录名一致，使用小写连字符格式，全局唯一。
- `description` 是能力目录中的简短用途说明，不写临时业务规则。
- `allowed-tools` 是该 Skill 被加载后允许加入 Tool Loadout 的工具集合；缺失、重复或
  未知工具在启动时失败。
- `agent-roles` 是代码授权的角色白名单，只能使用 `chat`、`task-manager`、`task`，不是
  自动注入声明。
- `load-policy` 目前只能是 `on-demand` 或 `core`；`on-demand` 需要 Agent 显式加载，
  `core` 只表示该角色启动时可以看到基础工具，仍不自动注入 Skill 正文。
- `triggers`、`examples` 和 `anti-triggers` 只用于能力说明、测试和审计，不负责运行时
  关键词分支，也不触发动态安装。
- Skill 正文描述调用边界、输入输出、失败处理和安全要求；不能写入某个用户、某个群
  或某一次错误的临时补丁。
- `priority` 只影响能力目录展示顺序，不改变权限。

SkillRegistry 启动时必须验证：

1. YAML frontmatter 没有重复键、未知字段或错误类型。
2. Skill 名称、工具名称和角色名称符合格式约束。
3. Skill 正文非空且工具绑定完整。
4. 所有工具都有唯一实现、输入 Schema 和角色授权结果。
5. Skill 指令中引用的工具都存在，ToolRegistry 中的孤立工具必须显式标记为内部工具。

### 7.3 Agent 能力矩阵

矩阵描述授权和目录可见范围，不表示 Skill 正文或全部工具会自动进入模型上下文。

| Agent | 可见 SkillCatalog | 按需加载后的能力 | 明确禁止 |
| --- | --- | --- | --- |
| Chat Agent | `group-history`、`napcat-reply` | 查找聊天背景、组织自然语言、发送文本/引用/@/媒体 | 任务识别、任务状态读取、任务编排、代码、搜索、图片生成 |
| Task Manager Agent | `group-history`、`task-manager`、`chat-bridge` | 拉取消息、识别任务、读取 `TaskDescription`、读写任务状态、创建/暂停/恢复/删除 Task、委托 Chat 回复 | 代码执行、网络搜索、图片生成、直接 NapCat 发送 |
| Task Agent | 当前任务授权的 `code`、`web-search`、`image`、`html-render`、`audio`、`workspace`、`artifact`、`task-delivery` | 完成单一目标、获取证据、生成和验证交付物，维护说明并向当前任务作用域交付结果 | Chat Session、群历史、任务 Registry 写接口、Chat Agent、通用 NapCat 工具 |

`chat-bridge` 不是普通聊天工具，而是 Task Manager 到 Chat Agent 的内部结构化通信
协议；`task-manager` 的状态写入也必须经过 Service/Registry 的确定性校验。模型只能
提出结构化意图，不能直接修改运行时权限或数据库。

### 7.3.1 Agent Loop 生命周期与工具装载

以下工具由 Runtime 注册，不属于业务 Skill；它们用于动态装载能力、推进生命周期和明确
结束当前循环：

```text
chat_turn_complete
  outcome: replied | ignored | failed
  sent_message_ids: string[]
  reason: optional string

task_manager_turn_complete
  outcome: no_task | waiting_for_user | scheduled | updated | failed
  affected_task_ids: string[]
  submitted_reply_ids: string[]
  reason: optional string

task_turn_complete
  outcome: waiting_for_input | failed
  task_id: string
  revision: integer
  reason: string

load_skill
  name: string

update_task_description
  involved_people: PersonRef[]
  goal: string
  execution_result: TaskExecutionResult

emit_task_event
  event_type: started | progress | waiting_for_input | artifact_ready | failed
  task_id: string
  revision: integer
  summary: string
  waiting_reason: optional string
  artifacts: ArtifactRef[]
  evidence: EvidenceRef[]

complete_task
  status: completed | partially_completed
  summary: string
  remaining: string[]
  accepted_criteria: string[]
  delivery_message_ids: optional string[]
```

其中：

```text
PersonRef
  participant_ref
  role
  involvement

TaskExecutionResult
  status: not_started | in_progress | waiting_for_input | completed | partially_completed | failed
  summary
  artifact_refs
  delivery_message_ids
```

控制工具规则：

- `chat_turn_complete` 只能由 Chat Agent 调用，且 `replied` 必须引用本轮成功的
  `send_napcat_reply` 结果。
- `task_manager_turn_complete` 只能由 Task Manager 调用，且任务操作和
  `request_chat_reply` 必须先获得成功结果；只有 `outcome="no_task"` 才能让 Coordinator
  把原始消息路由给 Chat Agent。
- `task_turn_complete` 只能由 Task Agent 调用，只用于等待输入或失败；调用前必须已经
  成功提交对应的 TaskEvent。
- `load_skill` 只能加载当前 `SkillCatalog` 中且通过 AgentRole、任务范围和权限校验的
  Skill。成功结果包含 SkillInstruction 和 ToolLoadoutDelta，下一轮才启用新增工具；
  加载成功不能结束 Agent Loop，也不能扩大授权集合。
- ToolRegistry 在执行入口再次校验 AgentRole；即使模型或调用方绕过当前工具负载，
  也不能调用其他角色的工具。
- 任务创建必须显式提供非空的 `allowed_skills`，Task Agent 的运行时授权集合严格取自
  该字段；失败任务恢复必须绑定当前 intake 批次的 `confirmation_message_ids`，不由
  Runtime 解析自然语言关键词。
- `update_task_description` 只能由当前 Task Agent 调用；它只产生
  `task.description_updated` 事件和最新说明投影，不改变任务状态、revision 或权限。
  成功后 Agent 继续当前循环，不能把它当作终止工具。
- `emit_task_event` 只能向当前 Task Manager 发送事件；`task.completed` 不由它代替，
  必须经过 `complete_task` 完成门。
- `complete_task` 必须单独成轮调用，不能与其他工具调用混在同一批次；Runtime 根据
  ToolRegistry 证据、Artifact、发送结果和任务状态再次校验。
- 控制工具的成功结果才允许 Runtime 结束对应 Agent Loop；普通文本、计划或工具调用
  意图不能结束循环。

### 7.4 Tool 契约

ToolRegistry 的实现参考 Niu 的 `Tool`、`ToolContext` 和 `ToolResult`：

```text
Tool
  name
  description
  input_schema
  handler
  skill_name
  timeout
  read_only
  execution: auto | parallel | sequential | barrier
  idempotency_required
  completion_required
```

`ToolContext` 由程序创建，模型不能伪造：

```text
ToolContext
  scope_id
  sender_id
  message_id
  sender_name
  agent_role
  run_id
  reply_target
  input_media
```

Tool 执行规则：

- 参数先经过 JSON Schema 和业务边界校验，再进入 handler。
- 每个 Tool 有独立超时；超时、网络错误、参数错误和权限错误转换成稳定的错误码。
- 只读工具可以并行；写入、回复、任务状态修改和有顺序依赖的工具串行执行。
- `run_id + tool_use_id` 作为幂等键，重复调用返回同一结果，不能重复发消息或重复创建
  任务。
- 工具不得向模型返回 Token、Cookie、完整异常堆栈、宿主绝对路径或无界原始响应。
- Tool 结果使用统一结构：

```text
ToolResult
  content
  is_error
  error_code
  retryable
  artifacts
  images
  mention_sender
  suppress_final_text
```

工具成功只表示取得了结果、证据或 Artifact，不自动表示任务完成；Task Agent 必须通过
任务完成校验，并在需要时先完成任务交付。Task Manager 可以根据完成事件决定是否追加
`ChatReplyRequest`，但不能替代 Task Agent 的任务结果交付。

### 7.5 NapCat Reply Skill

通用 `napcat-reply` Skill 只装配给 Chat Agent。Task Manager 只能创建
`ChatReplyRequest`，Task Agent 不能触碰通用聊天回复工具；任务 Agent 使用下方独立的
`task-delivery` Skill。

Skill 工具建议拆成：

```text
send_napcat_reply
  text
  segments
  reply_to_message_id: optional
  mention_user_id: optional
  artifacts: optional ArtifactRef[]
  idempotency_key
```

发送层负责：

1. 从运行时上下文确定目标群或私聊，不接受模型自填的跨作用域目标。
2. 校验引用消息、@对象、消息段类型、Artifact 引用和附件大小。
3. 普通文本、引用、@、图片、语音、文件和表情分别转换为 NapCat SDK 类型；禁止模型
   拼接 CQ 码、OneBot 原始 JSON 或本地路径。
4. 附件必须来自已验证的 Artifact，不能把 URL、Markdown 图片或“已生成”的文字当成
   附件。
5. 多媒体发送顺序由 Skill 固定：先发送附件，再发送最终文本；发送结果记录 NapCat
   message_id。
6. 最终正文默认不包含 URL；外部 URL 只能在工具之间流转，不能直接作为群聊正文。
7. 用户要图片、音频或文件时，按 Artifact 类型发送对应 QQ 消息；不能只回复资源链接。
8. 使用 `idempotency_key` 防止模型重试导致重复发送；重复请求返回原发送结果。

`send_napcat_reply` 返回：

```text
NapCatReplyResult
  sent: boolean
  message_ids
  delivered_artifacts
  error_code: optional
  retryable
```

发送失败时，Skill 返回结构化错误；Chat Agent 不得声称已经发送。Task Manager 可以根据
失败结果重新生成 `ChatReplyRequest`，但不能直接调用 NapCat。

### 7.5.1 Task Delivery Skill

`task-delivery` Skill 只装配给 Task Agent，用于交付当前任务已经验证的最终或部分结果：

```text
send_task_delivery
  text: optional
  artifacts: ArtifactRef[]
  reply_to_message_id: optional
  mention_user_id: optional
  idempotency_key
```

发送规则：

1. 目标作用域、任务 ID 和 revision 由 Runtime 从 Task Agent 上下文注入，模型不能自填
   其他群、私聊或任务的目标。
2. `text` 只能是当前任务的交付说明；`artifacts` 只能引用当前 revision 已通过校验的
   文本、图片或附件。中间产物、调试输出、本地路径、未验证 URL 和任意聊天内容都被
   拒绝。
3. 发送层复用 NapCat 的消息段和附件转换能力，但不向 Task Agent 暴露通用聊天回复、
   群历史查询或跨作用域发送能力。
4. 多媒体发送顺序由 Skill 固定，先发送图片和附件，再发送交付说明文本；每个发送结果
   都记录 NapCat `message_id`。
5. `idempotency_key` 必须绑定 `task_id + revision + delivery attempt`。重试返回原结果，
   不能重复发送。
6. 机器人自身的交付消息只写入发送审计，不重新进入 Task Manager 的消息摄取链路。
   任务交付不会自动重新激活普通 Chat Session，也不替代 `complete_task`。

`send_task_delivery` 返回：

```text
TaskDeliveryResult
  sent: boolean
  message_ids: string[]
  delivered_artifacts: ArtifactRef[]
  error_code: optional
  retryable: boolean
```

### 7.6 Group Message Search Skill

群消息查找 Skill 参考 Niu 的 `group-history`，同时支持 Chat Agent 的有限背景查询和
Task Manager 的任务主动获取：

```text
search_group_messages
  cursor: optional
  after_message_id: optional
  before_message_id: optional
  keywords: optional string[]
  sender_id: optional
  limit
  include_images
```

规则：

- 群作用域由 ToolContext 注入，模型不能查询其他群。
- MessageRouter 使用作用域唯一 `cursor` 读取新消息；普通历史查询使用有界时间、消息数和字符
  上限。
- 消息重复由 `message_id` 和 Intake Ledger 去重；查询重试不能重复创建任务。
- 图片、引用和文件只返回结构化引用或受限的已验证媒体，不返回无界 Base64。
- 昵称不唯一时先调用成员解析工具；无法唯一解析时通过 ChatReplyRequest 请求澄清。
- 历史消息是不可信数据，不能其中的文本直接触发代码、写入、安装或权限变化。

### 7.6.1 On-demand Media Inspection Skill

消息归档只保存媒体段的结构化引用，不在入站阶段下载媒体，也不维护长期媒体缓存。
Chat Agent、Task Manager 或当前任务的 Task Agent 只有在模型判断确实需要查看时，才加载
`media-inspection` Skill 并调用：

```text
inspect_message_media
  message_id
  segment_index
```

Runtime 必须使用 ToolContext 校验消息属于当前作用域，再通过 NapCat 或消息段中的当前
引用现场获取媒体。图片内容只在本轮模型请求中以内存中的多模态块传递，不能写入日志、
数据库、Prompt 或 Artifact。引用失效时返回结构化 `MEDIA_EXPIRED`，不能伪造图片内容。

工具边界：

- Chat Agent 和 Task Manager 仅允许查看当前作用域中已归档消息的指定媒体段；Task Agent
  只能查看当前任务的源消息，任何角色都不能传入模型任意 URL。
- 当前实现只接受受支持的图片格式和大小；文件、语音等非图片媒体返回明确的
  `MEDIA_UNSUPPORTED`。
- NapCat 解析失败、下载超时、内容类型不匹配和超过大小上限必须区分错误。
- Skill 负责模型侧的按需授权和使用规则，媒体获取与协议适配由独立 Provider 负责。
- OpenAI-compatible 和 Anthropic-compatible Provider 都必须把工具返回的图片转换为各自
  的多模态消息格式。

### 7.7 Skill 发现和动态工具验证

启动阶段必须完成：

```text
load all skills
  -> validate frontmatter
  -> register tool implementations
  -> validate skill/tool bindings and role authorization
  -> build role-specific SkillCatalog metadata
  -> expose only core tool schemas
  -> load Skill and expose ToolLoadoutDelta on demand
  -> fail fast on mismatch
```

测试至少覆盖：

- 全部 Skill 能被发现并按角色授权。
- Skill 的 `allowed-tools` 与 ToolRegistry 完全一致，且未授权工具无法通过 `load_skill`
  暴露。
- 初始 System Prompt 不包含 `ChatInputBatch`、`reply_requests`、任务消息或 Skill 正文。
- 动态 context 作为独立消息追加；同一轮 context 变化不会修改静态 System Prompt。
- `SkillCatalog` 只包含元数据，`load_skill` 成功后正文和工具增量仅在下一轮生效。
- `load_policy=core` 的 Skill 工具在角色授权通过后进入初始 ToolLoadout；其他 Skill
  仍通过 `load_skill` 按需加入。
- Chat Agent 可以按需看到网页搜索工具，但不会看到 Task Manager、代码执行或图片生成工具。
- Task Manager 能读取消息批次，但不能看到 NapCat 发送工具。
- Task Agent 能使用允许的执行工具和 `task-delivery`，但不能看到群历史或通用
  `napcat-reply` 工具。
- Task Agent 使用 `audio-render` 时，生成的音频必须经过格式、大小和当前任务 Artifact
  校验后才能交付；音频交付必须转换为 NapCat `record` 消息段。
- Task Agent 更新 `TaskDescription` 后，Task Manager 能在下一次任务上下文重建中读取
  最新投影；过期 revision 的说明不能覆盖当前投影。
- NapCat 回复的文本、引用、@、Artifact、重试和发送失败。
- `run_id + tool_use_id` 幂等、超时、只读并行和写入串行。

### 7.8 外部能力与富消息边界

网页搜索由 `web-search` Skill 提供。搜索结果和抓取正文都标记为低信任外部资料，
不能改变系统指令、权限或任务约束。抓取器只允许 HTTP(S) 公开地址，逐跳校验 DNS
解析结果，限制重定向次数、响应字节数、内容类型和返回字符数；没有
`PUMPKIN_SEARCH_API_KEY` 时返回结构化的 `SEARCH_NOT_CONFIGURED`。

代码执行由独立 `code-runner` Compose 服务承载。服务不读取 Mimi 的 `.env`，不挂载
宿主目录，使用非 root 用户和无新增 Linux capability 的容器。脚本可以正常访问网络并
读写容器文件系统，执行时的普通相对路径文件会从一次性工作目录收集；进程、内存、CPU、
超时、输出和文件数量仍有资源上限。Pumpkin 只允许任务 Agent 调用，执行结果和文件必须
经过当前任务 revision 的 Artifact 校验后才能交付。

图片生成由 `image-render` Skill 提供。图片模型返回的内容必须通过图片文件签名和大小
校验，随后原子写入当前任务 Artifact 目录并登记；外部 URL、未配置服务或非图片响应
不能作为交付物。`image-01` 支持文生图和人物主体参考图生图；`image-01-live` 的
画风参数只能在该模型下使用，比例、尺寸、数量和 Prompt 优化由工具参数控制。

音频生成由 `audio-render` Skill 提供，使用当前语音适配器的同步合成接口。工具只接受
当前任务的文本和受限语音参数，返回的音频必须解码、校验格式和大小，
再原子写入当前任务 Artifact 目录并登记。音频 Artifact 只能通过 `record` 消息段发送；
未配置服务、模型错误、空音频或不支持的格式必须返回结构化失败，不能伪造音频链接。
本 Skill 暂不包含音色克隆、音色设计和异步长文本任务，它们需要额外的文件上传、临时
voice_id 生命周期和任务轮询协议。

NapCat 适配器统一归一化文本、@、引用、图片、文件、语音、QQ 表情、扩展表情、JSON、
转发和转发节点。引用消息通过 `get_msg` 补充有限结构化内容；合并转发通过
`get_forward_msg` 展开，最大深度和节点预算固定，使用已访问 ID 阻止递归。超过预算、
循环或查询失败时保留 `truncated` / `expand_error` 标记。发送层只接受受限结构化段，
本地媒体路径必须位于 Pumpkin 数据目录或来自已验证 Artifact。

## 8. 模型适配与重试

### 8.1 统一接口

业务层依赖统一的 `ModelProvider`，不依赖具体厂商 SDK。接口至少需要表达：

- 文本和多模态输入
- Chat / Task Manager / Task 三种请求模式
- 结构化输出和工具调用
- 超时、取消和流式响应
- Token 用量和模型元信息
- 供应商错误到统一错误类型的转换

模型供应商、模型名称、凭据引用、请求超时和重试策略放入配置，不散落在业务函数中。
Task Manager Prompt 和 Task Prompt 默认可以复用同一个任务模型配置；只有存在明确的部署
差异时，才考虑拆分独立模型配置。

### 8.2 错误与空消息重试

模型请求结果分为：

- 成功且有有效文本或工具调用
- 成功但为空
- 可重试错误：网络超时、连接中断、限流、部分 5xx
- 不可重试错误：鉴权失败、参数错误、权限拒绝、明确的内容拒绝
- 响应格式错误：无法解析结构化输出或工具调用参数

建议流程：

1. 对可重试错误和空消息执行有限次数重试，次数、退避和抖动全部配置化。
2. 同一供应商重试耗尽后，按模型路由配置切换备用供应商。
3. 空消息重试时保留原始上下文，但使用新的请求 ID；不能默默当作成功。
4. 有副作用的工具调用必须带幂等键，避免模型重试造成重复发送、重复写入或重复创建
   任务。
5. 所有供应商均失败时，Chat Agent 返回明确的不可用降级提示；Task Manager Agent 将
   任务置为 `failed` 或 `waiting_for_input`，不能伪造完成。
6. 日志记录供应商、模型、请求 ID、重试次数、错误分类和耗时，不记录完整提示词、
   Token、Cookie 或群聊隐私。

## 9. 目标调用链

### 9.1 触发并开始对话

```text
NapCat event
  -> NapCatMessageAdapter
  -> MessageIngress
  -> MessageArchive
  -> MessageRouter
  -> Dispatch(CHAT/TASK/BACKGROUND)
  -> Chat Worker / Task Worker
  -> Task Manager Agent (TASK only)
  -> append TaskManagerTurnContext
  -> resolve SkillCatalog / ToolLoadout
  -> Agent Loop: Model -> ToolResult -> next turn
  -> ordinary message
  -> check active ChatSession / @ / configured prefix
  -> append to PreSessionWindow or ChatPendingWindow
  -> when Chat Agent idle: seal one ChatInputBatch
  -> Chat Agent
  -> NapCat Reply Skill
  -> renew ChatSession TTL
```

### 9.2 Task Manager 主动发现并执行任务

```text
message written to Message Archive
  -> MessageRouter reads after scope cursor
  -> Dispatch(TASK)
  -> Task Manager Agent
  -> append TaskManagerTurnContext
  -> resolve SkillCatalog / ToolLoadout
  -> Task Manager Agent Loop
  -> identify, interpret, split and schedule tasks
  -> create TaskHandoff
  -> single-task Agent
  -> append TaskTurnContext
  -> load authorized Skills on demand
  -> Task Agent Loop: Model -> ToolResult -> next turn
  -> update TaskDescription / emit TaskEvent
  -> Task events
  -> Task Manager receives next dynamic task event
  -> creates optional ChatReplyRequest
  -> ChatPendingWindow (high-priority internal status reply)
  -> Chat Agent uses Chat Prompt to phrase the reply
  -> NapCat Reply Skill
```

### 9.3 Task Manager 回复任务相关消息

```text
task-related message written to Message Archive
  -> MessageRouter
  -> Dispatch(TASK)
  -> Task Manager Agent
  -> append TaskManagerTurnContext
  -> Task Manager Agent Loop
  -> TaskStatusSnapshot
  -> ChatReplyRequest
  -> Chat Agent uses Chat Prompt
  -> NapCat Reply Skill
```

### 9.4 任务执行与交付

```text
Single Task Agent
  -> select allowed skills
  -> execute tools
  -> collect evidence
  -> classify intermediate / final artifacts
  -> validate final deliverables
  -> send_task_delivery
  -> NapCat
  -> delivery result
  -> complete_task
  -> Task Manager records task.completed
```

## 10. 配置草案

配置名称仅作为设计建议，具体实现时统一收敛到 `PumpkinConfig`：

```toml
[assistant]
name = "<configured-assistant-name>"

[activation]
mention_required = false
pre_session_window_seconds = 15
pre_session_max_messages = 12
session_ttl_seconds = 900

[chat]
pending_window_seconds = 10
pending_window_max_messages = 24
pending_window_max_chars = 12000
batch_flush_debounce_ms = 250

[model.chat]
provider = "<primary-provider>"
model = "<chat-model>"
fallback_providers = ["<backup-provider>"]
request_timeout_seconds = 45
max_retries = 2
empty_response_retries = 2

[model.task]
provider = "<primary-provider>"
model = "<task-model>"
fallback_providers = ["<backup-provider>"]
request_timeout_seconds = 90
max_retries = 2
empty_response_retries = 2

[task]
max_running_tasks = 4
max_queued_task_runs = 32
single_task_timeout_seconds = 900
intake_batch_size = 20
allowed_skills = []

[media.image]
provider = "<image-provider>"
api_base_url = "<image-api-base-url>"
api_key = "<image-api-key>"
model = "<image-model>"

[media.audio]
provider = "<audio-provider>"
api_base_url = "<audio-api-base-url>"
api_key = "<audio-api-key>"
model = "<audio-model>"

[media.inspection]
timeout_seconds = 10
max_bytes = 5242880
```

`pending_window_seconds`、`pending_window_max_messages` 和
`pending_window_max_chars` 是 Chat Agent 忙时的消息窗口边界。窗口达到边界时压缩最早
内容并保留摘要、原文引用和 `window_truncated` 标记；不能静默删除已归档消息。
`batch_flush_debounce_ms` 只用于在 Chat Agent 空闲时合并同一瞬间到达的消息，不改变
任务优先和一次批次消费的语义。交互链路默认使用较短的窗口和单次请求时限；图片、音频
生成与代码执行等较慢的外部操作仍由各自 Provider 的独立时限控制，不能复用聊天时限。

图片和音频 Skill 只有在对应 Provider、Base URL、API Key 和 Model 均配置时才进入
`SkillCatalog`；缺少任一项时保持未装载，不能通过 `load_skill` 绕过配置门槛。Provider
适配器由 Runtime 装配，业务 Skill 不依赖具体供应商协议。

`max_running_tasks`、`max_queued_task_runs`、`single_task_timeout_seconds` 和
`intake_batch_size` 是任务执行保护配置，不是任务数量上限。超过运行或排队限制时，
Task Manager 必须保留任务记录并返回结构化排队或失败事件，不能静默丢弃。示例值需要
根据模型、沙箱和宿主资源压测后调整；默认单任务时限为 15 分钟，避免后台任务无限占用
执行槽位。

## 11. 非目标

- 不在本阶段实现永久人格记忆或无限历史记忆。
- 不在本阶段实现跨进程恢复正在执行的模型请求；重启后运行中的任务标记为可恢复或失败，
  由 Task Manager 根据 revision 决定是否重新调度。
- Task Manager 不直接向群发送业务回复；Task Agent 只能通过受限的 `task-delivery` Skill
  向当前任务作用域发送已验证的任务结果。
- 不通过固定关键词列表替代模型意图识别。
- 不把某一家模型供应商的请求格式暴露到 Chat 或 Task 业务层。
- 不在 Pumpkin 内复制其他插件的 NapCat、群消息归档或媒体实现。

## 12. 实现验收项

以下核心语义已经落成接口、配置、持久化和测试：

1. Session 在唤醒后于 TTL 内接收同一作用域的后续普通消息，无需重复 @ 或前缀；过期
   后普通消息仍归档，但等待下一次唤醒。
2. 每条消息永远先进入 Task Manager 的摄取链路。任务识别、任务编排和单任务 Agent
   调度与 Chat Agent 解耦，任务执行不会被聊天忙闲阻塞。
3. Chat Agent 忙时，已判定为普通聊天的消息进入有界 `ChatPendingWindow`；空闲后按
   消息顺序一次性封存为一个 `ChatInputBatch`，不逐条启动模型请求。
4. 窗口大小由时间、消息数和字符预算共同控制；压缩必须留下摘要、消息引用和显式
   截断标记，不能静默丢弃归档消息。
5. 任务完成、失败或等待补充事件可以生成 `ChatReplyRequest`；如果任务 Agent 已通过
   `task-delivery` 发送完成或部分完成结果，则不重复发送同一结果。Session 过期时，
   必要的状态/澄清回复可以单独唤醒 Chat Agent，只注入回复素材，不恢复完整旧聊天上下文。
6. Task Manager 默认不拆分任务。只有多个独立目标、不同生命周期、明确依赖或权限隔离
   确实必要时才拆分；条件充分就执行，只有缺少完成目标所必需的信息时才追问。
7. 群消息查找、`napcat-reply`、`task-delivery` 和 `artifact` 均通过 Pumpkin 自有
   Skill Registry 装配，不直接依赖其他插件实现；模型供应商使用配置注入的
   OpenAI-compatible Provider，凭据只从环境变量读取，Artifact 仅登记当前任务工作区内
   已存在、可读取且不超过 20 MiB 的文件。
8. `media-inspection` 只在模型需要理解图片时按需加载；Runtime 验证消息作用域和媒体段
   索引后现场获取媒体，将受限图片以内存多模态块传给模型，过期或不支持的媒体返回明确
   错误，不写入长期缓存。

实现验收覆盖 `plugins/pumpkin/tests/`：适配器字段归一化、作用域和机器人自消息过滤、
Skill 授权、Session 唤醒和过期任务回复、窗口批次与截断、重复事件幂等、Chat 回复、
任务交付、媒体作用域校验、多模态 Provider 转换和完成门。

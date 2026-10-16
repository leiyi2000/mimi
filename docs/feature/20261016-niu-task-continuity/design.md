---
status: implemented
owner: Mimi maintainers
updated: 2026-10-16
commits: []
---

# Niu 任务连续性与上下文架构

## 1. 摘要

Niu 当前已经具备单次 Agent Run 的 Tool Use、错误码、计划、Finish Gate 和按群连续话题。
但 `RunState` 只在一次 `AgentLoop.run()` 内存在，`ConversationStore` 跨消息保存的主要是
用户原话和最终回复。复杂任务因此缺少可恢复的任务状态：

- 用户的目标、约束和交付形式没有独立存储；
- 工具结果只存在当前 Run，下一条消息无法直接恢复；
- 用户纠正容易被当成新话题；
- 工具成功可能被误认为用户目标已经完成；
- 上下文压缩会把用户陈述、工具事实和模型推测混在一起。

本设计将 Niu 的状态拆成三个层次：

```text
Conversation
  群消息流、发送者和短期交互关系

Topic
  群聊中一段相关消息的组织边界

Task
  一个可恢复的目标、约束、证据和交付生命周期
```

单次 `Run` 只负责尝试推进 `Task`。模型负责理解语义和提出动作，程序负责保存状态、校验
变更、执行工具和判断是否满足完成条件。

本文描述当前已实现的任务连续性协议。单次运行行为仍以
[`20261014-niu-agent-purpose`](../20261014-niu-agent-purpose/design.md) 和现有代码为准。

### 1.1 当前实现进度

Phase 1 至 Phase 4 已经落地：

- 新增按 `topic_id` 隔离的进程内 `TaskState`；
- 每次 Agent 请求注入独立的 `<task_context>`；
- `RunTrace`、Tool 成功/失败事件和 Artifact 类型归约为有界 `Evidence`；
- Agent 错误和 Artifact 发送失败会保留可恢复状态；
- 历史过期时同步清理任务状态；
- 增加 TaskState、Evidence、上下文转义和 Topic 隔离测试。
- 显式消息通过受限模型输出判断 `TaskRelation`，低置信结果进入澄清路径；
- `update_task` 作为受运行时校验的 Action Proposal，不能修改 owner、证据或完成状态；
- `complete_task` 同时经过单次 Run 的工具门和 TaskState 的交付物、验收条件门；
- 旧检查点只从 TaskState 的 Evidence 和结果构造，不再把用户原话当作已确认事实。

## 2. 当前边界与问题

入口和调用链保持不变：

```text
NapCat event
  -> NiuApplication.handle()
  -> QQMessageAdapter
  -> ConversationStore.route()
  -> ConversationStore.accept()
  -> group FIFO worker
  -> AgentLoop.run()
  -> ModelProvider / ToolRegistry
  -> NapCat reply
```

当前职责：

- `plugins/niu/main.py` 装配 Provider、Skill、Tool 和群消息入口；
- `plugins/niu/adapters/qq_message.py` 将 NapCat 消息段转换为领域消息；
- `plugins/niu/core/conversation.py` 管理每群 FIFO、`topic_id`、超时和短期轮次；
- `plugins/niu/core/agent.py` 驱动单次模型和工具循环；
- `plugins/niu/core/runtime.py` 管理单次 `RunState`、Skill 激活和工具完成门；
- `plugins/niu/core/task.py` 管理单次 Run 内的 `TaskFrame`；
- `plugins/niu/core/run.py` 记录脱敏 `RunTrace`。

需要解决的不是某个图片工具，而是以下状态断裂：

```text
用户目标       只在当前消息和模型理解中存在
用户约束       混在自然语言历史中
工具证据       只在当前 Run 的 tool_result 中存在
下一步动作     依赖模型重新猜测
完成标准       主要由模型口头声明
跨消息恢复     只能从最终回复反推
```

## 3. 设计目标

### 3.1 目标

1. 让未完成任务可以跨消息继续，而不依赖重复搜索或重新猜测。
2. 让用户纠正成为任务状态更新，而不是无条件创建新任务。
3. 让任务上下文区分用户约束、已验证事实、工具证据和模型推测。
4. 让工具失败进入可恢复状态，并保留下一步动作。
5. 让“完成”基于交付物和验收条件，而不是仅基于模型返回文本。
6. 让群级 Topic 与 Task 解耦，保留群友补充能力和当前发送者权限边界。
7. 不把语义路由实现成关键词表。
8. 保持简单闲聊的一次模型调用和短回复体验。

### 3.2 非目标

- 不暴露模型隐藏思考、逐步推理或完整工具参数。
- 不将完整群聊永久落盘。
- 不引入多 Agent 或外部 Agent 框架。
- 不让模型直接修改权限、群身份或持久化数据。
- 不用更大的原始聊天窗口替代结构化任务状态。
- 不通过关键词列表判断“继续任务”或“新任务”。

## 4. 核心概念

### 4.1 Conversation

`Conversation` 是消息流，不是任务本身。它保存：

- 当前群和消息序列；
- 发送者 ID、昵称快照和当前权限；
- 当前消息、引用消息和多模态内容；
- 有界的短期消息历史。

Conversation 不负责判断工具是否完成任务。

### 4.2 Topic

`Topic` 是群聊相关性窗口，继续使用现有的 `topic_id`、空闲 TTL、最长 TTL 和
`ignore/absorb/respond` 门控。

Topic 只回答：

> 这条消息是否属于当前群的这段讨论？

它不回答：

> 这条消息是否在继续同一个执行任务？

### 4.3 Task

`Task` 是可恢复的用户目标。建议字段如下：

```python
TaskState(
    task_id,
    group_id,
    topic_id,
    owner_ids,
    goal,
    deliverable,
    constraints,
    acceptance_criteria,
    phase,
    evidence,
    pending_actions,
    last_error,
    status,
    last_run_id,
    updated_at,
)
```

字段含义：

| 字段 | 说明 |
| --- | --- |
| `goal` | 用户希望获得的结果 |
| `deliverable` | 交付物类型，如文本、图片、文件、指令执行结果 |
| `constraints` | 用户明确提出的限制，不自动称为事实 |
| `acceptance_criteria` | 判断目标是否满足的条件 |
| `phase` | 当前阶段 |
| `evidence` | 工具返回的结构化证据摘要 |
| `pending_actions` | 下一步可执行动作 |
| `last_error` | 最近一次失败及其稳定错误码 |
| `status` | 当前任务生命周期状态 |
| `owner_ids` | 参与过该任务的成员，不继承其权限 |

Task 初期只保存在 `ConversationStore` 进程内，不扩大当前数据持久化边界。

### 4.4 Run

`Run` 是一次推进任务的尝试，继续使用当前 `AgentLoop`、`RunState` 和 `RunTrace`：

```text
Task
  -> Run 1: 搜索
  -> Run 2: 核验
  -> Run 3: 下载并发送
```

Run 结束不代表 Task 结束。Run 可以得到：

```text
completed
partially_completed
failed
blocked
```

Task 只有通过自己的验收条件后才进入 `completed`。

## 5. 状态机

```text
IDLE
  -> UNDERSTANDING
  -> PLANNING
  -> EXECUTING
  -> VERIFYING
  -> COMPLETED

EXECUTING / VERIFYING
  -> RECOVERABLE_FAILURE
  -> EXECUTING

UNDERSTANDING
  -> CLARIFICATION_NEEDED
  -> UNDERSTANDING

任意未完成状态
  -> PARTIALLY_COMPLETED
  -> EXECUTING
```

阶段不是模型自由编造的长文本，而是有限集合：

```text
understanding
planning
executing
verifying
clarification_needed
recoverable_failure
completed
partially_completed
blocked
```

程序负责校验阶段迁移；模型只能提出迁移建议，不能直接修改权限、发送者或任务归属。

## 6. 消息路由

### 6.1 两层路由

消息先经过现有 Topic 路由，再经过 Task 关系判断：

```text
Message
  -> TopicRelation
       ignore / absorb / respond
  -> TaskRelation
       new / continue / correction / supplement
       clarify / unrelated / unclear
```

两者不能合并：

- 消息可以属于当前 Topic，但开启新 Task；
- 消息可以是当前 Task 的补充，但不需要 Agent 立即回复；
- 消息可以显式唤醒机器人，但与之前 Task 无关；
- 消息也可能无法判断，需要向用户澄清。

### 6.2 Task 关系判断

Task 关系判断使用结构化输入：

```text
当前 TaskState
当前 Topic 摘要
当前消息和发送者
当前 Task 最近证据
最近两轮当前 Task 消息
```

输出固定结构：

```json
{
  "relation": "new | continue | correction | supplement | clarify | unrelated | unclear",
  "confidence": 0.0,
  "constraint_patch": [],
  "reason": "machine-readable short reason"
}
```

这里不使用固定关键词列表。路由器可以是受限的无 Tool 模型调用，也可以在已有 Agent
首轮返回结构化行动提案时完成，但必须满足：

- 只读取任务上下文，不调用业务 Tool；
- 输出必须经过 Schema 校验；
- 低置信度不能静默当作 `new` 或 `ignore`；
- `unclear` 交给主 Agent 生成最小澄清问题；
- `constraint_patch` 只作为候选变更，最终由 TaskState 校验并提交。

### 6.3 路由结果

| relation | 行为 |
| --- | --- |
| `new` | 创建新的 Task；不自动继承旧 Task 的约束和证据 |
| `continue` | 恢复当前 Task，沿用未完成阶段和证据 |
| `correction` | 恢复当前 Task，更新约束并重新计算待办 |
| `supplement` | 将消息写入 Task 事实候选，不立即启动完整 Run |
| `clarify` | 启动一次最小澄清回复，不调用业务 Tool |
| `unrelated` | 不进入当前 Task |
| `unclear` | 保留当前状态，向用户确认消息归属 |

显式前缀只表示“必须处理”，不表示“必然是新 Task”。

## 7. 上下文组装

每次模型请求按以下优先级组装：

```text
1. stable prompt
   安全、权限、NapCat 协议和执行规则

2. current task state
   当前目标、交付物、约束、阶段、证据、待办和验收条件

3. current run context
   本轮消息、当前发送者、图片、引用和当前 Run 的工具结果

4. selected task history
   当前 Task 最近有界消息和已验证事实

5. long-term memory
   经过权限过滤的稳定记忆
```

不再把旧话题的原始文本直接作为“确认事实”。上下文中必须区分：

```text
UserConstraint
  用户明确要求的限制

VerifiedFact
  已通过工具或协议确认的事实

ToolEvidence
  工具结果的来源、摘要、状态和 Artifact 引用

Hypothesis
  模型推测，不能作为完成证据
```

### 7.1 Evidence

工具结果归约为有界 Evidence：

```python
Evidence(
    evidence_id,
    run_id,
    tool_name,
    kind,
    summary,
    source,
    success,
    supports,
    artifact_ref,
)
```

Evidence 不复制完整网页、Base64 图片或群聊原文，只保留下一步判断所需的信息和安全引用。
完整工具结果继续留在当前 Run 消息中，跨消息只保留摘要和引用。

## 8. 模型与程序的职责边界

### 8.1 模型负责

- 理解用户目标；
- 判断当前消息与 Task 的语义关系；
- 提出目标、约束和验收条件；
- 选择下一步 Tool；
- 解释 Tool Result；
- 在证据不足时提出澄清问题。

### 8.2 程序负责

- 保存和恢复 TaskState；
- 校验模型提出的状态变更；
- 校验工具权限、参数和调用顺序；
- 将 ToolResult 归约为 Evidence；
- 管理失败重试和替代路径；
- 校验 Artifact 类型和发送结果；
- 判断是否满足确定性的完成条件；
- 写入 RunTrace 和指标。

模型不得通过普通文本直接改变：

- 当前发送者权限；
- Task owner；
- 群 ID；
- 任务完成状态；
- 已验证事实；
- 记忆写入结果。

## 9. Action Proposal

主 Agent 可以在每轮提出结构化行动提案，但不需要把内部推理发给用户：

```json
{
  "task_relation": "continue",
  "state_patch": {
    "constraints": ["禁止生成图片"],
    "deliverable": "QQ 图片"
  },
  "phase": "executing",
  "next_action": "从候选网页提取原图并发送",
  "tool_calls": [
    {
      "tool": "attach_remote_file",
      "depends_on": ["search_result_1"]
    }
  ],
  "user_reply": "我继续找网络原图，找到后直接发图片。"
}
```

运行时执行顺序：

```text
parse
  -> schema validation
  -> permission validation
  -> task state patch validation
  -> dependency validation
  -> tool execution
  -> evidence reduction
  -> acceptance check
```

不能让模型直接返回一个 `completed` 字段就完成任务。

## 10. 完成验收

完成条件分为三层：

### 10.1 结构验收

- 必需 Tool 已返回成功；
- 没有未处理的副作用；
- 任务没有待澄清输入；
- 没有 Artifact 发送失败。

### 10.2 交付物验收

- 用户要求图片时存在图片 Artifact；
- 用户要求文件时存在文件 Artifact；
- 用户要求文本结果时存在非空文本；
- 用户要求执行动作时存在对应 Tool 的成功证据。

### 10.3 语义验收

- 交付物满足用户明确约束；
- 结果来源与用户要求一致；
- 没有用模型推测替代工具证据；
- 结果没有把部分完成描述成全部完成。

语义验收可以由模型提出判断，但必须基于 Evidence，且失败时进入
`partially_completed` 或 `clarification_needed`，不能直接标记成功。

## 11. 失败与恢复

| 失败类型 | Task 行为 |
| --- | --- |
| 参数错误 | 记录字段级错误，允许修正后重试 |
| 空结果 | 记录证据缺口，改写查询或选择替代来源 |
| 超时 | 保留阶段和已完成证据，限制重试次数 |
| 相同错误重复 | 禁止原样重试，要求替代路径 |
| 用户约束冲突 | 进入 `clarification_needed` |
| Artifact 发送失败 | 交付未完成，保留 Artifact 来源和待发送动作 |
| 模型无响应 | Run 失败，Task 保留为可恢复 |
| 无法满足验收 | 部分完成，明确剩余项 |

失败不会清空 TaskState，也不会把失败文本写成新的已验证事实。

## 12. 示例：网络图片任务

用户第一句：

```text
南瓜 找一张网上的温蒂图片，不要自己生成，直接发图片
```

建立任务：

```text
goal: 找到指定角色的网络图片
deliverable: QQ 图片
constraints: 来源必须是网络；禁止 AI 生成
acceptance: 存在符合约束的图片 Artifact，并成功发送
phase: understanding
```

搜索返回网页：

```text
evidence: 找到候选网页
phase: executing
pending: 提取原图并发送
status: active
```

此时不能完成，因为网页链接不是图片 Artifact。

用户补充：

```text
南瓜 不要链接
```

路由结果是 `correction`，不是新 Task：

```text
constraints += 不发送网页链接
deliverable = QQ 图片
pending = 提取原图并发送
```

下载失败：

```text
last_error = REMOTE_FILE_DOWNLOAD_FAILED
evidence += 候选 1 下载失败
phase = recoverable_failure
pending = 尝试下一候选
```

只有图片成功发送后，任务才允许进入 `completed`。

## 13. 实施阶段

### Phase 0：观测，不改变用户行为

- 为现有 `RunTrace` 增加 Task 关联字段；
- 记录用户消息、Run、工具、结果和最终状态的关系；
- 统计过早结束、重复工具错误、部分完成和用户重试。

### Phase 1：引入 TaskState（已实现）

- 在 `ConversationStore` 中维护群级活动 Task；
- 当前 `TaskFrame` 继续服务单次 Run；
- 将目标、用户约束、Run 结果和 Artifact 摘要注入下一轮；
- 不改变现有 Topic 路由。

### Phase 2：拆分 Topic 与 Task 路由（已实现）

- 保留 `TopicGate` 的 `ignore/absorb/respond`；
- 新增结构化 Task 关系判断；
- 移除任何基于自然语言关键词的“继续任务”判断；
- 对 `unclear` 提供最小澄清路径。

### Phase 3：接入 Evidence 与验收（已实现）

- 将 ToolResult 归约为 Evidence；
- 增加交付物类型和用户约束校验；
- 将 `complete_task` 从“Tool 成功门”升级为“目标验收门”；
- 将部分完成和可恢复失败写回 Task。

### Phase 4：替换旧检查点（已实现）

- `PreviousTopicCheckpoint` 只保留有界格式化职责；
- 新任务上下文统一来自 TaskState；
- 删除“把后续用户原话直接当确认事实”的逻辑；
- 更新连续对话设计文档和插件 README。

## 14. 代码边界

计划涉及：

| 文件 | 责任 |
| --- | --- |
| `plugins/niu/core/task_state.py` | TaskState、Evidence、状态迁移和上下文格式 |
| `plugins/niu/core/topic.py` | Topic 路由与 Task 关系判断的数据协议 |
| `plugins/niu/providers/topic.py` | 受限语义路由模型调用 |
| `plugins/niu/core/conversation.py` | 群级 Task 生命周期、消息归属和恢复 |
| `plugins/niu/core/agent.py` | 将 TaskState 注入每轮并回传 Run 结果 |
| `plugins/niu/core/runtime.py` | Action Proposal、状态变更和 Tool 依赖校验 |
| `plugins/niu/core/task.py` | 单次 Run 完成门与 Task 验收协作 |
| `plugins/niu/core/run.py` | Task 关联的脱敏运行轨迹 |
| `plugins/niu/prompts/execution.md` | 任务执行规则和状态使用协议 |
| `plugins/niu/tests/` | 路由、恢复、证据、失败和完成验收测试 |

不修改 `plugins/niu/adapters/qq_message.py` 的领域边界，不让适配层理解 Task 语义。
不修改长期记忆的数据所有权，不把临时 TaskState 写入长期记忆。

## 15. 验收矩阵

### Task 生命周期

- 简单闲聊不创建有意义的活动 Task；
- 多步请求创建 Task 并保存目标和交付物；
- 同一 Task 的纠正更新约束而不是重置证据；
- `continue` 恢复上一轮待办动作；
- 新任务不会继承旧 Task 的约束；
- Task 过期后不再被普通消息恢复。

### 上下文

- 下一轮模型能看到目标、约束、阶段、证据和待办；
- 用户原话不会直接成为“已验证事实”；
- 工具完整结果不跨消息复制，只保留有界 Evidence；
- 不同用户可以参与任务，但不继承彼此权限；
- 旧 Topic 不会污染新 Task。

### 工具和完成

- 工具失败会留下稳定错误和恢复动作；
- 相同错误不会无限重试；
- 缺少目标 Artifact 时不能标记完成；
- Artifact 发送失败不会写入完成历史；
- 部分完成会明确保留剩余项；
- 纯文本简单问题仍保持一次模型调用。

### 语义路由

- 不使用固定关键词列表判断任务关系；
- 路由低置信时进入澄清，不静默忽略；
- 路由器不能调用业务 Tool；
- 路由输入不包含图片 Base64、长期记忆或无关历史；
- 路由结果经过 Schema、权限和状态迁移校验。

## 16. 观测指标

上线前后至少记录以下脱敏指标：

```text
task_created
task_continued
task_corrected
task_clarification_requested
task_completed
task_partially_completed
task_failed
task_recovered
premature_text_completion
artifact_required_but_missing
repeated_tool_error
```

每条指标只关联 `run_id`、`task_id`、`group_id` 的内部脱敏标识、阶段、工具名和错误码，
不记录群聊正文、图片、Token、Cookie 或完整 Tool 参数。

重点比较：

- 用户为同一目标重复发起请求的次数；
- 工具失败后成功恢复的比例；
- 任务完成前无 Artifact 的终止比例；
- TaskState 注入后的输入 Token 增量；
- 简单聊天的额外模型调用次数；
- `unclear` 询问后的任务继续率。

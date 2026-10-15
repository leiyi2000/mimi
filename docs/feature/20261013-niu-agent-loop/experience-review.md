# Niu Agent 体验评审与落地结果

## 1. 结论

Niu 没有改造成 smolagents，也没有引入该框架。现有 QQ 群聊适配、权限隔离、按群并发、
媒体输出和隐私边界保持不变，并已针对评审发现完成以下改造：

1. 复杂任务通过 `publish_plan` 在其他 Tool 前发送一次用户可见计划。
2. Web Search 固定由本地 `search_web` 调用 AnySearch，向模型返回标题、完整 URL 和摘要。
3. 输出截断时执行无 Tool 的最终总结，保留已有 Artifact；后续 Pi 对齐改造已删除固定
   Tool 轮次上限。
4. 工具错误包含稳定错误码、安全说明和重试标记；重复错误会要求模型切换方法。
5. `AgentResponse.status`、`RunTrace` 和附件优先发送建立了明确的完成语义。
6. 专用 Skill 通过 `activate_skill` 激活后才注入完整指令和 Tool Schema。

实现保留轻量内核，借鉴了 smolagents 的结构化步骤、错误进入下一轮上下文、最终答案校验
和耗尽后的兜底总结。没有展示 Thought、逐步进度或“正在处理”，简单问答仍保持单次模型
调用。

本文结论来自 2026-10-13 的静态代码评审，没有线上延迟、成功率或用户行为数据。涉及耗时
的数字均为配置推导或建议目标，不是实测结果。

## 2. 对照范围

外部基线固定为 Hugging Face smolagents 仓库提交
[`96f33fa`](https://github.com/huggingface/smolagents/tree/96f33faaf028479119ec8d34507b47694cf14e34)，
重点评审 `MultiStepAgent`、`ToolCallingAgent` 和 `AgentMemory`。本项目重点检查：

- [`core/agent.py`](../../../plugins/niu/core/agent.py)
- [`core/conversation.py`](../../../plugins/niu/core/conversation.py)
- [`core/tools.py`](../../../plugins/niu/core/tools.py)
- [`core/skills.py`](../../../plugins/niu/core/skills.py)
- [`main.py`](../../../plugins/niu/main.py)
- [Niu 轻量 Agent Loop 设计](design.md)

比较维度是任务完成率、长任务可预期性、失败恢复、过程可观测性、权限边界和群聊适配，不比较
框架生态、下载量或模型效果。

## 3. smolagents 的 Agent 设计

### 3.1 统一的多步骤运行时

`MultiStepAgent` 把一次任务建模为 `TaskStep -> PlanningStep? -> ActionStep* ->
FinalAnswerStep`。每个 Action Step 都记录模型输入、模型输出、工具调用、观察、错误、耗时和
Token 用量，并在下一步重新转换为模型消息。[S1](#source-1) [S2](#source-2)

这不是单纯的日志结构。错误也会作为 Observation 回到下一轮，并明确要求模型不要重复之前
的错误，必要时换一种方法，因此框架具备受步数限制的自我修复能力。[S2](#source-2)

### 3.2 流式事件和回调

运行时可以依次产出模型增量、工具调用、工具结果、规划步骤、动作步骤和最终答案。每一步
结束后执行类型化 callback，监控器也通过同一机制统计指标。[S1](#source-1)

这里值得借鉴的是内部事件协议，而不是把模型的隐藏推理直接展示给用户。事件用于日志和
指标；只有复杂任务需要在执行前额外发送结构化计划，不逐步播报内部动作。

### 3.3 显式的最终答案契约

`ToolCallingAgent` 使用独立的 `final_answer` Tool 表示任务完成，并拒绝把最终答案和其他
工具调用混在同一轮。框架还能执行 `final_answer_checks`；达到最大步数时，不直接丢弃结果，
而是根据已有运行记忆再生成一次最终答复。[S1](#source-1)

显式终局的价值在于：运行时可以区分“模型只是说了一段话”和“模型确认任务已完成”，并在
发送前检查答案与附件、工具状态是否一致。

### 3.4 可复盘的运行结果

`RunResult` 包含输出、结束状态、完整步骤、Token 用量和总耗时。`AgentMemory` 同时提供精简
步骤、完整步骤和 replay。[S1](#source-1) [S2](#source-2)

该设计主要改善工程体验：一次失败可以回答“卡在哪一步、哪个工具、花了多久、是否重试”，
而不需要从跨请求日志中人工拼接调用链。

### 3.5 可选规划，而非强制规划

smolagents 的规划由 `planning_interval` 控制，可以在首步建立计划并周期性更新。官方最佳
实践同时强调减少 LLM 调用、合并相关工具、优先使用确定性逻辑，因此不应把规划套到每个
简单问题上。[S3](#source-3)

## 4. Niu 已有的场景化优化

这些能力应保留，不应为了框架一致性退化。

| 维度 | Niu 当前设计 | 相对通用 Agent 的价值 |
| --- | --- | --- |
| 群聊调度 | 每群单消费者 FIFO，不同群受控并发 | 避免同群上下文乱序，同时保留跨群吞吐 |
| 背压 | 每群有界队列，满载时明确拒绝 | 不让请求无限堆积 |
| 模型与工具资源 | 模型请求才占模型信号量，工具执行不占用 | 慢工具不会长期锁住模型槽 |
| 工具幂等 | `message_id + tool_use_id` 缓存结果 | 降低重复副作用风险 |
| 不可信内容隔离 | 读取群历史后只暴露只读 Tool | 比普通 ReAct 循环有更明确的权限降级 |
| Code Mode | Monty 无文件、网络、环境变量和子进程 | 明显小于本地 Python 执行面的风险 |
| Skill 来源 | 仅加载随代码发布的 Skill | 不接受运行时下载代码带来的供应链风险 |
| QQ 输入 | 用户、昵称、表情和图片转为结构化多模态内容 | 不需要模型猜测消息段语义 |
| QQ 输出 | 文本、图片、语音、文件和商城表情统一为 Artifact | 领域输出比返回路径或裸对象更可靠 |
| 隐私 | 群级短期上下文、成员级记忆、图片只保留摘要 | 状态范围与聊天产品一致 |
| 用户错误 | 鉴权、限流、空响应和输入问题使用友好文案 | 不把 SDK 异常直接暴露给群友 |

Niu 的核心方向是正确的：它是 QQ 群聊运行时，不是通用自主编程框架。主要问题发生在
“任务进入队列之后、最终消息发送之前”的体验闭环。

## 5. 评审发现与落地

### P0：Web Search 链接召回和结构化结果不足（已完成）

原有模型侧搜索结果无法稳定保留原始 URL，特定站点的深层页面还可能只返回转载新闻或文字
摘要。对于明确索要链接的请求，这会导致模型知道目标内容却无法给出可验证地址。

落地：

- 移除原有模型侧搜索工具及其独立开关、信号量和超时配置。
- 默认暴露本地只读 `search_web` Tool，固定调用 AnySearch。
- Tool 返回结构化标题、完整 HTTP(S) URL 和摘要，过滤非法 URL，并将网页内容标记为低
  信任数据。
- Skill 要求模型自动改写查询，可增加 `site:domain` 限定词；只有取得完整 URL 后才能
  声称找到目标链接。
- `ANYSEARCH_API_KEY` 是唯一新增的必需 Secret，不提供搜索功能开关或其他部署调参。

### P0：复杂任务执行前没有可见计划（已完成）

评审时，[`AgentLoop.run()`](../../../plugins/niu/core/agent.py) 收到模型 Tool Use 后立即并行
执行，模型生成的中间文本也不会发送。对于研究、整理、生成媒体或其他需要多个依赖步骤的
任务，用户只能等待最终结果，无法提前知道 Agent 将做什么、产出什么，也难以及时发现理解
偏差。

落地：

- 新增结构化 `publish_plan` Tool，只接收目标、2–4 个用户可理解的步骤和预期产物。
- 仅当任务需要多个依赖步骤或明显属于长耗时操作时调用；简单问答保持单次回复。
- `publish_plan` 是执行屏障：同轮还有其他 Tool Use 时，必须先发送计划，再开始其余工具，
  不能与后续工具并行。
- 每次运行最多发布一次计划，不逐步播报内部动作，不发送 Thought、工具参数或隐藏推理。
- 计划发送失败不阻断任务，但写入 Run Trace，最终回复仍需明确实际完成情况。

### P0：轮次耗尽会丢弃已有成果（已完成）

本节记录旧实现及当时的上限兜底；当前 Agent Loop 已对齐 Pi 改为自然终止，不再设置固定
Tool 轮次上限，final synthesis 仅处理输出截断等非正常终止。

评审时，[`AgentLoop.run()`](../../../plugins/niu/core/agent.py) 在最后一轮再次收到 Tool Use
时直接抛出 `ToolRoundsExceeded`。此前已产生的文本、Artifact 和成功工具结果不会发给用户。
smolagents 在达到上限后会基于已有步骤调用一次最终总结。[S1](#source-1)

落地：

- 达到上限后禁止继续调用 Tool，执行一次短超时、无 Tool 的 final synthesis。
- 将成功结果、失败结果和已生成 Artifact 摘要传入该轮。
- synthesis 失败时返回确定性降级文案，并发送已经成功生成的 Artifact。
- 用户文案说明“已完成哪些、哪些未完成”，不要让用户简化问题后从头重试。

### P1：工具错误不利于模型自我修复（已完成）

评审时，[`ToolRegistry.execute()`](../../../plugins/niu/core/tools.py) 对模型只返回
`UNKNOWN_TOOL`、`INVALID_TOOL_INPUT`、`TOOL_TIMEOUT` 或 `TOOL_FAILED`。这些错误适合稳定
分类，但缺少字段名、允许值、是否可重试和替代动作。官方 smolagents 指南也强调工具错误应
提供足够信息，让模型能修正调用。[S3](#source-3)

落地后的 API 内容：

```text
{
  "code": "INVALID_TOOL_INPUT",
  "message": "工具参数不符合输入约束，请根据工具 Schema 修正。",
  "retryable": true
}
```

内部异常和参数值继续只写受控日志；给模型的是脱敏后的可操作信息，给用户的是简短业务
说明。相同错误连续两次时返回 `REPEATED_TOOL_ERROR`，要求模型改走替代工具或尽力答复。

### P1：没有显式完成状态和最终质量检查（已完成）

评审时，只要响应中没有客户端 `tool_use` 且文本非空，就被视为成功
([`AgentLoop.run()`](../../../plugins/niu/core/agent.py))。旧实现没有检查
`stop_reason`，因此因 Token 上限截断的文本也可能作为成功答复；也没有验证“声称已生成
图片”是否真的存在图片 Artifact。

落地：

- `AgentResponse` 增加 `status` 和脱敏 `trace`，区分 `completed` 与
  `partially_completed`。
- 检查模型 `stop_reason`；`max_tokens` 执行一次无 Tool 的压缩总结。
- 最终总结失败时使用确定性部分完成文案，并保留已经成功生成的 Artifact。
- 简单纯文本问题保留“无 Tool 文本即完成”的快速路径，不增加模型调用。

### P1：文本先于附件发送，成功语义可能反转（已完成）

评审时，[`ConversationStore._process()`](../../../plugins/niu/core/conversation.py) 先发送最终
文本，再逐个发送 Artifact。如果文本说“图片已生成”，但 QQ 图片发送失败，用户先看到成功
再看到失败。

落地：

- 含 Artifact 的回复固定先发送全部 Artifact，全部成功后才发送完成文案。
- 任一附件发送失败时停止完成文案，返回明确的部分失败提示，并且不写入完成历史。
- 不增加“正在发送”中间消息，也不为当前固定策略引入额外枚举。

### P1：缺少单次运行轨迹和关键指标（已完成）

评审时，长期审计只覆盖记忆写入，没有统一的 Agent Run。无法直接统计排队时间、计划发送、
模型时间、工具时间、重试次数、错误阶段和最终状态。

落地：

```text
RunTrace
  run_id, group_id, message_id
  started_at, finished_at, plan_sent
  status, model_rounds, tool_calls
  input_tokens, output_tokens
  error_code, events[]
```

步骤详情只写结构化日志，默认不持久化提示词、群聊原文、图片或工具敏感参数。`run_id` 要
贯穿模型、工具和发送日志。

### P2：所有 Skill 指令和 Tool Schema 每轮重复注入（已完成）

评审时，[`AgentLoop._request()`](../../../plugins/niu/core/agent.py) 每轮拼接全部已启用 Skill
正文并暴露全部可用 Tool。随着 Skill 增长，这会提高输入成本，也增加模型误选工具的概率。
`SkillRegistry.allows()` 已存在，但当前请求路径没有按选中 Skill 过滤 schema。

落地：

- 首轮保留简短 Skill Catalog 和少数基础 Tool。
- 模型通过 `activate_skill` 选择一到三个 Skill，下一轮才注入完整说明和对应 Tool。
- 对记忆、回复附件和动态 `mimi` 指令等高频且意图明确的能力直接激活，不增加路由轮次；
  `mimi` 必须在首轮提供完整命令表，避免模型否认点歌等已安装插件能力。
- `ToolRegistry.schemas()` 根据已激活 Skill 过滤 Schema；未激活专用 Tool 即使被模型
  猜中也会返回 `SKILL_NOT_ACTIVE`。

## 6. 目标运行模型

不引入 smolagents 依赖，在现有模块内增加小型事件与结果模型：

```text
QQ message
  -> received / rejected
  -> started
  -> classify
       -> simple task: answer directly
       -> long task
            -> publish plan
            -> model round
                 -> tool requested
                 -> tool started
                 -> tool succeeded | tool failed
                 -> retry | alternate path | final synthesis
  -> answer validated
  -> artifacts sent
  -> completed | partially_completed | failed
```

实际数据结构：

```text
RunEvent(kind, round_index, tool_name, duration_ms, error_code)
RunTrace(run_id, status, model_rounds, tool_calls, usage, events)
AgentResponse(text, artifacts, status, trace)
```

`RunEvent` 和 `RunTrace` 服务于日志与指标，`AgentResponse` 服务于发送层；模型下一轮上下文
继续使用 Anthropic 原生消息块，不重复建立一套步骤序列化协议。

对简单问答保持单次模型调用。只有任务确实需要多个依赖步骤时，才先发布一次面向用户的
简短计划并进入多步骤循环。这样既让长期任务可预期，又不增加普通聊天的噪声和延迟。

## 7. 实施结果

| 优先级 | 工作 | 状态 | 保留约束 |
| --- | --- | --- | --- |
| P0 | 解耦普通请求与搜索信号量/超时 | 已完成 | Server Tool 只临时暴露一轮 |
| P0 | 复杂任务先发布结构化计划 | 已完成 | 不发送 Thought 或逐步进度 |
| P0 | 最大轮次后的 final synthesis | 已完成 | 只增加一次无 Tool 调用 |
| P1 | `RunEvent`、`RunTrace`、结构化运行日志 | 已完成 | 日志不含消息原文 |
| P1 | 结构化工具错误和有限自我修复 | 已完成 | 错误说明必须脱敏 |
| P1 | 终局检查、截断处理、附件发送语义 | 已完成 | 附件失败不发送成功文案 |
| P2 | Skill 两阶段激活 | 已完成 | 简单纯文本请求不增加轮次 |

不建议当前实施：

- 不直接替换为 smolagents。它不能替代现有 QQ、群状态、权限和媒体边界。
- 不把 Thought、内部推理或原始工具参数发到群里；只发送精简的用户可见计划。
- 不启用宿主 Python、Shell、远程 Agent 下载或动态 Skill 安装。
- 不先增加多 Agent。当前问题是单 Agent 的计划、完成和恢复闭环，不是角色数量不足。

## 8. 验收指标与测试

当前完成的是确定性行为验收；线上延迟、成功率和 Skill 命中率仍需部署后建立基线：

| 指标 | 建议目标 |
| --- | --- |
| 复杂任务计划 | 在第一个非计划 Tool 执行前发送，且每次运行最多一次 |
| 简单任务计划 | 不发送，不增加额外模型轮次 |
| 普通请求搜索槽占用 | 0 |
| 达到轮次上限 | 返回部分结果或明确的部分完成状态，不丢已生成 Artifact |
| 工具失败 | 用户看到可执行建议；日志能定位 `run_id + round + tool` |
| 最终截断 | `stop_reason=max_tokens` 不得标记为完整成功 |
| 附件发送失败 | 不先宣称“已发送成功” |
| 隐私 | 运行日志不落群聊原文、图片、Secret 和完整工具参数 |

新增回归测试覆盖：

1. Web Search 首轮可用，并向 AnySearch 发送 Bearer 认证和固定中文区域参数。
2. 搜索成功、空结果、非法 URL、限流、HTTP 错误和非法响应均有确定行为。
3. 复杂任务先发送结构化计划，再执行其他 Tool。
4. 简单请求不发布计划；同一运行不能重复发布计划。
5. 工具错误包含重试信息；相同错误不会无限原样重试。
6. 最大轮次后 final synthesis 返回部分完成状态。
7. `max_tokens` 和空响应不会进入完整成功状态。
8. 附件发送失败时不会先发送成功文案，也不会写入完成历史。
9. Run Trace 不包含计划、消息原文、图片或完整工具参数。
10. 专用 Skill 激活前后分别过滤完整提示词和 Tool Schema。

## 9. 来源

<a id="source-1"></a>
[S1] Hugging Face, `smolagents/src/smolagents/agents.py`, commit `96f33fa`,
2026-10-13 查阅：
https://github.com/huggingface/smolagents/blob/96f33faaf028479119ec8d34507b47694cf14e34/src/smolagents/agents.py

<a id="source-2"></a>
[S2] Hugging Face, `smolagents/src/smolagents/memory.py`, commit `96f33fa`,
2026-10-13 查阅：
https://github.com/huggingface/smolagents/blob/96f33faaf028479119ec8d34507b47694cf14e34/src/smolagents/memory.py

<a id="source-3"></a>
[S3] Hugging Face, “Building good agents”, 2026-10-13 查阅：
https://huggingface.co/docs/smolagents/en/tutorials/building_good_agents

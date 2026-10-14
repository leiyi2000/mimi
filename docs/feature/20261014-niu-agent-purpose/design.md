# Niu Agent 目标驱动优化设计

## 1. 结论

Niu 当前“不够有目的性”的主要原因不是系统提示词不够长，而是运行时没有持续维护“正在完成
什么、还缺什么、何时算完成”。模型每轮只能从聊天文本和工具结果重新推断任务；一旦返回
普通文本且没有 Tool Use，运行时就直接结束。因此模型容易：

- 未把用户请求转成可验证的目标和完成标准；
- 搜到部分信息后过早作答，没有检查证据缺口；
- 工具失败后只解释失败，没有选择替代路径；
- 多轮对话经过字符截断后丢失已完成事项和下一步；
- 同轮工具全部并行，即使后一个调用依赖前一个结果。

本设计参考 Pi 的结构化提示词、可替换运行时上下文、turn hooks、工具执行协议和结构化压缩，
但不复制它的编程 Agent 产品形态。Niu 保留 QQ 群聊的短回复、按群 FIFO、有限轮次、Skill
按需激活和 Artifact 发送语义，并新增两个核心机制：

1. **TaskFrame**：复杂任务的内部目标契约，记录目标、完成标准、约束、证据缺口和下一动作。
2. **Finish Gate**：模型不再以“输出了一段文本”自动代表完成；运行时根据 TaskFrame 和工具
   结果决定完成、继续、部分完成或请求澄清。

简单闲聊、常识问答和无需工具的明确请求不创建 TaskFrame，继续保持一次模型调用。

本文同时记录目标设计与首轮实现边界；当前运行事实仍以
[`ARCHITECTURE.md`](../../../ARCHITECTURE.md) 和代码为准。

### 1.1 实现状态

首轮实现已经落地：

- 参考 Pi 的可替换 system section，将固定提示词拆成安全、执行、表达和 NapCat 协议，
  并独立注入能力目录、记忆、激活 Skill、任务状态和运行指令；
- Skill 路由卡、动态激活枚举、显式意图预激活和 Tool 绑定校验；
- `TaskFrame`、`complete_task` 与基于真实 Tool Result 的 Finish Gate；
- `fetch_url` 的重定向、DNS/IP、MIME、响应大小和文本长度限制；
- 只读工具并行，以及包含副作用时按模型源顺序串行；
- 单次 Run 隔离工作区的列举、文本读写、Shell、文件导出和统一清理。

跨消息 Task Checkpoint、附件复制、工具结果 Evidence 压缩和运行中 steering 尚未实现。
这些能力不会由当前代码或文档对外承诺，后续应依据真实场景指标分阶段引入。

## 2. 对照范围

外部基线固定为 Pi 仓库提交
[`195120a`](https://github.com/earendil-works/pi/tree/195120a9156b79b0fab67d3a458b5315bed7a33f)。
重点阅读：

- [`packages/agent/src/agent-loop.ts`](https://github.com/earendil-works/pi/blob/195120a9156b79b0fab67d3a458b5315bed7a33f/packages/agent/src/agent-loop.ts)
- [`packages/agent/src/types.ts`](https://github.com/earendil-works/pi/blob/195120a9156b79b0fab67d3a458b5315bed7a33f/packages/agent/src/types.ts)
- [`packages/coding-agent/src/core/system-prompt.ts`](https://github.com/earendil-works/pi/blob/195120a9156b79b0fab67d3a458b5315bed7a33f/packages/coding-agent/src/core/system-prompt.ts)
- [`packages/coding-agent/src/core/agent-session.ts`](https://github.com/earendil-works/pi/blob/195120a9156b79b0fab67d3a458b5315bed7a33f/packages/coding-agent/src/core/agent-session.ts)
- [`packages/coding-agent/src/core/compaction/compaction.ts`](https://github.com/earendil-works/pi/blob/195120a9156b79b0fab67d3a458b5315bed7a33f/packages/coding-agent/src/core/compaction/compaction.ts)

本项目重点检查：

- [`core/agent.py`](../../../plugins/niu/core/agent.py)
- [`core/conversation.py`](../../../plugins/niu/core/conversation.py)
- [`core/tools.py`](../../../plugins/niu/core/tools.py)
- [`prompts/system.md`](../../../plugins/niu/prompts/system.md)
- [`prompts/personality.md`](../../../plugins/niu/prompts/personality.md)
- [Niu Agent 体验评审](../20261013-niu-agent-loop/experience-review.md)

Pi 是编程 Agent，Niu 是群聊 Agent。比较目标是提炼运行时机制，不比较终端 UI、代码编辑
能力、会话树或模型供应商。

## 3. Pi 的关键设计

### 3.1 提示词短，但上下文有明确分层

Pi 默认身份说明很短。完整系统提示词由独立 section 组成，包括工具摘要、工具规则、附加
规则、项目上下文、Skill 目录和工作目录。section 可以独立替换，工具集合变化时也会重建
对应提示词，而不是在一段大文本中不断追加。

这说明 Agent 的稳定性主要来自：

- 稳定规则与动态上下文分离；
- 工具既有 Schema，也有使用准则；
- Skill 首先以目录形式暴露，匹配任务后再加载详细正文；
- 当前真实工具集合和提示词描述保持一致。

Niu 已有固定 prompt、Skill catalog 和激活后的 Skill 正文，但目前仍把它们简单拼接成一个
system 字符串，缺少“当前任务状态”这一动态 section。

### 3.2 Agent Loop 提供明确的 turn 边界

Pi 在一次 assistant turn 周围提供三类 hook：

- `prepareRequest`：每次模型请求前更新上下文、模型或推理强度；
- `prepareNextTurn`：一轮完成且即将继续时压缩上下文、刷新工具或注入消息；
- `finishTurn`：assistant 和工具结果都落入上下文后，决定结束或强制继续一轮。

循环还区分 steering 和 follow-up：前者在执行中改变方向，后者等待当前任务自然结束再处理。
关键价值不是 hook 名称，而是运行时可以在模型调用前后执行确定性策略，而不是把所有控制权
交给“模型有没有继续调用工具”。

### 3.3 工具调用是运行时协议

Pi 对工具参数集中校验，在完整 assistant turn 结束后执行工具，并将结果作为正式消息写回。
工具可配置串行或并行；截断的工具参数不会被执行；before/after hook 可以拒绝、改写或观察
调用。

Niu 已有 Tool Schema、超时、错误码、幂等缓存和只读降级，这是正确基础。欠缺的是依赖关系
表达：当前除 Skill 激活和计划外，其余同轮 Tool Use 一律并行。模型若同时提出“搜索候选页”
和“读取候选页”，后者并没有前者的 URL，逻辑上只能失败或猜测。

### 3.4 长上下文被压缩为任务检查点

Pi 的 compaction 不是普通聊天摘要，而是固定保存：

- Goal
- Constraints & Preferences
- Progress：Done、In Progress、Blocked
- Key Decisions
- Next Steps
- Critical Context

这个结构使压缩后的上下文仍能回答“任务是什么、做到了哪里、接下来做什么”。Niu 当前历史
只保存 user/final assistant 文本，并按字符上限从旧到新截断；工具证据、失败原因和未完成
事项不会进入下一次群消息。

### 3.5 Pi 没有替 Niu 定义 Goal Runtime

Pi 提供了足够灵活的 Agent Loop 和结构化 compaction，但通用底层并没有强制每次任务先生成
Goal 或 Plan。它依赖 coding-agent 的系统提示词、上下文文件、工具规则和会话管理形成任务
连续性。

因此 Niu 不应宣称“照搬 Pi 的 Goal 系统”。TaskFrame 和 Finish Gate 是基于 Pi 可插拔运行
时思想、针对群聊短上下文和易提前结束问题增加的场景化设计。

## 4. 当前问题

### 4.1 提示词只规定“怎么说”，没有规定“怎么完成”

[`prompts/system.md`](../../../plugins/niu/prompts/system.md) 主要处理安全、记忆和计划，
[`prompts/personality.md`](../../../plugins/niu/prompts/personality.md) 主要处理语气、长度和
富回复。它们没有明确要求模型：

1. 识别用户真正要的产物；
2. 提取可验证的完成标准；
3. 区分事实、推断和未知；
4. 每轮只选择最能缩小缺口的下一动作；
5. 回复前检查是否真的满足目标；
6. 无法继续时说明已完成、未完成和所需输入。

“善于观察”是人格描述，不是执行协议，无法稳定约束工具决策。

### 4.2 无 Tool 文本被直接视为完成

[`AgentLoop._run()`](../../../plugins/niu/core/agent.py) 在模型没有返回 Tool Use 时，只检查文本
是否为空和是否达到 Token 上限，随后返回成功。运行时无法区分：

- 已完成目标的最终答案；
- 工具执行前的解释；
- 缺少证据的猜测；
- 仍需继续搜索的阶段性总结；
- 应向用户澄清的阻塞状态。

已有 final synthesis 解决了轮次耗尽时丢结果的问题，但不能阻止正常轮次中的过早结束。

### 4.3 每轮重新推断目标

当前循环局部变量记录激活 Skill、是否发过计划、只读状态和重复错误次数，却没有任务目标、
完成标准和当前阶段。模型每轮必须从增长的消息数组自行恢复任务状态，工具结果较长时尤其
容易偏离最初请求。

### 4.4 历史是聊天记录，不是任务状态

[`ConversationStore`](../../../plugins/niu/core/conversation.py) 只持久化本轮用户文本和最终回复。
这是合理的隐私默认值，但复杂任务跨消息继续时，模型无法知道上一轮：

- 哪些来源已经核验；
- 哪个工具失败过；
- 哪个约束是用户明确提出的；
- 哪个产物仍未交付。

不能通过无限扩大原始群聊历史解决，否则会增加隐私面、提示词注入面和 Token 成本。

## 5. 提示词优化

### 5.1 提示词分四层

```text
stable rules
  安全、权限、不可信内容、事实性、隐私

conversation contract
  群聊语气、短回复、At、富回复和 Artifact 规则

capability catalog
  当前可用 Skill 摘要、已激活 Skill 正文、真实 Tool Schema

run context
  当前用户消息、记忆、TaskFrame、工具结果和预算
```

稳定层不随轮次变化；运行层由 `prepare_request()` 每轮重建。不要把 TaskFrame 追加进历史
文本，而应覆盖同名动态 section，避免旧状态与新状态同时出现。

### 5.2 新增任务执行协议

在稳定 prompt 中加入简短规则，控制行为而非要求展示推理：

```text
处理任务时先确定用户要获得的结果。对于需要工具、核验或多个依赖步骤的任务：

1. 以用户明确要求为目标，不擅自扩大范围。
2. 将完成标准转成可核验的结果；缺少关键输入时只问一个最必要的问题。
3. 每轮选择最能减少当前证据缺口的动作，先使用已有结果，不重复无效调用。
4. 工具结果只是证据，不等于任务完成；区分已确认事实、合理推断和未知。
5. 只有完成标准均满足，或已明确说明无法完成的部分时，才给出最终答复。
6. 不展示隐藏推理、内部 TaskFrame、工具参数或逐步思考。
```

这段规则不要求模型输出 Goal、Thought 或自评 JSON。结构化状态由运行时维护，用户仍只看到
必要的计划和最终答复。

### 5.3 动态 TaskFrame

只有进入复杂路径后，系统提示词才增加：

```text
<task_frame>
目标：找到用户指定视频的可点击原始链接。
完成标准：
- 返回至少一个已核验的完整 URL
- URL 指向用户要求的平台和内容
约束：
- 不使用未经核验的猜测链接
当前阶段：gathering_evidence
已完成：
- 已获得两个候选标题
证据缺口：
- 尚未确认候选 URL 是否为原始视频
下一动作：打开候选结果并核验标题和平台
剩余预算：2 个模型轮次
</task_frame>
```

TaskFrame 必须短小、脱敏、面向任务事实，不保存隐藏推理。`下一动作` 是运行时对模型的行动
约束，不是展示给用户的过程播报。

### 5.4 Tool 描述补足“何时用、成功是什么”

Tool Schema 继续描述参数结构；Tool/Skill 指令额外统一说明：

- **Use when**：什么证据缺口适合该工具；
- **Do not use when**：已有结果足够或权限不允许的情况；
- **Success evidence**：什么结果才能支持完成标准；
- **Failure recovery**：超时、空结果、不可访问时的替代动作；
- **Side effects**：是否会发消息、写记忆、生成或发送文件；
- **Concurrency**：可否与其他调用并行，是否依赖上一结果。

这比单纯增加工具描述长度更有效，因为它直接约束工具选择和完成判断。

### 5.5 Skill 命中优化

当前 Skill catalog 每项只有名称和一句描述，`activate_skill.skills[]` 只是任意字符串，不包含
当前可选 Skill 的动态枚举。模型需要先从自然语言目录记住精确名称，再构造激活调用；名称
错误、能力重叠或描述未覆盖用户措辞时，就会直接跳过能力。

现有默认激活集合还会掩盖路由问题。`agent-runtime`、`memory`、`mimi`、`reply` 和
`web-search` 首轮已经注入；真正依赖模型激活的是 `code`、`group-history`、`image`、
`music`、`render` 和 `stickers`。因此不能只用搜索或记忆场景验证 Skill 路由。

Skill catalog 应改为结构化路由卡：

```text
<skill>
  <name>code</name>
  <summary>在隔离工作区执行程序和处理文件</summary>
  <use_when>计算、数据转换、运行 Bash/Python、下载并处理文件、生成非纯文本产物</use_when>
  <examples>运行这段代码；把 JSON 转 CSV；下载这些数据并画图；生成 zip</examples>
  <do_not_use_when>只需搜索网页；只需直接回答；需要读取 Mimi 或宿主机文件</do_not_use_when>
  <tools>run_shell, read_workspace_file, write_workspace_file</tools>
</skill>
```

具体改造：

1. YAML 元数据增加 `triggers`、`examples`、`anti-triggers` 和可选 `priority`，启动时校验类型与
   长度。
2. `activate_skill` 的 Schema 根据当前启用 Skill 动态生成 `enum`，并在每个名称旁保留简短
   路由说明，拒绝模型臆造名称。
3. system prompt 明确“用户意图与目录任一示例语义相近时先激活；不确定时最多选择三个”，
   但不要求模型展示分类过程。
4. 对高置信显式意图做确定性预激活，不额外调用分类模型。例如“运行代码、执行 Python”
   激活 `code`，“生成图片、画一张”激活 `image`，“查群历史、之前谁说过”激活
   `group-history`。规则只覆盖明确措辞，模糊请求仍交给模型。
5. Tool Registry 启动时校验 Skill `allowed-tools` 与 Tool `skill_name` 双向一致，避免目录宣称
   有能力但 Schema 未注册，或 Tool 已注册却永远无法激活。
6. RunTrace 记录候选 Skill、激活来源（默认、规则、模型）、首次激活轮次和
   `SKILL_NOT_ACTIVE`，不记录用户原文。

不建议把所有 Skill 默认激活。完整 Skill 正文和 Tool Schema 会增加 Token、误调用和副作用
风险；`code`、记忆写入、媒体生成等能力尤其应保持按需暴露。

### 5.6 最终答复协议

复杂任务最终答复在内部满足以下检查，外部文案仍遵循短回复规则：

- 先给用户请求的结果，不复述执行过程；
- 关键事实有对应工具证据，不把搜索摘要当页面事实；
- 声称“已生成、已发送、已记住”时有成功 Tool Result 或 Artifact；
- 部分完成时明确已完成项、未完成项和原因；
- 需要用户输入时只问阻塞当前任务的最小问题；
- 链接必须来自工具返回的完整 URL，不自行补全。

## 6. Agent 流程设计

### 6.1 双路径入口

```text
incoming message
  -> deterministic fast-path check
       -> simple: current single-request flow
       -> complex: initialize TaskFrame and RunState
```

简单路径包括闲聊、改写、翻译、明确常识问答和无需核验的短内容生成。复杂路径包括：

- 模型首轮请求 Tool Use；
- 用户明确要求搜索、核验、执行、生成文件或调用群能力；
- 任务包含多个依赖步骤或明确完成条件；
- 上一轮存在可继续的 active checkpoint。

不建议额外调用一个 LLM 专门分类。第一阶段可在首轮仍按现有方式请求模型：若直接给出文本，
走快速路径；若提出 Tool Use，则由运行时根据用户请求、工具类型和调用参数初始化 TaskFrame。
后续若误判率高，再评估轻量分类器。

### 6.2 RunState

```python
@dataclass(slots=True)
class TaskFrame:
    goal: str
    success_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    stage: str
    completed: tuple[str, ...]
    evidence_gaps: tuple[str, ...]
    next_action: str


@dataclass(slots=True)
class RunState:
    task: TaskFrame | None
    active_skills: set[str]
    round_index: int
    remaining_rounds: int
    plan_published: bool
    read_only_only: bool
    evidence: list[Evidence]
    failures: list[ToolFailure]
    artifacts: list[Artifact]
```

建议阶段值保持有限集合：

```text
understanding
clarification_needed
gathering_evidence
executing
validating
completed
partially_completed
blocked
```

`Evidence` 只保存工具名、调用 ID、结果摘要、来源 URL、成功状态和可支持的完成标准，不复制
完整网页或群聊原文。状态只在单次运行内存在；跨消息 checkpoint 另行处理。

### 6.3 每轮生命周期

```text
prepare_request
  -> 重建 prompt sections
  -> 注入最新 TaskFrame、预算、active skills 和权限状态
  -> 确保提示词中的工具集合等于真实 schema

model turn
  -> text and/or tool calls

validate_calls
  -> 参数校验、权限校验、依赖校验、重复调用检查

execute_calls
  -> 无依赖只读调用可并行
  -> 有副作用或存在数据依赖的调用串行
  -> 结果规范化为 Evidence / Failure / Artifact

finish_turn
  -> 更新 TaskFrame
  -> 运行 Finish Gate
  -> end / continue / clarify / partial

prepare_next_turn
  -> 必要时压缩工具结果
  -> 刷新 skill/tool loadout
  -> 注入一条内部 continuation directive
```

实现不必照抄 Pi 的 TypeScript API，但应保留这三个明确边界。建议在 Python 中封装
`RunController.prepare_request()`、`RunController.finish_turn()` 和
`RunController.prepare_next_turn()`，避免继续把策略堆入 `AgentLoop._run()`。

### 6.4 Finish Gate

Finish Gate 是确定性检查和模型语义判断的组合。

确定性条件：

- 有未完成的副作用 Tool Use 时不得完成；
- Tool Result 显示失败时不得声称对应操作成功；
- 用户要求文件但没有 Artifact 时不得标记完整完成；
- 用户要求链接但 Evidence 中没有完整 URL 时不得声称已找到；
- `stop_reason=max_tokens` 时不得直接完成；
- 达到轮次上限时只能进入 final synthesis 或部分完成。

语义条件由模型在正常响应中通过一个内部 `complete_task` Tool 表达：

```json
{
  "status": "completed",
  "criteria": [
    {"id": "c1", "met": true, "evidence_ids": ["e3"]}
  ],
  "answer": "最终给用户的短回复"
}
```

`complete_task` 不执行外部副作用，仅提交完成声明。运行时校验 criteria、Evidence 和
Artifact 后才接受；不通过时返回结构化错误并继续一轮。为了控制成本：

- 简单路径不要求 `complete_task`；
- 复杂路径在激活 TaskFrame 后才暴露该 Tool；
- `complete_task` 必须单独一轮提交，不得与外部副作用 Tool 混用；
- 达到预算时仍沿用现有 final synthesis 和部分完成语义。

相比“每个无 Tool 文本都再调用一次模型审查”，该方案不增加独立 judge 请求，并让运行时
获得明确终局。

### 6.5 下一动作而非完整重规划

每轮结束只更新一个 `next_action`，不让模型反复生成整份计划。选择优先级：

1. 处理用户新增约束；
2. 补齐阻塞完成标准的关键证据；
3. 执行已经具备前置条件的副作用；
4. 校验产物；
5. 提交完成声明。

如果相同错误重复两次，下一动作必须改为替代工具、缩小范围、请求最小澄清或部分完成，不得
继续原调用。已有 `REPEATED_TOOL_ERROR` 可直接作为该策略的输入。

### 6.6 工具并发策略

默认并行应改为显式分组：

| 类型 | 策略 |
| --- | --- |
| 多个互不依赖的搜索、读取 | 并行 |
| Skill 激活、计划、完成声明 | 串行屏障 |
| 写记忆、发消息、生成媒体等副作用 | 按源顺序串行 |
| 参数引用上一工具结果 | 下一轮执行 |
| 同一资源上的读写组合 | 串行 |
| 无法判定依赖 | 保守串行 |

第一阶段无需设计工具调用 DAG。通过 Tool 元数据增加 `execution="parallel_read" |
"sequential" | "barrier"` 即可覆盖主要风险；模型需要上一结果时自然进入下一轮。

### 6.7 跨消息 Task Checkpoint

只有复杂任务结束为 `blocked`、`partially_completed`，或用户明确表示稍后继续时，才为群聊
保存一个短期 checkpoint：

```text
task_id
owner_user_id
goal
constraints
done
blocked
next_step
evidence_refs
expires_at
```

恢复条件必须同时满足：

- 同一群、同一用户；
- 新消息明确引用、继续或修正该任务；
- checkpoint 未过期；
- 没有与新请求冲突。

checkpoint 不保存完整网页、工具原始输出、图片或其他成员消息。默认 TTL 建议与现有群历史
空闲 TTL 一致，且每个群只保留少量最近任务。普通聊天不创建 checkpoint。

### 6.8 工具能力补齐

#### 现有 `code` 已经具备什么

当前 `code` Tool 已能在隔离 Linux 容器中运行完整 Bash 脚本，脚本内可以使用 Python、
pip、curl、Git 和普通文件读写，也能把 `output/` 文件作为 Artifact 返回。因此不缺底层
Shell 和临时文件系统，更不能把宿主机或 Mimi 容器的文件系统直接暴露给模型。

问题在于它是“一次调用、一个全新工作区”：

- 模型必须一次写对完整脚本，不利于读取、修改、验证的迭代流程；
- 下一次 Tool Use 看不到上一次工作区，只能把文本重新塞入 `files`；
- 搜索结果需要打开网页时，只能先激活高权限 `code` 再使用 curl；
- 用户上传的普通文件目前没有统一映射进工作区；
- `code` 被标为非只读，读取网页或检查文件也会扩大权限面。

#### 建议增加的最小工具集

```text
web-search
  search_web          搜索候选页面
  fetch_url           只读获取公开 URL 的正文、标题和链接

workspace
  list_workspace      列出本次运行工作区文件
  read_workspace_file 分段读取文本文件
  write_workspace_file 写入文本文件
  run_shell           执行 Bash/Python/pip/curl/Git
  export_workspace    将指定文件转为 Artifact
```

`fetch_url` 应优先实现，并保持首轮可用的只读 Tool。它使用与 Sandbox 相同的公网出口限制，
拒绝回环、私网、容器内网和云元数据，限制重定向、响应大小、MIME 类型和超时，返回最终 URL、
标题、正文摘要及页面链接。它补齐 `search_web` 只能返回索引标题和摘要、无法核验深层页面的
缺口。只有遇到必须执行 JavaScript 的页面并有真实需求数据时，再考虑受限浏览器 Tool。

`workspace` 是单次 Agent Run 范围的临时工作区，不是长期文件系统。首次调用时创建，后续
workspace tools 复用，运行结束、超时或异常后统一删除。输入附件只通过显式复制进入该目录；
所有路径规范化并限制在工作区内，禁止 symlink 逃逸。读写、文件数、总大小、命令时间和
Artifact 均有独立限额。

为了兼容现有模型行为，第一阶段保留 `code` 作为“提交完整脚本”的快捷 Tool，同时引入
`workspace` Skill。稳定后可将 `code` Tool 重命名为 `run_shell`，旧名称仅做短期兼容。

#### 不应提供的工具

- 不提供读取 Mimi 源码、`.env`、数据卷或宿主机任意路径的通用 `read`。
- 不提供写入插件目录、持久化目录或 Docker Socket 的通用 `write`。
- 不提供绕过出口代理的原始网络工具。
- 不把每个网站做成专用 Tool；搜索、抓取、工作区处理应保持通用。
- 不默认开放交互式终端；命令必须有明确脚本、超时、退出码和完整结果。

## 7. 群聊适配

### 7.1 不展示内部思维

TaskFrame、Evidence、Finish Gate 和 `complete_task` 都是内部协议。群里只发送：

- 长任务开始前已有的用户可见计划；
- 必要的单个澄清问题；
- 最终结果或明确的部分完成说明。

不发送 Thought、阶段标签、工具参数、自评分数或“我正在思考”。

### 7.2 新消息处理

当前每群 FIFO 意味着一个运行过程中用户的新消息要等前一条完成。Pi 的 steering 值得借鉴，
但第一阶段不应打破群内顺序。先支持两类队列语义：

- 当前任务完成前到达的同用户补充，仍按下一条消息处理，可恢复 checkpoint；
- 其他成员消息始终视为独立任务，不注入当前 TaskFrame。

只有线上数据证明长任务频繁被用户纠正，才增加运行中 steering；届时也必须按
`group_id + owner_user_id + task_id` 绑定，不能把群里任意消息注入正在执行的任务。

### 7.3 回复长度

内部状态更丰富不代表最终回复更长。普通完成结果仍遵循一到两个短句；研究报告、对比表和
多章节内容继续通过富回复或附件交付，群消息只给一句引导。

## 8. 实施方案

### 阶段一：提示词与完成门

1. 在 `system.md` 增加任务执行协议。
2. 新增 `TaskFrame`、`RunState`、`Evidence` 和 `RunController`。
3. 首次出现业务 Tool Use 时初始化 TaskFrame。
4. 复杂路径暴露 `complete_task`，加入确定性 Finish Gate。
5. 保留简单路径、现有计划、final synthesis、错误恢复和 Artifact 逻辑。

该阶段直接解决“没有目标”和“过早结束”，不涉及数据库迁移。

### 阶段二：工具调度与动态提示词

1. 将 system 拼接改为命名 section builder。
2. 将 Skill catalog 改为路由卡，动态生成 `activate_skill` 枚举并增加显式意图预激活。
3. 每轮覆盖 TaskFrame section，不追加旧版本。
4. Tool 增加执行类别和成功证据说明。
5. 只读无依赖调用并行，副作用和屏障调用串行。
6. `RunTrace` 增加 Skill 路由、finish-gate 拒绝原因和 task stage，不记录原始内容。

### 阶段三：上下文压缩与任务续接

1. 增加只读 `fetch_url` 和 Run 范围的临时 workspace tools。
2. 将普通文件附件显式复制到工作区，运行结束后统一清理。
3. 工具结果超出阈值时压缩为 Evidence，不把全部正文送入后续轮次。
4. 历史裁剪前生成 Goal/Constraints/Progress/Next Step checkpoint。
5. 增加短期、按群和成员隔离的 active task store。
6. 用实际指标决定是否实现 steering，不预先引入复杂队列协议。

## 9. 测试

### 9.1 单元测试

- 简单闲聊只调用模型一次，不创建 TaskFrame；
- 首轮 Tool Use 后创建正确目标、完成标准和证据缺口；
- 文本存在但完成标准未满足时 Finish Gate 强制继续；
- 要求 URL 时，没有完整 URL Evidence 不接受完成；
- 要求图片时，没有图片 Artifact 不接受完成；
- 成功副作用 Tool Result 可支持对应完成声明；
- `complete_task` 与外部 Tool 同轮出现时被拒绝；
- 相同错误两次后下一动作不能重复原调用；
- 有依赖或副作用工具串行，只读独立工具并行；
- 轮次耗尽仍保留 Artifact 并返回部分完成；
- TaskFrame section 每轮替换，旧状态不残留。
- `activate_skill` 只接受当前目录中的动态枚举值；
- 显式代码、绘图和群历史请求能预激活对应 Skill；
- Skill `allowed-tools` 与 Tool Registry 不一致时启动失败；
- `fetch_url` 拒绝私网、超限响应和不安全重定向；
- workspace 跨同一次 Run 保留文件，跨 Run 不可见；
- workspace 路径穿越和 symlink 逃逸被拒绝。

### 9.2 场景回归

固定以下对话 fixture：

1. “在吗”应直接短答；
2. “找黄仁勋世界巡演 AI MV 大赛的 B 站原视频链接”必须取得完整、可核验 URL 后完成；
3. “帮我分析这个网页并生成表格”必须先读取证据，再生成 Artifact，再提交完成；
4. 搜索无结果时应改写查询，预算耗尽后如实部分完成；
5. 用户缺少必要输入时只问一个澄清问题；
6. 前一条任务部分完成后，同用户“继续查”能恢复 checkpoint；
7. 其他群成员“继续”不得接管前一人的任务。

### 9.3 指标

上线前后比较：

- `premature_completion_rate`：Finish Gate 拒绝的首次完成声明比例；
- `task_success_rate`：固定场景完成标准通过率；
- `tool_error_repeat_rate`：相同工具错误重复比例；
- `mean_model_rounds`：简单路径与复杂路径分别统计；
- `evidence_coverage`：最终事实声明具备 Evidence 的比例；
- `partial_completion_rate`：部分完成占复杂任务比例；
- `skill_activation_recall`：固定意图集应激活 Skill 的命中率；
- `skill_activation_precision`：激活后实际使用对应 Tool 的比例；
- `skill_activation_round`：首次需要专用能力到成功激活的轮次；
- P50/P95 总耗时和输入 Token。

目标不是让轮次越多越好。简单路径模型轮次应保持不变；复杂路径成功率提升必须抵消新增
Token 和延迟。

## 10. 风险与约束

### 10.1 TaskFrame 由模型生成，可能本身理解错误

用户明确约束必须原样保留；运行时不得从工具结果反向改写用户目标。TaskFrame 初始化后若
目标发生实质变化，应要求用户确认或由新消息明确修正，不能静默扩张任务。

### 10.2 Finish Gate 可能造成无效续轮

每次拒绝必须返回具体、可操作且脱敏的原因，并受最大轮次限制。连续两次缺口没有变化时直接
转部分完成或澄清，不允许无限“再检查一次”。

### 10.3 状态增加隐私和注入面

TaskFrame 只保存任务摘要，不保存其他成员原话。网页和工具内容只能进入 Evidence data，
不能成为高优先级规则。checkpoint 有 TTL、群和成员隔离，不写入长期用户记忆。

### 10.4 Prompt 过长反而降低遵循度

稳定执行协议控制在少量规则；Tool 细节随 Skill 激活；动态 TaskFrame 设置字符上限。不要
同时保留多版计划、摘要和 TaskFrame。

## 11. 不采纳项

- **不整体替换为 Pi**：Pi 是 TypeScript 编程 Agent，无法直接承接 NapCat、群级队列、
  记忆边界和 Mimi Tool。
- **不让每条消息先生成计划**：简单聊天会增加延迟和机械感。
- **不展示思维链**：用户只需要计划、证据充分的结果和失败边界。
- **不引入独立 LLM judge**：先用 `complete_task` 加确定性校验，避免每次完成额外请求。
- **不持久化完整运行 transcript**：RunTrace、Evidence 摘要和短期 checkpoint 足够排障。
- **不立即实现会话树和分支**：群聊缺少对应交互，收益不足。
- **不立即实现运行中 steering**：先保持每群 FIFO 和状态隔离，依据线上纠正率再决定。
- **不使用站点专用搜索流程**：继续遵守通用查询改写、结构化 URL 和页面核验策略。

## 12. 验收标准

实现完成后应满足：

1. 简单消息保持当前一次调用和短回复体验。
2. 复杂任务在运行时拥有可观测但不向用户泄露的 TaskFrame。
3. 模型普通文本不能绕过复杂任务的完成标准。
4. 最终成功声明能够关联 Tool Result、Evidence 或 Artifact。
5. 依赖工具不会因盲目并行而使用不存在的上游结果。
6. 长上下文压缩后仍保留目标、约束、进度、阻塞和下一步。
7. 跨消息续接按群、成员和 TTL 隔离，不扩大长期记忆范围。
8. 现有计划、错误恢复、final synthesis、Skill 激活和 Artifact 发送语义不退化。
9. Skill 路由通过动态枚举、语义示例和显式意图预激活获得可测试的命中率。
10. Bash、文件读写和文件导出只发生在单次 Run 的隔离工作区，无法访问 Mimi 或宿主机。
11. 搜索结果可通过独立只读 `fetch_url` 核验，无需为普通网页读取激活 `code`。

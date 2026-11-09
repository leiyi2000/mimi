---
status: superseded
owner: Mimi maintainers
updated: 2026-11-09
commits: []
---

# Pumpkin 统一 Agent Loop 与 Playwright Browser Profile

> 本方案已被
> [`../20261109-pumpkin-browser-code-execution/design.md`](../20261109-pumpkin-browser-code-execution/design.md)
> 取代。保留本文仅用于记录 locator inspect/action 控制面的设计背景，不再作为后续实现依据。

## 1. 背景

Pumpkin 原先使用同一套 `SessionAgentLoop` 执行 Conversation Agent 和 Task Agent：
Runtime 构建上下文，模型产生强类型工具调用，工具结果回灌下一轮，并在步骤预算内通过显式
终态工具结束。两类 Agent 的差异主要由 `AgentLoopProfile`、`TurnPolicy`、工具集和完成
工具表达。

Playwright 浏览器能力已经具备独立 Worker、公网出口校验、单 run 浏览器现场、task
revision、Artifact gate、幂等防重放和 Outbox 交付。改造前的 Browser Controller 虽然删除了
旧实现中的 checkpoint、三阶段收尾和独立模型验收，但仍单独实现模型请求、决策循环、工具
Schema 装配、错误处理、步骤预算、结果回灌和显式终态。这些职责与 `SessionAgentLoop`
重复，使三类 Agent 在模型协议、错误语义、预算收敛和 Trace 上可能逐渐分叉。

本设计将 Chat、Task、Browser 收敛到同一个 Agent Loop 内核。三类 Agent 使用不同的
Profile、上下文适配器、工具装载、执行策略和完成门禁；Browser 不再拥有第二套模型循环，
只保留页面状态、动作后断言、副作用冻结和证据验收。

```text
Conversation input ─┐
Task execution ─────┼─> AgentLoop
Browser contract ───┘     ├─ build context
                          ├─ call model
                          ├─ validate tool calls
                          ├─ execute through profile policy
                          ├─ feed results back
                          └─ finish by profile completion gate

AgentLoop profile
  ├─ Chat: chat tools + Outbox completion
  ├─ Task: skills + Artifact completion
  └─ Browser: Playwright tools + page evidence completion
```

## 2. 设计结论

1. Chat、Task、Browser 必须复用同一个 `AgentLoop` 实现，不再各自维护模型步骤循环。
2. `AgentLoop` 只拥有通用控制流：模型调用、强类型工具调用、步骤预算、错误处理、工具结果
   回灌、Trace 和显式终态；它不理解 QQ、任务、网页或 Playwright。
3. 三类 Agent 的差异通过强类型 Profile 和策略接口注入，不在通用循环中增加按角色判断的
   大型条件分支。
4. Chat 和 Task 继续使用持久化 Session Event、Inbox 和 Turn/Step 投影。Browser 使用
   当前 task/scope/revision 下的隔离运行上下文，不混入 Conversation Session，但仍经过
   同一 `AgentLoop`。
5. Browser Profile 每个模型步骤只允许一个工具调用，禁止并行工具批次，也不加载普通
   Skill。该约束属于工具调用策略，不构成独立模型循环。
6. 浏览器修改工具由 `BrowserToolExecutor` 执行。一次 Agent 工具调用可以在执行层展开为
   Worker 动作和自动观察断言，再向 `AgentLoop` 返回一个完整 `ToolResult`。
7. 页面版本、最新观察、Playwright locator 动作回执、恢复机会、结果未知和 Worker 命令预算归
   `BrowserExecutionState` 所有，不进入通用 Agent 状态。
8. 修改结果未知时，Browser 执行策略动态隐藏后续修改工具，只允许观察、捕获或部分完成；
   不重放副作用。
9. Browser 完成只由最新页面证据决定：最后一次修改后必须有新观察，required evidence
   必须存在，截图或 PDF 必须绑定最终 observation 和 resource version。
10. Task Agent 仍只调用高层 `delegate_browser_task(goal)`。Browser Profile 的第一个
    `AgentLoop` 步骤生成不可变 `BrowserTaskContract`，父 Agent 不接触底层浏览器参数，
    也不再启动独立 Compiler 模型循环。
11. Playwright Worker 的进程隔离、公网代理、租约、心跳、容量等待、串行命令和资源回收
    保持不变。
12. 父 Task 的 revision、幂等、防重放、Artifact gate、纠正核验和 Outbox 交付保持不变。

## 3. 统一 Agent 架构

### 3.1 AgentLoop

`AgentLoop` 是三类 Agent 共用的模型步骤内核，负责：

1. 按 Profile 的步骤上限生成有界模型迭代。
2. 使用 Profile 的 Provider mode 调用 `ModelProvider`。
3. 保存统一的模型请求 Trace。
4. 校验响应是否满足本 Profile 的工具调用数量约束。
5. 保证每次迭代只接受一次模型响应，避免重复执行同一步。

`AgentLoop` 不直接依赖具体 Session 存储或工具执行器。`SessionAgentLoop` 负责
Conversation/Task 的持久化事件、Skill、工具批次、错误收敛和完成 fallback；
`BrowserAgentRunner` 负责 Browser 的隔离消息、串行工具执行和证据完成门禁。两者向同一
`AgentLoopIteration` 提供当前消息与可见工具，并消费统一的 `AgentLoopStep`。

### 3.2 AgentLoopProfile

Profile 至少表达以下策略：

```text
AgentLoopProfile
  profile
  tool_role
  provider_mode
  system_prompt
  max_steps
  min_tool_calls_per_step
  max_tool_calls_per_step
  allow_parallel_tools
  completion_tools
  completion_fallback
  turn_policy
```

这些字段使用枚举、dataclass 或 Protocol 表达，不使用散落的角色字符串和工具名分支。
Profile 负责声明差异，`AgentLoop` 负责执行相同算法。

### 3.3 Chat Profile

Chat Profile 保持现有行为：

- 使用 Conversation Session Projection、当前 topic 和引用上下文；
- 支持按需 Skill 加载；
- 暴露聊天回复、任务创建、任务查询和受控媒体工具；
- 普通正文通过 `reply_and_complete` 的确定性路径发送；
- 终态由聊天完成工具和 Outbox 结果决定；
- Conversation Inbox、话题关闭和参与边界继续由现有 Runtime 管理。

### 3.4 Task Profile

Task Profile 保持现有行为：

- 使用 task handoff、原始用户请求、纠正条件和 task revision 上下文；
- 只加载任务授权的 Skill；
- 允许符合工具策略的只读并行调用；
- 使用搜索、网页读取、代码、媒体、Artifact 和浏览器委派预算；
- 最后保留完成步骤，并允许 Runtime 提交已有已验证 Artifact 的部分完成 fallback；
- 终态由 `complete_task`、TaskService 和 Artifact gate 决定。

### 3.5 Browser Profile

Browser Profile 使用同一 `AgentLoop`，但采用浏览器专属配置：

- `profile=browser`，模型 Provider 使用 browser mode；
- 上下文只包含 `BrowserTaskContract`、当前页面观察、最近动作结果和剩余命令预算；
- 不读取 Conversation Session，不加载普通 Skill；
- 每个模型步骤必须产生且只产生一个浏览器工具调用或完成工具；
- `max_tool_calls_per_step=1`，`allow_parallel_tools=false`；
- Worker 返回的实际命令清单决定可见浏览器工具；
- `outcome_unknown=true` 后动态移除全部修改工具；
- 完成工具固定为 `finish_browser_task`；
- `BrowserCompletionGate` 可以把模型请求的 `completed` 确定性降级为
  `partially_completed`。

Browser Profile 不使用独立计数器控制模型循环。执行报告中的 `decision_count` 只镜像
`AgentLoop` 当前步骤索引；Worker `command_count` 仍由浏览器执行状态单独统计。

### 3.6 Browser 命令执行器

Browser Controller 中的命令执行器是 Profile 的执行策略，不是 Agent 循环。它负责：

- 校验公网 URL、交互授权、参数 Schema、结构化 Playwright locator 和允许按键；
- 分页读取完整页面正文，并通过受控 Playwright locator inspect 读取结构化元素状态；
- 对多个相互独立的读取执行最多 50 条有界 locator inspect 批次；
- 对多个已观察且相互独立的控件执行最多 50 条有界 locator action 批次，先整体预检
  locator，再逐条记录动作效果；
- 获取和维护单次委派的 Worker run、心跳与清理；
- 串行发送 Worker 命令并维护浏览器命令预算；
- 把观察命令转换为带 observation ID 和 resource version 的 `ToolResult`；
- 对每个修改动作自动执行 `act -> observe/assert`；
- 根据直接效果和新观察计算是否产生进展；
- locator 无匹配、多匹配，或观察/动作未执行时，把具体错误回灌给 Agent，最多允许一次
  不同策略；
- 动作无进展时只允许下一模型步骤选择一次不同策略；
- 动作结果未知时冻结后续修改；
- 只向模型返回有界页面文本、结构化状态和必要错误，不暴露 Worker 协议细节。

一次浏览器修改工具在 Agent 层仍是一个工具调用，但可能消耗两条 Worker 命令：

```text
AgentLoop
  -> browser_playwright_act(locator, method, value)
       -> Worker: Playwright locator action
       -> Worker: browser_snapshot
       -> Controller: compare + assertion receipt
  <- ToolResult(action + assertion)
```

这样 Browser 与 Chat、Task 共享模型控制流，同时保留动作后必须核验的确定性约束。

### 3.7 BrowserCompletionGate

Browser 完成策略检查：

1. 当前 run 至少存在一条最新页面观察。
2. 最后一次修改后存在同一 run、同一或更新 resource version 的观察。
3. 模型提交的 evidence 是最终观察中的精确子串。
4. `BrowserTaskContract.required_evidence` 全部出现在对应观察通道。
5. facts 和 links 能由当前证据台账支持。
6. screenshot/PDF 的 observation ID 和 resource version 与最终观察一致。
7. `outcome_unknown` 时不能完成。

完成门禁只校验浏览器运行的客观证据，不重复调用第二个模型判断同一页面。父 Task 的完成
门禁继续检查浏览器终态、Artifact、revision 和用户纠正条件。

### 3.8 Playwright Worker

Worker 继续负责：

- 每次委派启动全新的 Chromium、BrowserContext 和 Page；
- 通过 run 专属公网代理拒绝非公网流量；
- 执行 Agent 选择的受控 Playwright locator inspect/action；
- 串行执行强类型命令，不理解表单、字段或站点语义；
- 返回 execution、effect、document epoch、resource version、observation ID 和错误；
- 在超时、取消、心跳失联或硬租约到期时回收全部现场。

Worker 不运行模型，不理解 Agent 步骤、父任务语义或完成状态。

## 4. 统一控制流程

三类 Agent 均执行以下流程：

```text
PREPARE
  -> BUILD_CONTEXT
  -> MODEL_STEP
  -> VALIDATE_CALLS
  -> EXECUTE_TOOLS
  -> APPEND_RESULTS
       -> NEXT_STEP
       -> COMPLETE
       -> FAIL
  -> BUDGET_FALLBACK
```

Browser 的差异只发生在 Profile 钩子中：

```text
BUILD_CONTEXT
  -> Browser context projection

VALIDATE_CALLS
  -> exactly one call

EXECUTE_TOOLS
  -> observe
  -> or act + automatic assert

COMPLETE
  -> BrowserCompletionGate
```

通用循环不包含 `if profile == browser` 的页面状态机。Browser 的动作、断言、恢复和完成
证据均封装在执行策略与完成策略中。

## 5. 状态所有权

| 状态 | 所有者 | 生命周期 |
| --- | --- | --- |
| 模型步骤、可见工具、通用错误计数、终态 | `AgentLoop` | 单 Turn |
| Conversation/Task Turn、Step、工具事件 | `AgentSession` | 持久化 Session |
| Browser contract、观察、动作回执、结果未知 | `BrowserExecutionState` | 单次浏览器委派 |
| Browser Worker run、Page、命令序号 | Playwright Worker | 单租约 |
| task revision、委派防重放、Artifact | `TaskRuntime` / TaskService | 任务版本 |
| QQ 发送与结果未知 | Outbox | 持久化发送记录 |

Browser 上下文不得写入 Conversation Agent 的 Session。是否持久化 Browser 的通用
Turn/Step 事件由 Browser context adapter 决定，但它必须绑定 task/scope/revision，且不保存
完整页面正文、填写值、Cookie、Storage 或敏感工具参数。

## 6. 内部契约编译与浏览器入口

Task Agent 只调用 `delegate_browser_task` 并传入一个完整 `goal`，不接触 URL 参数、执行
步骤、模式、幂等键、Playwright、selector、JavaScript 或底层 Worker 协议。

Browser Profile 使用同一 `AgentLoop` 的首个步骤，将 `goal` 与不可变
`original_user_requests` 编译为 `BrowserTaskContract`：

- 只能选择原目标中明确出现的公开 HTTP(S) URL，不得发明地址；
- 只有用户原话明确授权时才能启用公开交互或提交；
- 登录、秘密、验证码绕过和高影响操作直接拒绝；
- 输出模式、产物模式、能力和精确证据条件从目标生成；
- 编译结果通过枚举、长度、产物一致性和公网 URL 校验。

该步骤只暴露 `define_browser_task` 与 `reject_browser_task`，不创建浏览器 run，也不维护
独立 Provider stream。编译失败或尚未发出浏览器命令时，Runtime 可以释放执行预留。
同一 task revision 只接受一个完整浏览器目标；重复目标合并运行或复用终态，改写目标返回
防重放错误。

## 7. 浏览器动作与恢复

### 7.1 观察

首次导航由 Browser 运行上下文在第一个模型步骤前完成，并写入初始观察。观察包含：

- 当前 URL、标题和有界可见文本；
- Playwright 原生 ARIA snapshot；
- document epoch、resource version、observation ID 和 progress fingerprint。

完整正文通过字符偏移分页。Agent 根据 ARIA snapshot、正文和按需 locator inspect 理解
页面；同一页面版本的分页正文与 inspect 结果进入有界工作记忆，页面修改后清空。Worker
不生成控件清单、表单状态、题目组或站点适配数据。单次观察统一上限为 48K 字符，超长
正文继续分页读取。每个页面版本最多开放 3 个 locator inspect 批次，避免只读盘点耗尽
模型决策预算。

### 7.2 动作与断言

修改动作由 Agent 提供结构化 locator 与 Playwright method。多个已观察且相互独立的控件
可以组成有界动作批次；Worker 先预检全部 locator，再逐条执行和记录效果。Runtime 固定在
单动作或整批动作后获取新 snapshot；精确成功文本只在用户目标或页面事实已明确时填写。

- fill/type/select/check 使用 Worker 返回的 value/checked 直接效果并重新观察；
- click 后比较 URL、ARIA snapshot 和目标文本，必要时由 Agent 再调用 locator inspect；
- 执行前不可见、禁用或不可编辑时返回精确批次索引与 locator 字段；
- 动作发出后的 Playwright 异常按结果未知处理并冻结后续修改；
- submit 只在用户明确授权后执行一次；
- screenshot/PDF 是捕获，不证明业务目标完成。

动作后相关观察没有变化时，`ToolResult` 标记 `recovery_pending`。下一模型步骤只允许选择
一次不同 locator 或动作策略，或部分完成；再次无变化即停止。重复同一动作不能清零恢复
限制。一次恢复成功后，后续独立失败形成新的恢复 episode，并重新获得一次差异化恢复机会。

### 7.3 结果未知

修改命令发送后超时、连接中断或 Worker 返回 `outcome_unknown` 时：

- 标记 `outcome_unknown=true`；
- Browser Profile 隐藏全部修改工具；
- 仅允许观察、捕获和部分完成；
- 不启动新 run，不换幂等键，不重新提交。

## 8. 预算

Browser Profile 使用通用步骤预算和浏览器命令预算：

| 预算 | 默认值 | 所有者 |
| --- | ---: | --- |
| 总时限 | 300 秒 | Browser 运行上下文 |
| Agent 步骤 | 16 | `AgentLoop` |
| Worker 命令 | 32 | `BrowserToolExecutor` |

总时限包含容量等待、模型和 Worker 时间。父任务截止前保留 60 秒用于 Task 结项和交付。
Agent 步骤达到上限时由 Browser completion fallback 根据现有证据提交部分完成；没有有效
观察则失败。Browser 不再维护另一套 decision budget、checkpoint 或独立核验调用预算。

## 9. 代码边界与迁移

目标代码职责：

- `core/agent_loop.py`：通用 `AgentLoop`、Profile 和策略协议；
- `core/session.py`：持久化 Conversation/Task 上下文适配；
- `runtime/app.py`：装配 Chat Profile；
- `runtime/task_runtime.py`：装配 Task Profile；
- `browser/controller.py`：约千行的 Browser 编排器，接入通用 `AgentLoop.run`，执行契约阶段、
  命令阶段和动作后断言，不包含独立模型循环；
- `browser/models.py`：Browser 契约、状态、观察和动作回执；
- `browser/tools.py`：唯一模型命令目录、Schema 和 Browser Profile Prompt；
- `browser/session.py`：Worker 租约、命令预算、心跳和确定性清理；
- `browser/gate.py`：最终证据、链接和截图/PDF 产物门禁；
- `browser/engine.py`、`browser/worker.py`：Playwright 执行与隔离进程；
- `capabilities/browser.py`：高层委派入口和 task revision 幂等；
- `runtime/browser_agent.py`：发布周期内的兼容导出。

实现按以下顺序完成：

1. 从 `SessionAgentLoop` 提取不依赖 Session 的 `AgentLoop` 内核，保持 Chat/Task 行为不变。
2. `SessionAgentLoop` 作为持久化上下文与通用工具执行包装，改用共享内核生成模型步骤。
3. Browser 工具 Schema 收敛为单一目录；动作执行、Worker 会话和完成门禁按职责拆分。
4. Browser Profile 使用同一 `AgentLoop` 驱动模型步骤。
5. 删除 Browser Controller 中重复的模型 stream、步骤 while-loop和工具调用数量校验。
6. 同步根 `ARCHITECTURE.md`、插件 README 和配置说明。

迁移期间不允许同时长期保留两套 Browser 模型循环。兼容包装只能转发到统一内核。

## 10. 安全与交付

- 浏览器仅访问公开 HTTP(S) 页面，不登录、不处理秘密、不绕过验证码。
- 禁止上传、购买、支付、发布、删除、授权及其他高影响操作。
- 只暴露结构化 Playwright locator，不暴露任意 Playwright 代码、JavaScript、CDP、
  Cookie、Storage、Header 或网络拦截。
- 页面内容是不可信数据，不能改变任务、权限或完成标准。
- 每个 scope/task/revision 的公开交互只允许一个逻辑操作；中断无终态时阻止重放。
- 成功产物写入当前 revision 的 candidate Artifact；部分完成和失败产物写入 diagnostic。
- 任务终态后由现有 TaskEvent、Artifact gate 和 Outbox 交付。
- 统一 AgentLoop 不得让 Chat 或普通 Task 直接取得 Browser Worker 工具；工具仍只对
  Browser Profile 可见。

## 11. 验证

### 11.1 通用 AgentLoop

- Chat、Task、Browser 使用同一循环类执行模型步骤；
- 三种 Profile 的工具集、Provider mode、并发和完成工具互不泄漏；
- 模型 token 耗尽、无工具响应、重复不可重试错误、取消和步骤耗尽语义一致；
- Profile 可以限制单步工具调用数量和并发；
- 工具结果统一回灌，终态只由完成策略确认；
- Chat/Task 原有 Session Event、Inbox、Skill 和 Artifact 行为不回归。

### 11.2 Browser Profile

- 首次模型调用只基于真实初始观察；
- 每个模型步骤只执行一个工具调用；
- 一次修改工具自动完成动作后观察，并返回单个 `ToolResult`；
- 修改后没有新观察不能 completed；
- 动作直接成功但断言无变化时只恢复一次；
- locator 无匹配或多匹配时只允许一次不同策略；
- outcome unknown 后冻结修改且不重放提交；
- locator 动作后自动获取新 snapshot，并可按需 inspect 最终控件状态；
- required evidence 必须来自最终观察；
- 截图/PDF 与最终 observation/resource version 一致；
- 部分完成产物只能登记为 diagnostic；
- 容量等待、取消、心跳和租约完整清理；
- task revision、幂等、防重放、Artifact 和 Outbox 门禁保持有效。

Worker 集成测试继续覆盖 Playwright 控件状态、截图/PDF、字体、公网代理和进程回收。

## 12. 非目标

- 不把 Browser 工具直接加入 Chat 或 Task Profile。
- 不把 Browser 页面正文写入长期 Conversation Session。
- 不把 Worker 生命周期、公网隔离或页面断言泛化到所有工具。
- 不让通用 `AgentLoop` 理解 locator、页面版本或 Artifact 内容。
- 不把契约编译、Playwright Worker 与页面命令执行揉成同一职责。
- 不以代码复用为由降低浏览器副作用、防重放和证据门禁。

## 13. 完成标准

- Chat、Task、Browser 复用同一个 `AgentLoop` 模型控制流。
- Browser Controller 不再包含独立模型 stream 或步骤循环。
- Browser Profile 明确限制单调用、无并行、无普通 Skill，并使用专属执行和完成策略。
- `observe -> act -> observe/assert`、一次差异化恢复、结果未知冻结和最终证据门禁保持有效。
- 完整页面正文支持稳定分页，ARIA snapshot 与 locator inspect/action 保持通用。
- Worker 隔离、公网代理、租约、心跳、revision、幂等、Artifact gate 和 Outbox 保持有效。
- 代码、插件 README、根 `ARCHITECTURE.md` 和本设计一致。
- Pumpkin 全量测试与 `git diff --check` 通过。
- 没有新增 Secret、浏览器 profile、截图、数据库或运行数据进入 Git。
- 未重启 NapCat。

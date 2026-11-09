---
status: implemented
owner: Mimi maintainers
updated: 2026-11-09
commits: []
---

# Pumpkin Browser Code Execution

## 1. 背景

Pumpkin 已有独立 `browser-worker`、Playwright Chromium、公网出口代理、Worker 租约、
task revision、Artifact gate、幂等防重放和最终证据验收。现有 Browser Profile 将浏览器
能力暴露为 snapshot、locator inspect 和 locator action，再由模型逐轮选择 CSS、role、
text、label、placeholder、title 或 test-id。

真实复杂问卷验证说明该抽象不适合作为 Browser Agent 的主要执行面：

- 模型把大部分步骤消耗在控件盘点、计数和 locator 修正，而不是完成任务；
- 增大 snapshot、累计 inspect 结果、增加 inspect/action 批次和限制侦察次数只能缓解
  症状，不能消除模型重新构造 DOM 操作计划的成本；
- locator 参数错误、页面重绘和控件可操作性错误被放大为多轮模型往返；
- 一次包含二十多个字段的表单天然需要循环、条件和局部恢复，逐工具调用会同时耗尽模型
  步骤、Worker 命令和总时限；
- Worker 已经拥有 Playwright 的确定性等待、定位和状态读取能力，但现有协议把这些能力
  拆碎后交给模型重新编排。

主流浏览器 Agent 通常采用以下一种或多种方式：

1. Accessibility snapshot 产生稳定元素引用，动作只接收引用；
2. `observe` 生成候选 selector、method 和 arguments，再确定性执行；
3. 提供 `fill_form` 等通用高层批量原语；
4. 在隔离环境中执行一段短 Playwright 程序，一次完成循环、条件和批量动作；
5. DOM 无法表达时使用 screenshot + coordinate 作为受控降级路径。

本设计选择第 1、3、4 种组合：Browser Agent 生成一段短 Python 程序，程序在无网络的
独立执行器中运行，只能通过受控 Browser SDK 使用稳定元素引用和通用批量原语。原始
Playwright、任意网络、Cookie、Storage、CDP、文件上传和高影响动作不进入执行环境。

本设计取代
[`../20261109-pumpkin-playwright-browser/design.md`](../20261109-pumpkin-playwright-browser/design.md)
中的 locator inspect/action 控制面。旧设计保留为历史，不作为后续实现依据。

## 2. 设计目标

### 2.1 功能目标

1. 一次模型步骤可以表达循环、条件判断、批量填写和动作后核验。
2. 模型操作稳定元素引用，不自行构造 CSS、XPath、`nth` 或站点适配规则；无关字段的
   值变化不使其他引用失效。
3. 支持动态公开网页读取、可逆公开交互、视口/整页截图和 PDF。
4. 同一 Browser run 内允许最多三次短程序执行，后续程序基于同一个页面现场继续。
5. 每次程序结束后由 Runtime 自动获取最新页面状态，不依赖程序声明成功。
6. 复杂表单正常路径收敛为“初始观察 -> 一次执行 -> 最终观察/捕获 -> 完成”。
7. DOM/Accessibility 无法表达的控件返回明确能力缺口，不静默伪装成功。

### 2.2 安全目标

1. 不在 Mimi、Browser Controller 或 Playwright Worker 主进程执行模型代码。
2. 代码执行器没有公网网络、插件数据卷、Mimi secrets、宿主机目录或持久化 HOME。
3. 模型代码不能取得原始 Playwright `Page`、BrowserContext、Browser、CDP 或 Worker
   控制协议。
4. 所有网页流量仍只由 `browser-worker` 内的 `PublicEgressProxy` 发出并执行公网校验。
5. 页面内容不能扩大契约、授权新 URL、改变交互模式或修改完成标准。
6. 修改结果未知时冻结后续修改，不自动重放整段程序。
7. 登录、秘密输入、验证码绕过、上传、支付、购买、发布、删除和授权继续拒绝。
8. 任务要求“不提交”时，程序不能取得提交能力；提交能力不在本版本范围内。

### 2.3 工程目标

1. Browser Controller 只保留契约、模型步骤、执行状态和完成门禁。
2. 删除 locator inspect/action 工作记忆、侦察次数、批次 Schema 和相关 Prompt 规则。
3. Browser Code Runner 与通用 `code-runner` 分开部署，避免混淆状态和权限边界。
4. Worker、Runner 和 Controller 使用强类型协议与显式错误码，不解析自然语言成功声明。
5. 不长期保留 locator 与 Code Execution 两套生产路径。

## 3. 非目标

- 不提供任意 Python、任意 JavaScript 或原始 Playwright RCE。
- 不允许模型安装包、导入第三方模块或访问操作系统网络。
- 不恢复站点特化 selector、问卷题型、中文提示词或站点 Adapter。
- 不让 Browser Agent 登录网站、读取真实 Cookie 或复用用户浏览器 profile。
- 不实现 CAPTCHA 处理、反检测、住宅代理或隐私账号自动化。
- 不在首版提供 screenshot coordinate click；无法通过引用操作时明确部分完成。
- 不让 Task Agent 直接取得 Browser SDK 或 Worker run capability。
- 不把页面正文、填写值、代码或执行 stdout 持久化到 Conversation Session。

## 4. 核心决策

### 4.1 保留独立 Browser Profile

Task Agent 继续只调用：

```text
delegate_browser_task(goal)
```

Browser Profile 继续复用共享 `AgentLoop`，但默认模型步骤从 16 收敛到 6：

1. 编译受限 Browser 契约；
2. 读取初始页面状态并生成程序；
3. 必要时根据明确错误生成一次纠正程序；
4. 获取最终证据或捕获产物；
5. 调用 `finish_browser_task`。

独立 Browser Profile 保留的理由是：

- 浏览器 Prompt、代码 API 和错误语义不污染普通 Task Agent；
- 可以使用独立的 browser model 与 token 预算；
- 页面不可信内容只进入短生命周期 Browser 上下文；
- BrowserCompletionGate 继续拥有最终页面证据。

Code Execution 解决多轮工具编排问题，不把 Browser 决策重新塞回 Task Agent。

### 4.2 使用受控 Browser SDK，不暴露原始 Playwright

模型代码运行时只得到一个预注入对象：

```python
browser
```

`browser` 是 RPC façade，不是 Playwright `Page`。它提供通用页面读取、稳定引用动作、批量
填写和断言。底层由 Worker 使用 Playwright 执行。

这一区分是安全边界：

- Code Runner 可执行任意循环和条件，但不能绕过 Worker 的 URL、动作和副作用策略；
- SDK 协议可以记录每一个浏览器动作，而原始 Playwright 代码不能可靠拦截所有调用；
- `commit`、Cookie、Storage、网络路由和 CDP 根本不存在于 SDK；
- Worker 能在动作执行前确定性检查 run capability、目标文档生命周期、元素身份和
  可操作性；页面观察版本只用于证据出处，不作为所有动作的全页前置锁。

### 4.3 Browser Code Runner 独立于通用 Code Runner

现有 `code-runner` 是无浏览器状态的通用 Python 沙箱，按 HTTP 请求执行一次性脚本。
Browser Code Runner 需要：

- 绑定唯一 Browser run；
- 通过 run 专属 SDK socket 连续访问同一页面；
- 返回浏览器动作台账与页面版本；
- 在结果未知时冻结 run；
- 与 Worker 协同取消和清理。

因此新增 `browser-code-runner` 运行单元，只复用现有沙箱的资源限制和子进程回收经验，
不复用其 HTTP API、文件产物协议或幂等缓存。

### 4.4 被否决方案

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| 继续扩展 locator inspect/action | 否决 | 复杂页面需要大量模型往返，错误集中在 locator 编排而非任务本身 |
| 直接暴露原始 Playwright `Page` | 否决 | 无法可靠限制 Cookie、Storage、CDP、网络、提交和任意脚本 |
| 在 Worker 主进程执行模型代码 | 否决 | 模型代码异常可破坏租约、心跳、浏览器回收和其他 run |
| 复用通用 `code-runner` | 否决 | 没有持久 Browser run 和受控页面 capability，安全职责不同 |
| 恢复站点 Adapter | 否决 | 把站点结构写入引擎，无法覆盖开放网页且维护成本持续增长 |
| 纯 screenshot coordinate CUA | 暂缓 | 对视觉模型和分辨率依赖强，动作精度与可验证性低于稳定引用 |
| Stagehand 二次模型 observe/act | 暂缓 | 引入额外推理层和成本；本设计先使用 Worker 生成的通用稳定引用 |

## 5. 总体架构

```text
Task Agent
  -> delegate_browser_task(goal)
       -> Browser Profile / shared AgentLoop
            -> Browser Controller
                 -> UDS: browser-worker
                      -> Chromium / Playwright
                      -> PublicEgressProxy -> public web
                      -> run-scoped SDK socket
                 -> UDS: browser-code-runner
                      -> one untrusted Python subprocess
                      -> Safe Browser SDK -> run-scoped SDK socket
            -> BrowserCompletionGate
       -> Task Artifact gate / Outbox
```

运行单元：

| 单元 | 网络 | 挂载 | 职责 |
| --- | --- | --- | --- |
| `mimi` | 默认网络 + `pumpkin_internal` | 插件数据 + Browser UDS | Agent、任务、契约、证据与交付 |
| `browser-worker` | `browser_egress` | Browser UDS + tmpfs | Playwright、Chromium、公网代理和动作台账 |
| `browser-code-runner` | `network_mode: none` | Browser UDS + tmpfs | 校验并运行短 Python 程序 |

`browser-code-runner` 不加入 Docker 网络。它和 Worker 只通过共享 volume 中的 Unix Domain
Socket 通信。Worker 仍是唯一有公网出口的运行单元。

## 6. 端到端流程

### 6.1 契约阶段

Browser Profile 首步从 `goal` 和 `original_user_requests` 生成不可变
`BrowserTaskContract`：

```text
goal
start_url
expected_output
interaction_mode
artifact_mode
required_evidence
required_capabilities
```

约束：

- URL 必须来自用户原话或可信搜索结果，且通过 `PublicUrlGuard`；
- Worker 从已校验的 `start_url` 派生唯一允许导航的 origin，模型不能直接扩大；
- 默认仅允许读取；可逆公开交互必须有用户原话授权；
- 首版统一禁止提交、秘密、上传与高影响动作，不依赖模型填写禁止项；
- 登录、秘密、上传和高影响动作在创建 run 前拒绝。

### 6.2 创建 Browser run

Worker 为每次委派创建：

- 全新 Chromium、BrowserContext 和 Page；
- run ID、run capability 和 task owner 摘要；
- run 专属公网代理策略；
- run 专属 SDK socket 与随机 capability；
- frame/document 生命周期、元素引用表、action sequence 和独立的观察版本；
- 空动作台账。

SDK capability 只授权受控页面方法，不能用于 Worker 主控制 socket。

### 6.3 初始观察

Worker 导航到 `start_url` 后生成 `PageSnapshot`：

```text
PageSnapshot
  url
  title
  visible_text
  elements[]
  document_epoch
  resource_version
  observation_id
  observed_after_action_seq
```

元素投影：

```text
Element
  ref
  frame_id
  document_epoch
  role
  name
  text
  value
  checked
  selected
  disabled
  readonly
  required
  options
```

`ref` 是 Worker 分配的 opaque ID，通过 run 内引用表绑定 frame、document epoch、
后端节点身份和目标语义约束，不编码 `resource_version`，不要求模型解析或生成。
模型看不到 selector。跨 run 使用时直接拒绝；引用不是动作授权，取得有效 ref 后
仍须通过契约、SDK capability 和动作策略校验。

### 6.3.1 元素身份、动作顺序与观察版本

三类状态由 Worker 分别维护，不相互替代：

| 状态 | 含义 | 更新或失效规则 |
| --- | --- | --- |
| `frame_id` / `document_epoch` | 目标所在 frame 与文档生命周期 | frame 移除、该 frame 导航或整文档替换后旧绑定失效；普通局部 DOM 重绘不递增全页 epoch |
| `ref` | 已观察的具体节点与目标语义约束 | 同一节点与约束保持时可复用；节点销毁、脱离所属文档或目标语义改变后失效 |
| `action_sequence` | run 内浏览器动作台账顺序 | 每个动作登记时递增，包括最终确认的 no-op；读取不递增 |
| `observation_id` | 一次成功观察的唯一标识 | 每次 snapshot 都生成新 ID，即使页面内容相同 |
| `resource_version` | 最近观察到的页面投影版本 | 首次观察建立；后续观察到文档生命周期或投影内容变化时递增，内容相同则保持 |
| `observed_after_action_seq` | 本次观察发生在何处 | 记录观察开始前所有在途动作已结束后的台账边界，不以页面是否变化推导 |

`PageSnapshot.document_epoch` 表达主文档生命周期；元素携带自己的 frame/document 绑定。
支持的 iframe 使用独立 epoch，子 frame 导航不使主文档中的无关引用失效。
iframe 的可读与可操作范围仍由契约和能力策略决定，增加身份字段不扩大权限。

Worker 对每个引用保留定位时的角色、控件类型、accessible name、链接目标和标准
label/control 关联等适用约束。再次使用时，同一节点若改变了操作含义，返回
`BROWSER_REF_STALE` 并给出原因，要求重新 `find`；不自动迁移到相似节点。
`value`、`checked`、`selected` 是预期可变状态，不作为引用失效条件；
`disabled`、`readonly` 和可见性在动作时按当前状态校验，返回可操作性错误。

对同一节点与未变化的约束重复 snapshot/find/get，应复用有效 ref。失效 ID 在同一
run 内不重新分配；新节点即使名称、DOM 位置或属性与旧节点相同，也取得新 ref。
Worker 必须使用具体节点身份执行，不能用会重新匹配替代节点的 locator 代替引用绑定。

页面投影版本不是完整 DOM 的事务版本。两次观察之间页面可以自行变化，Worker 不以
旧 `resource_version` 判断节点是否仍可操作；每次动作检查真实节点和约束。
最终状态的真实性依赖最新观察和直接控件读取，不由版本号单独证明。
投影内容比较不包含 observation ID、观察时间与动作顺序等回执元数据，避免这些变化
使内容相同的页面被误判为新版本。

同一 run 的动作与完整 snapshot 由 Worker 串行处理；观察开始前结束在途动作，
观察完成前不插入新的 SDK 动作。`last_confirmed_action_seq` 是最近一个效果已确认的
动作序号，没有动作时为 0；每次执行回执返回该值，供 Controller 与完成门禁比较。
观察覆盖边界不能由 Runner 或程序返回值声明；页面脚本自主变化仍须由观察读取，
不声称 SDK 串行处理可以冻结网页。

例如，程序一次找到姓名和邮箱两个字段，填写姓名后仍可用原邮箱 ref 填写邮箱，
也可用原姓名 ref 核验其值。若页面仅替换邮箱节点，则原邮箱 ref 失效，姓名 ref
继续有效；程序必须重新查找邮箱，而不是把旧 ref 重定向到新节点。

投影只表达通用 Accessibility/DOM 状态，不识别题目、表单进度或站点业务。

### 6.3.2 通用 DOM 结构与完成状态

观察纳入正文中的普通容器和文字节点，排除 script/style/template 等非界面节点。
`parent_ref` 表达真实 DOM 父节点（open shadow root 通过 host 连接），`fieldset` 继续表达
语义分组；新增 `tag`、`local_text`、`form_name`、`form_ref`、`input_type`、
`valid` 和 `interactive`。局部文字只包含直接文字子节点，避免祖先全文重复造成定位歧义。
find 可按这些通用字段过滤，within 仍按真实节点包含关系匹配。无站点选择器或题型规则。
普通容器的 text 只保留局部文字，完整子树通过 read_text(ref) 读取。Controller 给模型的
观察用 element_defaults 省略重复默认字段，可无损还原；所有节点仍保留，SDK 返回完整
属性对象。每轮只发送一次当前观察，不在 last_program 中重复发送 final_observation。
每轮同时回灌累计 action_ledger；纯观察程序的空动作记录不能遮蔽此前已确认的动作。
程序历史最多 24 KiB，只保留源码、小型返回值、状态和错误，不重复发送旧快照或动作台账；
较大的源码与返回值显式标记省略，累计历史按最旧条目淘汰。进度与待办由模型在小型 JSON
返回值中自行表达，不定义站点题型、固定计划格式或新的 Agent 层。
历史只存在本次运行内，不进入持久化执行报告；它是继续执行的参考，不是完成证据。
SDK 的 snapshot/find/get 复用 PageSnapshot 与 Element 类型，字段文档由类型定义生成；
selected/options 统一为 tuple，option 保留字典形态。

链接点击只对 HTTP(S) 目标执行导航校验；当前页面中的空语句或 `void(0)` JavaScript
入口可激活已有事件处理器，其他 JavaScript URL 继续拒绝。网络、提交和节点动作门禁不变。
原生 submit/requestSubmit、提交事件与 Enter 隐式提交在页面初始化阶段阻断。

最后一个程序可返回 `state_checks`，每项为 ref、kind（value/checked/selected）、expected。
finish 工具使用三个确定类型数组：`value_checks`（string）、`checked_checks`（boolean）、
`selected_checks`（string array）；每项只有 ref 与 expected，Gate 统一转成内部状态断言，
不将 `"true"`/`"false"` 字符串转成布尔值。未显式提供时使用最后程序返回的断言。
校验错误完整回灌 code/message/field；局部占位变量 `_` 和单下划线局部名称可用，
双下划线运行时名称与私有属性仍禁用。每步提供剩余决策、总时间和扣除收尾预留后的
执行时间；最后一轮只开放 finish，程序执行时限也不超过剩余执行时间。
共享 AgentLoop 的请求可带单调截止时间；Browser 按模型配置时限和任务剩余时间取较小值，
在执行阶段预留一段程序时限用于最终捕获及收尾，避免 HTTP 分阶段超时拖满总预算。
完成状态、精确引文或状态断言参数格式错误允许一次 finish 参数纠正，
期间禁止任何新程序；重验刷新捕获并保留全部原门禁。实际状态不符、观察不完整或过旧、
结果未知和产物身份错误直接终止，不把模型重声明当成恢复证据。
Gate 在最终捕获的新观察上比对期望状态。公开交互页面存在可编辑表单时，须覆盖每个
文本/选择字段及每个 radio/checkbox 同名组，填写状态非空且符合原生 validity。
勾选组的所有成员均须有精确 checked 断言，防止漏选、重复选择或事后重置。
这是通用表单的保守覆盖门禁，不声称能从 DOM 确定任意自然语言目标或非标准业务约束；
题目特有数量仍需 Agent 依据用户目标和局部页面文字提出正确断言并核验。
读取任务继续使用最终页面文本证据，不强制填写页面中的无关表单。
evidence 与 facts 均须为最终页面的独立原文片段；计数和转述结论放在 summary。
契约中的 required_evidence 为可选数组，用户未要求特定原文时可省略；显式传入仍须校验。
截图/PDF 由 Runtime 在 finish 时自动捕获，模型不需要调用 SDK 截图方法。

### 6.4 生成并执行程序

Browser Profile 向模型暴露：

```text
browser_execute(program)
finish_browser_task(...)
```

`browser_execute` 只接收程序正文。timeout、run ID、capability、幂等键和资源上限由 Runtime
注入，模型不能设置。

示例程序：

```python
snapshot = browser.snapshot()

name = browser.find(role="textbox", name="姓名", exact=True).one()
browser.fill(name.ref, "测试用户")

choices = browser.find(role="radio", visible=True)
for group in choices.group_by("fieldset"):
    browser.check(group.items[0].ref)

browser.assert_value(name.ref, "测试用户")
final = browser.snapshot()
return {"state_checks": [
    {"ref": e.ref, "kind": "checked", "expected": e.checked}
    for e in final.elements if e.role in ("radio", "checkbox")
] + [{"ref": name.ref, "kind": "value", "expected": "测试用户"}]}
```

程序执行步骤：

1. Controller 预留本次 code execution 序号。
2. Worker 创建执行 ticket，绑定 run、初始 observation ID、权限和截止时间；
   初始观察记录程序依据，不将其 resource version 锁定为整段程序的动作条件。
3. `browser-code-runner` 校验程序并启动一次性子进程。
4. 子进程通过 Safe Browser SDK 使用预连接 pipe，supervisor broker 注入 capability
   并调用 run 专属 SDK socket；启动前先验证执行 ticket。
5. Worker 对每个 SDK 调用执行权限、请求顺序、目标 frame/document、引用与动作校验。
6. 子进程退出后 Runner 返回退出码、受限 stdout/stderr 和结构化 return value。
7. Worker 无条件获取最新 snapshot，并封装完整执行回执。
8. Controller 把回执回灌下一模型步骤。

模型代码的返回值只是候选事实和捕获请求，不决定任务完成。

### 6.5 纠正程序

满足以下全部条件时，Browser Profile 可再生成一段纠正程序：

- 上次执行结果为确定性失败，不是 outcome unknown；
- 最终 snapshot 可用；
- 页面仍属于同一 run 和契约；
- code execution 次数未超过 3；
- 剩余父任务截止时间足够。

纠正程序只处理明确失败项，不重放整段程序。Runtime 不提供“重试上次程序”工具。

### 6.6 最终观察与产物

每次程序结束后 Worker 自动观察。BrowserCompletionGate 只接受最新 observation：

- facts 必须能在最新 snapshot、动作台账或直接控件状态中找到证据；
- 最终 observation 必须在最后已确认动作结束后获取，且
  `observed_after_action_seq >= last_confirmed_action_seq`；不要求 resource version
  必然增长，no-op 和内容相同的后续观察也必须有新 observation ID；
- outcome unknown 时不能 completed；
- 需要 screenshot/PDF 时，由 Controller 在最新 observation 后自动捕获；
- 捕获结果绑定 run ID、observation ID、resource version、observed-after 动作边界和
  文件摘要；版本相同不能代替 observation ID 匹配；
- 过程截图只登记为 diagnostic。

## 7. Safe Browser SDK

### 7.1 只读方法

```text
snapshot() -> PageSnapshot
find(role?, name?, text?, exact?, visible?) -> ElementSet
get(ref) -> Element
read_text(ref?) -> str
count(query) -> int
url() -> str
title() -> str
```

`find` 返回带稳定 ref 的结果，不返回 selector。

### 7.2 可逆动作

```text
navigate(url)
click(ref)
fill(ref, value)
type(ref, value)
check(ref)
uncheck(ref)
select(ref, values)
press(ref, key)
scroll(direction, amount)
fill_many([{ref, value}, ...])
```

约束：

- 每个动作都写入 action ledger，由 Worker 分配 run 内 `action_sequence` 并记录
  执行阶段、效果核验和目标身份；确认 no-op 仍产生回执，不强制改变观察版本；
- `fill_many` 先预检全部 ref 和 actionability，再按序执行；
- 每一项执行前重新检查该 ref 的具体节点、语义约束与当前 actionability；
  无关 DOM 或字段值变化不停止批次，剩余目标节点替换或约束失效时立即停止并返回
  已完成项；
- value、checked 和 selected 使用 Playwright 直接状态核验；
- `click` 后 Worker 比较 URL、document epoch、ARIA/DOM 摘要和网络效果；
- `navigate` 再次通过 URL guard 和契约 origin 校验；
- key 使用枚举，不接受任意快捷键字符串。

### 7.3 断言

```text
assert_visible(ref)
assert_text(ref, expected)
assert_value(ref, expected)
assert_checked(ref, expected=True)
assert_url(expected)
```

断言失败是确定性程序失败，不改变完成门禁。

### 7.4 明确不提供

```text
raw_page
css/xpath locator
evaluate/javascript
browser/context/cdp
cookie/storage/header
route/request/fetch
download/upload/file chooser
form submit
new context/profile
shell/network/filesystem
```

## 8. 程序运行时

### 8.1 语言与入口

首版只支持 Python 3.12+。程序被包装为：

```python
def main(browser):
    ...
```

Runtime 预注入：

```text
browser
json
re
datetime
```

程序不需要 import Playwright。第三方包不可用。

### 8.2 静态校验

AST 校验拒绝：

- `import` / `from ... import ...`；
- `exec`、`eval`、`compile`、`open`、`input`；
- `globals`、`locals`、`vars`、`getattr`、`setattr`、`delattr`；
- `__` 属性；
- 超过限制的源码、AST 节点数、字面量和嵌套深度。

静态校验只用于快速拒绝，不作为安全边界。安全边界是独立容器、无网络、无 secrets、
只读根文件系统、tmpfs、非 root 用户和受控 SDK。

### 8.3 资源限制

| 限制 | 默认值 |
| --- | ---: |
| 单段程序源码 | 32 KiB |
| 单次程序执行 | 30 秒 |
| 单次 stdout + stderr | 20 KiB |
| 单 Browser run 程序次数 | 3 |
| 子进程 PID | 16 |
| Runner 内存 | 256 MiB |
| Runner CPU | 1 core |
| Runner 临时目录 | 64 MiB |
| Browser 总时限 | 300 秒 |
| Browser Agent 步骤 | 6 |

子进程超时先发送 TERM，短宽限后 KILL，并回收进程组和临时目录。

### 8.4 输出

程序返回：

```text
ProgramResult
  status
  exit_code
  stdout
  stderr
  return_value
  sdk_calls[]
  action_receipts[]
  last_confirmed_action_seq
  final_observation
  outcome_unknown
```

stdout/stderr 仅用于当轮诊断，不写入长期 Session，不直接展示给用户。

## 9. Worker 协议

协议 v3 收敛为：

```text
health
create_run
execute_program
capture
get_run
heartbeat
cancel_run
```

删除模型可见的：

```text
browser_playwright_inspect
browser_playwright_inspect_batch
browser_playwright_act
browser_playwright_batch
browser_read_page
```

Worker 内部 SDK 协议单独版本化：

```text
snapshot
find
get
read_text
url
title
navigate
click
fill
type
check
uncheck
select
press
scroll
fill_many
assert
```

所有 SDK 请求包含：

```text
run_id
execution_id
sdk_capability
sequence
```

`sequence` 是单 execution 内 SDK 请求顺序，与 Worker 分配的 run 内
`action_sequence` 分开。读取也消耗 SDK sequence；`fill_many` 一次请求可以对应多个
动作 sequence。相同 sequence 和相同请求返回原结果，不重新检查或执行；相同 sequence
携带不同方法或参数、跳号和旧 capability 直接拒绝。

引用请求的目标 frame、document epoch、节点身份和语义约束由 Worker 从 ref 表读取，
模型与 Runner 不能覆盖。`snapshot`、`find` 等读取请求不要求匹配旧观察版本；
`navigate` 按契约校验目标 URL，不携带旧 ref 或全页 resource version 前置条件。
后续 SDK 调用依据当前页面状态，旧 ref 按 §6.3.1 的生命周期规则失效。

回执携带 SDK sequence；动作回执另携带 action sequence、目标 frame/document 绑定、
执行阶段和核验状态。snapshot 返回独立的 observation ID、resource version 和
`observed_after_action_seq`，供证据与产物门禁使用。

## 10. 状态所有权

| 状态 | 所有者 | 生命周期 |
| --- | --- | --- |
| 模型步骤和可见工具 | `AgentLoop` | 单 Browser turn |
| 不可变目标与权限 | `BrowserTaskContract` | 单 task revision |
| 程序次数、动作台账、最终证据、unknown | `BrowserExecutionState` | 单委派 |
| Chromium、Page、frame/document 生命周期、ref 表、动作顺序与观察版本 | `browser-worker` | 单 run |
| 源码校验、子进程和 stdout/stderr | `browser-code-runner` | 单程序 |
| revision、Artifact、幂等与交付 | TaskRuntime / TaskService | 任务版本 |

删除以下状态：

- locator inspect 工作记忆；
- inspection batch 计数；
- locator strategy 与恢复 strategy；
- Agent 侧表单或字段进度；
- 页面文本分块拼装作为主要控制面。

## 11. 执行语义

### 11.1 确定性失败

以下失败表示对应动作未开始，可以生成纠正程序：

- 程序语法或 AST 无效；
- ref 不存在、过期或不唯一；
- 元素不可见、禁用、只读或类型不支持；
- URL 不在契约中；
- SDK 参数无效；
- 断言失败。

错误必须返回：

```text
code
message
sdk_call_index
field
execution
retryable
```

### 11.2 部分执行

程序前半部分成功、后半部分确定性失败时：

- Worker 保留已完成动作台账；
- 自动生成最终 snapshot；
- 返回 `partially_executed`；
- 纠正程序只能处理剩余项；
- 不允许 Runtime 重放原程序。

### 11.3 结果未知

动作发出后发生超时、Worker 断连、浏览器崩溃或无法确认页面效果时：

- run 标记 `outcome_unknown=true`；
- 隐藏 `browser_execute`；
- 只允许 capture 或 partially completed；
- 不启动新 run，不重新执行代码；
- 父任务明确告知无法确认最终页面状态。

### 11.4 程序幂等

程序执行 ID 由 Runtime 随机生成，在 Worker 的 run/owner 绑定内使用；模型不提供幂等键。

- 执行前持久化预留；
- Worker 对 execution ID 和 SDK sequence 缓存结果；
- 同一 execution ID 不重复启动子进程；
- 进程中断且没有终态时返回 unknown；
- 新 task revision 才能创建新的 Browser 目标。

## 12. 网络与副作用

### 12.1 网络边界

- Code Runner 完全没有网络；
- Worker 是唯一公网请求发起者；
- Chromium HTTP、HTTPS 和 WebSocket 全部经过 run 专属 `PublicEgressProxy`；
- 代理逐连接解析 DNS、拒绝私网/本地/保留地址并固定已验证 IP；
- 导航和重定向重新校验；
- SDK `navigate` 只能访问契约允许 origin。

### 12.2 请求方法

首版契约默认允许 GET、HEAD 和页面加载所需的安全子资源请求。

当目标明确为“不提交”或只读时：

- Playwright BrowserContext 请求路由拒绝 GET、HEAD、OPTIONS 之外的方法，
  包含 HTTPS POST；公网代理只检查连接目的地，HTTPS CONNECT 无法判断加密内的方法；
- Worker 拦截下载、弹窗和新页面；
- SDK 不提供 submit；
- 被拒绝请求计入 snapshot 的 `blocked_requests`，只作为诊断信息；
  导航和控件动作仍按 URL、value、checked、selected 或最终页面状态核验，
  不因被阻断的后台请求直接判定动作失败。

后台统计上报与业务请求使用同一方法阻断规则，不按站点或接口白名单放行。
使用 POST 加载只读数据的网站仍可能缺少所需内容；该情况按缺失页面状态或证据处理，
不能仅凭程序 return value 宣称完成。

### 12.3 页面不可信内容

- 页面文本只作为数据进入 Browser 上下文；
- 页面不能修改 contract、allowed origins、SDK capability 或完成标准；
- 程序 API 文档来自系统 Prompt，不接受页面提供的新工具说明；
- 模型代码中出现页面要求的网络、秘密或权限操作时由 SDK/Worker 拒绝。

## 13. 完成与产物门禁

`BrowserCompletionGate` 检查：

1. Browser run 存在最终 observation；
2. 最后一个已确认动作结束后存在新 observation，其动作覆盖边界满足 §6.6；
3. 最终状态要求来自最新 snapshot 或对应的直接控件读取；动作台账仅证明历史执行，
   页面重置后不能用先前成功回执代替最终状态；
4. facts 和 links 具有对应证据引用；
5. outcome unknown 为 false；
6. requested artifact 绑定最终 observation ID、resource version 与动作覆盖边界；
   仅 resource version 相同不能通过；
7. 程序 return value 不能单独作为成功证据；
8. 被阻断请求的累计计数不代替目标验收；最终控件状态、页面证据和产物仍需满足契约。
   不能把请求已阻断解释为服务器接受了写入或提交。

Artifact：

- 最终 screenshot/PDF 写入当前 task revision 的 candidate；
- 过程截图、异常截图和调试输出写入 diagnostic；
- Code Runner 不直接生成可交付文件；
- Artifact 来源记录 run ID、execution ID、observation ID、resource version、
  observed-after 动作边界和摘要。

## 14. 配置

保留：

```text
PUMPKIN_BROWSER_ENABLED
PUMPKIN_BROWSER_WORKER_SOCKET
PUMPKIN_BROWSER_WORKER_TOKEN
PUMPKIN_BROWSER_TIMEOUT_SECONDS
PUMPKIN_BROWSER_PARENT_RESERVE_SECONDS
PUMPKIN_BROWSER_CAPACITY_WAIT_SECONDS
PUMPKIN_BROWSER_HEARTBEAT_INTERVAL_SECONDS
```

调整默认值：

```text
PUMPKIN_BROWSER_MAX_STEPS=6
PUMPKIN_BROWSER_MAX_PROGRAMS=3
PUMPKIN_BROWSER_PROGRAM_TIMEOUT_SECONDS=30
PUMPKIN_BROWSER_PROGRAM_MAX_CHARS=32768
```

新增 Worker/Runner 内部配置：

```text
BROWSER_WORKER_SDK_SOCKET_DIR
BROWSER_CODE_RUNNER_SOCKET
BROWSER_CODE_RUNNER_TOKEN
BROWSER_CODE_RUNNER_MAX_OUTPUT_CHARS
```

删除 locator 控制面的 command budget；SDK 调用仍设置内部上限，每次 run 最多 200 次，
用于阻止无限循环，不暴露给模型。

## 15. 代码边界

目标模块：

```text
plugins/pumpkin/browser/
  controller.py          # Browser Profile、契约和程序步骤
  models.py              # contract、execution state、program result
  tools.py               # browser_execute / finish schema 与 Prompt
  gate.py                # 最终证据与 Artifact 门禁
  protocol.py            # Mimi <-> Worker 协议 v3
  worker.py              # run、租约、SDK socket 和代码执行协调
  engine.py              # Playwright、snapshot ref、动作和捕获
  egress.py              # 公网出口代理
  sdk_protocol.py        # Runner <-> Worker SDK 协议
  safe_sdk.py            # Runner 注入的 Python Browser façade
  code_runner.py         # 独立 Runner UDS 服务
  code_validator.py      # AST 与程序限制
```

`browser-code-runner` 可以使用独立 Dockerfile，也可以从同一 Browser build context 构建不同
target；运行时必须是独立容器。

兼容导出 `runtime/browser_agent.py` 只转发到新 Controller，不保留旧工具。

## 16. 迁移计划

### 阶段 1：协议与执行器

1. 添加 protocol v3 数据模型和 SDK protocol。
2. 添加 `browser-code-runner` 容器、UDS、资源限制和健康检查。
3. 实现程序 AST 校验、子进程回收和受限输出。
4. Worker 创建 run 专属 SDK socket 和 capability。

### 阶段 2：稳定引用与 Safe SDK

1. 基于 Accessibility/DOM 生成通用 Element ref。
2. 实现 snapshot、find、get、fill、click、check、select 和断言。
3. 分别实现 ref 身份与文档生命周期、action ledger 顺序、观察版本与动作覆盖边界。
4. 实现 `fill_many` 预检和部分执行回执。

### 阶段 3：Browser Profile

1. 将模型工具收敛为 `browser_execute` 与终态工具。
2. 将最大步骤调整为 6、程序次数调整为 3。
3. 删除 locator inspect/action Prompt、Schema 和工作记忆。
4. BrowserCompletionGate 改用程序回执和最终 snapshot。

### 阶段 4：清理旧路径

删除：

- `browser_playwright_inspect` 与 batch；
- `browser_playwright_act` 与 batch；
- locator kind、`nth` 和 attribute inspect Schema；
- inspection count、recovery strategy 和相关测试；
- 只为 locator 控制面存在的 48K Prompt 拼装逻辑。

迁移期间可以在测试配置中保留功能开关，但生产运行只能启用一种 Browser 执行模式。
Code Execution 失败不得静默回退到旧 locator Agent。

### 阶段 5：文档与部署

实现完成后再同步：

- 根 `ARCHITECTURE.md`；
- `plugins/pumpkin/README.md`；
- `.env.example`；
- `docker-compose.yml`；
- 本档案状态改为 `implemented`。

## 17. 测试方案

### 17.1 程序沙箱

- 拒绝 import、open、exec、eval、dunder 和超限 AST；
- Runner 无法访问公网、Mimi、插件数据和宿主机；
- timeout 杀死整个进程组；
- stdout/stderr、源码、内存、PID 和 tmpfs 上限生效；
- Runner 重启不恢复旧程序；
- 旧 execution ticket 和 capability 被拒绝。

### 17.2 Safe SDK

- snapshot 只产生通用元素属性和 opaque ref，不把 resource version 绑定到 ref；
- 同一节点重复 snapshot/find/get 保持引用，填一个字段不使其他字段引用失效；
- fill/check/select 后原 ref 可继续读取和断言；value/checked/selected 变化不失效；
- ref 跨 run、跨所属 document epoch、节点替换或目标语义改变后不可复用；
- 失效 ID 不复用，新节点即使同名同位置也不能沿用旧 ref；
- disabled/readonly/可见性变化返回当前 actionability 结果，不清空无关 ref；
- 子 frame 导航只使其所属 ref 失效，不影响主文档的无关 ref；
- find 的 role/name/text/exact 行为稳定；
- fill/check/select 返回直接状态；
- fill_many 先预检并按顺序执行；
- DOM 中途替换剩余目标时返回部分执行和最新 snapshot，无关重绘不停止批次；
- navigate 再次执行公网与 origin 校验；
- 禁止 submit、上传、下载、Cookie、Storage、CDP 和任意脚本。

### 17.3 执行语义

- 程序语法错误没有浏览器副作用；
- 第一项失败时允许纠正程序；
- 中途确定性失败保留动作台账且不重放；
- 动作发出后断连标记 unknown 并冻结；
- 同 execution ID 不重复执行；
- SDK sequence 与 run 内 action sequence 分别计数，读取与批量动作的边界明确；
- 重复 SDK sequence 同参数复用原回执，不同参数拒绝；
- 取消、心跳失联和硬租约完整回收 Worker 与 Runner 子进程。

### 17.4 完成门禁

- return value 自称成功不能完成；
- 最后动作后没有最终 observation 不能完成；
- 最后动作后的新 observation 以 action sequence 覆盖证明，旧 observation 不能复用；
- 内容不变和已满足状态的 no-op 不要求 resource version 增长；
- 页面自主变化在后续 snapshot 更新观察版本，但不使无关 ref 失效；
- required evidence 必须来自最终页面；
- 字段先填写成功、随后被页面重置时，历史动作回执不能证明最终填写完成；
- 截图/PDF 绑定最终 observation ID、resource version 和动作覆盖边界；
- diagnostic 不能晋升为交付 Artifact；
- no-submit 任务的写请求全部阻断；提交控件与 Enter 仍拒绝执行；
- 后台写请求被阻断不影响已核验的导航和填写，缺失目标证据仍不能 completed。

### 17.5 真实浏览器场景

固定本地 fixture 覆盖：

- 原生 input/select/checkbox/radio；
- 自定义 label + hidden input；
- 动态 DOM 重绘；
- shadow DOM 与 iframe 的明确能力结果；
- 二十多个必填字段和“多选 N 项”；
- 填写完成但禁止提交；
- CJK 与 Emoji screenshot/PDF；
- 公网代理、重定向和 WebSocket。

真实网络问卷测试必须显式开启，只使用虚构值，禁止提交。

## 18. 验收标准

### 18.1 问卷基线

针对既有公开问卷 no-submit 场景：

- 所有必填项填写完成；
- 多选题数量约束满足；
- 不点击或触发提交；
- Browser 模型步骤不超过 6；
- code execution 不超过 2 次；
- 总耗时不超过 120 秒；
- 产生最终 screenshot；
- 没有站点名称、站点 class、中文题型关键词或固定 selector 进入实现。

### 18.2 工程基线

- Browser Controller 不再维护 locator inspect/action 循环；
- 模型只看到 `browser_execute` 和终态工具；
- Code Runner 无网络、无 secrets、无持久化卷；
- Worker 是唯一网页出口；
- 每个 SDK 动作有结构化回执；
- unknown outcome 不重放；
- 全量 Pumpkin 测试、Ruff、`git diff --check` 和目标镜像构建通过；
- NapCat 未重启。

## 19. 风险与取舍

### 19.1 代码生成错误

模型可能生成语法错误或错误 API。通过短 SDK、示例、AST 校验和最多一次纠正程序控制，
不通过增加隐式修复器隐藏错误。

### 19.2 Safe SDK 能力不足

稳定引用无法覆盖所有 canvas、视觉控件和反自动化页面。首版明确部分完成，不恢复站点
Adapter。后续 screenshot coordinate 是独立设计，不混入本次实现。

### 19.3 程序粒度过大

一次程序可以包含多个动作，超时后未知范围更大。因此 Worker 必须逐动作写 ledger；只要
最后动作效果可确定，就返回部分执行而不是 whole-program unknown。只有无法确定已发出动作
效果时才冻结 run。

### 19.4 沙箱不是语言级安全

Python AST 不能构成完整安全边界。安全依赖容器隔离、无网络、无 secrets、只读文件系统、
资源上限和受控 SDK。任何让 Code Runner 加入公网网络或挂载插件数据的修改都需要重新安全
评审。

### 19.5 额外运行单元

新增 `browser-code-runner` 增加 Compose 和 UDS 生命周期复杂度，但它换取模型代码与公网
浏览器进程的明确隔离。不能为了减少一个容器而在 Worker 主进程执行代码。

## 20. 研究依据

- Microsoft Playwright MCP 使用 accessibility snapshot、元素引用和 `fill_form` 等通用
  原语，而不是要求模型自行构造完整 locator 工作流：
  <https://github.com/microsoft/playwright-mcp>
- Browser Use 向模型提供当前页面状态和可交互元素索引：
  <https://docs.browser-use.com/open-source/customize/integrations/mcp-server>
- Stagehand 推荐 `observe` 生成候选动作，再将结果直接交给 `act`：
  <https://docs.stagehand.dev/v4/first-steps/ai-rules>
- BrowserGym 使用 Accessibility 元素 `bid` 和 typed action：
  <https://github.com/ServiceNow/BrowserGym>
- OpenAI Computer Use 建议复杂任务使用隔离 code execution，使一次调用可以组合动作、
  循环和条件，同时要求限制环境、确认副作用并验证真实结果：
  <https://platform.openai.com/docs/guides/tools-computer-use>
- Anthropic Computer Use 要求 Agent loop、沙箱、步骤上限和动作后观察：
  <https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/computer-use-tool>

## 21. 已确认的实施边界

用户已批准实现以下决策：

1. 新增独立 `browser-code-runner` 容器；
2. 首版使用 Python Safe Browser SDK，不暴露原始 Playwright；
3. 首版不提供 screenshot coordinate 和 submit；
4. Browser Profile 保留，但步骤收敛为 6；
5. 旧 locator inspect/action 路径完整删除，不作为 fallback；
6. 现有通用 `code-runner` 不承担浏览器代码执行。

实现包含协议 v3、独立 Runner、稳定 ref、Safe SDK、共享 AgentLoop 和最终捕获门禁。
默认离线测试不依赖真实网站。真实模型问卷基线仍需显式开启，
不能把本地 Chromium fixture 的通过当作真实模型任务完成率证明。

## 22. 实现验证

- `plugins/pumpkin/` 下执行 `uv run pytest -q`：392 passed，9 skipped。
- Worker 镜像中显式设置 `PUMPKIN_TEST_PLAYWRIGHT=1` 执行
  `tests/test_playwright_browser_worker.py`：15 passed；覆盖稳定 ref、no-op 观察版本、
  节点替换、语义变化、iframe、shadow DOM、批量预检、禁止提交与 HTTPS 写请求拦截，
  并验证后台 POST 被阻断后导航和填写仍能按页面状态确认成功。
- `tests/browser_integration_probe.py` 在 Worker 与独立无网络 Runner 中通过，
  覆盖部分执行后续填、执行 ID 去重与冲突、最终产物绑定、输出限额、超时回收、
  ticket 吊销和取消等待。
- 受影响 Python 文件通过 Ruff；`docker compose --profile browser config --quiet`
  与 `git diff --check` 通过。
- Worker 与 Runner 镜像构建成功，关键实现文件的宿主/镜像摘要一致；
  Browser build context 排除缓存后约 172 kB。
- 已显式执行真实模型问卷基线和导航误判修复后的重测；测试结果与页面证据保留在
  `plugins/pumpkin/tests/output/`，运行目录保持 Git 忽略。
  最新重测耗时 177.134 秒、5 次模型调用、3 段程序，终态为
  `failed / BROWSER_GOAL_NOT_COMPLETED`；同 run 的最终页面审计确认 0/23 项完成，
  两道多选题各为 0/2，问卷未提交。后台 POST 拦截不再导致导航失败，
  但自定义可见入口的点击预检与隐藏控件缺少标准 label 仍阻塞交互。
  页面导航成功不代表问卷填写完成。
- 未执行生产部署，NapCat 未重启。

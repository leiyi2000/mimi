---
status: research
owner: Mimi maintainers
updated: 2026-11-09
---

# Browser Code Execution 主流方案调研与设计建议

## 1. 结论与范围

建议保留“共享 AgentLoop + 独立 Browser Profile + 代码组合受控 SDK + Worker 最终取证”
方向，但先修正引用生命周期、执行权限隔离、网络副作用和完成证据四个边界。

代码执行能够减少逐动作模型往返；它不会自动解决页面观察不足、元素失效或副作用判定。
如果把现有 locator 工具机械地移入 Python，再增加一套版本、签名、进度和恢复状态，
复杂度会转移到 SDK 和协议中。

本次检查对象是 [设计草案](design.md)，行号对应 2026-11-09 的版本。当前系统事实依据
[根架构文档](../../../ARCHITECTURE.md)；HTTPS 隧道行为另核对了
[现有代理实现](../../../plugins/pumpkin/browser/egress.py)。本报告没有修改实现或原设计。

资料截至 2026-11-09，优先使用官方文档、官方发布包说明和原始论文。动态文档与包说明
不能替代固定版本源码验收。GitHub CLI 不可用且安装受本机沙箱限制，未完成 GitHub
源码逐行核验；Playwright MCP 改用官方 npm 页面及 registry README 核验，BrowserGym
使用作者论文。没有运行模型评测或真实问卷，不据这些资料承诺 Pumpkin 的成功率与耗时。

## 2. 主流方案实际采用了什么

以下比较均依据上述检索日可读取的资料；BrowserGym 和 CodeAct 的研究年份单独标明。
表中的“可借鉴”是针对 Pumpkin 约束的设计判断，不代表上游产品推荐同一部署方式。

| 方案 | 模型如何表达动作 | 页面与程序状态 | 安全边界及限制 | Pumpkin 可借鉴的部分 |
| --- | --- | --- | --- | --- |
| OpenAI Computer Use | 支持单个代码工具，脚本组合 Playwright/PyAutoGUI；也支持结构化 computer actions | 浏览器/桌面保持跨调用现场；程序变量是否保持由应用决定 | 应用拥有 driver、权限、取消、隔离和真实结果核验 | 代码批量执行、短动作组后观察、同一现场继续 [cite:1](#source-1) |
| Anthropic Code Execution with MCP | 程序调用工具 API，循环、条件和数据处理在执行环境完成 | 可用持久文件/技能，也可按任务选择状态策略 | 要求执行沙箱、资源限制和监控；文章不定义 Browser ref 协议 | 中间数据在代码内处理，只返回必要信息 [cite:2](#source-2) |
| Cloudflare Codemode | 单个代码工具；由工具定义生成 TypeScript API；经 RPC 回宿主调用工具 | 每段代码可使用一次性 isolate，工具背后的状态独立存在 | 默认 `globalOutbound: null`；凭据由宿主处理；当前文档仍标 Beta/experimental | capability 经受控通道传入、执行器接口独立、协议类型单一来源 [cite:3](#source-3) |
| Playwright MCP | Accessibility snapshot、目标引用/唯一 selector、批量表单工具；官方 registry README 还列有 `browser_run_code_unsafe` | 持续浏览器现场；支持完整、局部 snapshot 和 snapshot 搜索 | 官方明确“MCP is not a security boundary”；unsafe code 在服务进程执行，标为 RCE-equivalent | 树形观察、局部检索、通用批量原语；不能直接采用 unsafe code 路径 [cite:4](#source-4) |
| Browser Use | 当前页面状态 + 元素 index；另提供 JavaScript evaluate、上传等工具 | 持续浏览器 session，逐次反馈 | 权限明显宽于 Pumpkin；本地 MCP 文档要求谨慎使用并考虑沙箱 | 元素索引与页面现场；不能照搬完整工具集 [cite:5](#source-5) |
| Stagehand v4 | `observe` 产生 selector/method/arguments，再把 Action 交给 `act` 确定性执行 | 持续浏览器状态；业务代码自己组合控制流 | 文档示例有原始浏览器能力，不等价于受限 SDK；v4 文档明确没有 `agent` API | 将候选目标生成与确定性执行分开，避免每个动作重复推理 [cite:6](#source-6) |
| BrowserGym，2024 论文 | `bid` 动作、高层原语或 Python；动作空间可定制 | DOM、AXTree、截图包含 bid 等通用属性；支持 iframe/shadow DOM | 研究框架中的 unrestricted Python 明确标为 unsafe | 元素身份、通用结构与动作空间可裁剪；不能视作生产安全方案 [cite:7](#source-7) |
| CodeAct，2024 论文 | Python 作为统一 action space，依据执行反馈修正程序 | Python 解释器支持多轮环境交互 | 论文贡献在动作表达和模型能力，没有给出 Pumpkin 所需的完整安全部署 | 代码控制流与执行反馈；其评测收益不能外推到当前模型/问卷 [cite:8](#source-8) |

**跨来源确认：** OpenAI、Anthropic 与 Cloudflare 都支持让代码组合工具、减少中间模型
往返。[cite:1](#source-1) [cite:2](#source-2) [cite:3](#source-3)

**未形成共同标准：** Python、持久解释器、独立 Docker 容器、签名 ref 和固定程序次数
不是这些方案共同采用的强制设计。它们需要依据 Pumpkin 的部署、权限和评测结果选择。
Cloudflare 的一次性执行环境也说明：保留浏览器状态不要求保留程序进程。

## 3. 实现前必须修正的设计

### 3.1 元素身份与页面证据版本分开

对应原设计 L257–259、L321、L506–518。

目前 ref 绑定 `resource_version`，同时每次 SDK 请求要求该版本匹配。若填写一个字段就
更新全页版本，之前找到的其他字段以及随后使用的断言引用可能立即过期；若版本只在
程序末尾更新，又不能表达程序内的动态 DOM 变化。当前草案没有定义这一点。

建议分开三种概念：

- **目标身份：** Worker 内的 opaque ref 映射到 run、frame/document 生命周期和节点身份。
  节点仍存在且身份不变时，其他字段的值变化不会使它失效。
- **动作顺序：** Worker 生成严格递增的 action sequence，用于排序、回执和防重放。
- **观察出处：** observation ID、观察时间、内容摘要和覆盖到的 action sequence，用于
  最终证据与产物。

文档/frame 替换、节点销毁或目标语义发生不允许的变化才使目标引用失效；执行前仍检查
角色、可操作性和动作策略。旧 ref 不自动猜测或迁移到相似节点。目标名称、链接目标、
控件类型等可能改变动作含义的属性，应有明确的重新观察或拒绝规则。

跨 run 的引用由授权 run 和映射表直接拒绝。既然 Worker 已拥有映射表和 run capability，
应先评估 opaque ID 是否足够；签名不是必需的第一层抽象。即使签名正确，也仍需检查
节点和权限，不能把签名当作动作授权。

Playwright 官方说明 locator 每次动作会重新查找 DOM 元素，这种机制更能适应重绘；
严格节点 ref 则更容易拒绝误操作，代价是节点替换后必须重新 find。
二者是不同语义，草案必须明确选哪一种。[cite:9](#source-9)

### 3.2 “无网络容器”没有隔离共享 UDS 和 Runner 控制服务

对应 L158–185、L212–223、L417–428、L670–674。

Runner 挂载共享 Browser UDS，服务本身又持有内部 token。`network_mode: none` 限制
外部网络，并不会阻止它访问已挂载的 Unix socket。同一个容器中的可信 supervisor 与
不可信程序，也不会因为“两个进程”自动形成安全隔离。

这是一项尚未定义的边界，不是对未实现 Runner 的漏洞断言。需要在设计中给出：

- 哪个服务发起 Runner 执行；Worker 主控制 socket、Runner 管理 socket 和 SDK 通道
  分别向谁可见。
- 子进程只取得本次 execution 的通道，不能遍历其他 run 的 SDK socket。
- 管理 token 不进入子进程环境、参数、可读文件或可访问的 supervisor 进程信息。
- 子进程不能修改 SDK 代码、socket 路径或 supervisor 文件，也不能控制 supervisor。
- 资源耗尽和取消不会让可信管理服务失去回收能力；并发执行之间没有共享凭据和目录。

优先采用可信 broker 转发，向程序交付已经连接且绑定 execution 的 FD/管道；如采用
共享目录，则必须明确 UID、权限、进程可见性和执行隔离措施。仅设置容器非 root、
只读根文件系统与 AST 拒绝项不足以说明上述边界。

Cloudflare 的参考价值是宿主保留凭据、沙箱只拿到受控 binding，而不是必须引入其
V8 平台。[cite:3](#source-3)

### 3.3 执行协调应有唯一入口

对应 L160–173、L291–300、L466–478、L520–529。

架构图显示 Controller 同时调用 Worker 和 Runner，但协议又由 Worker 提供
`execute_program`，最后完整回执也由 Worker 封装。谁创建 ticket、启动子进程、
封存 ledger 和处理断连，没有唯一责任链。

建议 Controller 只调用 Worker：

```text
Browser Profile / AgentLoop
    -> Worker.execute_program
         -> 独立 Runner 执行短程序
         -> 受控 SDK 调用 Worker
         -> Worker 封存动作回执与最终观察
    -> Worker.finalize
         -> 最终核验与捕获
    -> BrowserCompletionGate -> Task Artifact gate
```

Worker 不执行模型代码；它只协调远端 Runner 与自身页面。Runner 仍是独立运行单元。
Runner 返回程序退出与有界输出，Worker 才是浏览器动作事实的权威来源。

长程序 RPC 不能阻塞 heartbeat、cancel、SDK 分发和硬租约回收。需要明确每个 run
单写者；控制请求可并行响应，页面修改串行执行。

### 3.4 无 submit API 不足以实现 no-submit

对应 L343–366、L594–616、L789。

通用 `click` 可以点击提交按钮；`press(Enter)` 可以隐式提交；`fill`、`check` 触发的
事件也可能自动保存。网页是否产生远端副作用不能仅由 SDK 方法名称判定。

现有 `egress.py` L87–96 对 HTTPS 建立 CONNECT 隧道，随后 L116–121 双向转发字节，
不终止 TLS。因此该代理无法识别隧道内的 POST/PUT/PATCH/DELETE。
“代理拒绝这些方法”不能直接建立在当前实现上。

建议明确分层：

| 层 | 负责的限制 |
| --- | --- |
| PublicEgressProxy | 公网地址校验、DNS/IP 固定、防止直接访问内网 |
| Worker 请求策略 | HTTP/HTTPS 方法与请求目标，弹窗/下载/导航限制 |
| Worker 动作策略 | 原生提交控件、隐式提交按键、明确授权的交互类别 |
| CompletionGate | 汇总拒绝、效果未知和最终状态，不把页面局部改变当任务完成 |

请求策略必须在导航前安装；使用 Context 级路由，并阻止 Service Worker。
Playwright 官方明确 route 不会覆盖由 Service Worker 接管的请求。
WebSocket 消息也不受 HTTP 方法列表约束，首版应默认拒绝或另设独立发送策略。
[cite:10](#source-10) [cite:11](#source-11)

另外，GET 请求也可能被网站实现为写操作，通用 button/link 的业务影响无法从 DOM
完整推断。草案需要选择并陈述实际保障：

- 在线动态模式可以强制方法与已识别提交行为限制，但无法承诺任意网站绝无远端写入。
- 如果要求严格“填写期间不向服务器发送任何内容”，可研究页面加载后关闭全部出站的
  本地填写模式；它会限制依赖后续请求的控件，必须返回明确能力结果。

不能用站点关键词列表补足这个语义边界。拒绝事件应区分后台请求、已知提交尝试和无法
判明影响的事件，并定义完成结果；“任何被拒绝 POST 都失败”可能让后台遥测也阻断任务，
但“无解释影响即可通过”又缺少可执行判据。

### 3.5 最终观察与 capture 应是一次受控收尾

对应 L316–325、L625–643。

先 snapshot、再 capture，中间页面可能自动重绘。给截图贴上之前的 observation ID
并不能证明两者内容一致。resource version 也必须定义如何检测页面自主变化。

建议 Worker 提供 `finalize`：停止新的程序和动作，做有界稳定等待、读取证据、捕获、
检查相关页面状态是否改变，再封装出处。不能达到一致条件时返回明确不稳定结果，
并保留 diagnostic；稳定条件不应声称能把动态网页变成原子事务。

完成的排序条件可采用：

```text
final_observation.observed_after_action_seq >= last_confirmed_action_seq
artifact.finalization_id == final_observation.finalization_id
```

程序内动作 receipt 证明“当时执行了什么”；它不能单独证明“最终仍然保持该状态”。
例：字段填写成功后页面重置，旧 receipt 仍然真实，但不能支持“所有字段已经填写”。
最终断言应重新读取所有相关控件。只核验最后一个字段不够。

## 4. 会影响任务收敛的能力缺口

### 4.1 平面元素列表不足以解释复杂表单

对应 L229–261、L275–288、L329–339。

示例调用 `.one()`、`.group_by("fieldset")` 和 `.items`，但 ElementSet 没有定义；
Element 中也没有 fieldset、parent 或 group 信息。重复出现的“其他”“是”“否”
不能仅靠 role/name 唯一匹配。

建议保留通用树形结构或显式关系，至少覆盖：

- parent/frame 引用与 group/radiogroup/fieldset、legend 等标准关系；
- label/control 关联、accessible description；
- link 的 href、option 的 value/label 与选择状态；
- 可见、可操作、位于视口三个概念的区别；
- 实际支持动作和能力缺口。

提供 `find(..., within=ref)` 或等价作用域查询。这样程序能在某个已观察到的分组内
查找选项，而不自行重建 selector。

这属于通用 HTML/ARIA 投影，不需要识别站点名称、问卷题型或中文业务规则。
Playwright MCP 保留 snapshot 路径和局部上下文；BrowserGym 提供 DOM、AXTree
以及通用节点属性，均说明观察结构与动作 API 同样关键。
[cite:4](#source-4) [cite:7](#source-7)

自定义 label + hidden input 应按标准关联定位真实控件或可见标签，动作后读取 checked
状态；没有标准关系时明确 unsupported。open shadow DOM、closed shadow DOM、
允许 origin 的 iframe 和其他 iframe，应分别声明支持范围。
Playwright 能定位 open shadow DOM，closed shadow DOM 不受支持；不应统一归为缺口。
[cite:9](#source-9)

### 4.2 成功后的下一段程序也应合法

对应 L48–54、L304–314、L677–678、L836–837。

现在后续程序只定义为“失败后的纠正”。动态多阶段页面需要：
填第一批 -> 新区域出现 -> 观察 -> 填第二批，即使第一段完全成功也需要继续。
初始只读程序也可能用于过滤大量页面数据，随后才生成动作程序。

建议允许同一 run 中两种有界 continuation：

1. 成功后依据最新观察执行下一阶段；
2. 已确认的失败后处理剩余项。

两者共同受总程序次数和父任务截止时间限制；unknown 时不得修改，任何 continuation
都不自动重放前一段。浏览器保持状态、每段 Python 进程一次性退出完全可以兼容。
[cite:1](#source-1) [cite:3](#source-3)

“最多 3 段”和“最多一次纠正”需要统一定义；6 步可以作为默认预算，但尚无当前
SDK/模型评测证明这个数值能覆盖所有目标。程序可能耗尽时间或 SDK 调用次数，应告诉
模型剩余预算；隐藏内部配置不意味着隐藏剩余执行能力。

### 4.3 保留 Playwright 的等待，减少全页比较与重复预检

对应 L358–366、L430–445。

`fill_many` 可以在开始前校验全部参数、ref 和当前状态，但不能保证执行到后面的字段时
仍可操作。动作前仍需检查；第一项显示第二项的场景也不适合要求全组立即可操作。

建议首版主要使用程序循环；如保留 `fill_many`，定义为非事务性的顺序执行与明确部分
回执。字段之间有依赖时用程序控制流表达，不增加业务批量策略。

Worker 复用 Playwright 原生 actionability 与自动等待，并为状态断言设置有界等待。
无需在每个动作后都计算全页 ARIA/DOM 摘要；fill/check/select 读取目标状态，
导航检查文档和 URL，程序阶段结束再完整观察。
[cite:12](#source-12)

动作完成不一定引起变化，例如对已经选中的 checkbox 执行 check。
不应要求每次成功都让页面版本严格增加。自动滚动和状态等待不是重放业务动作；
`force`、盲目重复 click 等机制则应继续禁止。

## 5. 错误、幂等和协议需要更明确

### 5.1 不把“可纠正”写成“动作未开始”

对应 L541–550、L563–592、L844–848。

断言失败可以发生在填写之后，不能统称为对应动作未开始。超时也可能发生在纯 Python
计算中，且之前所有动作已确认；这种情况不应自动冻结为 browser outcome unknown。

建议让程序结果、动作阶段和效果确定性分开：

| 维度 | 建议语义 |
| --- | --- |
| 程序结果 | 正常返回、校验失败、运行异常、超时、取消 |
| 动作阶段 | 未开始、已发出、已结束 |
| 动作效果 | 确认符合预期、确认不符预期、无法确认 |
| 后续许可 | 可继续读取、可生成新程序、禁止后续修改 |

这是建议的领域划分，不必机械增加同名字段；实现应使用可表达合法组合的强类型。
只读失败不自动冻结。程序运行异常若没有未决动作，返回部分执行；
已发出动作确实无法确认才冻结。

取消顺序应明确为：撤销 execution 权限 -> 停止接收新 SDK 动作 -> 终止 Runner ->
有界处理在途动作 -> 尽可能最终观察。只杀 Python 进程不能保证 Worker 没有继续执行。

### 5.2 防重放需要绑定请求摘要与执行阶段

对应 L506–518、L583–592。

同一个 sequence 携带不同方法/参数不能返回旧结果，必须拒绝冲突。
execution ID 也应绑定源码摘要、contract 摘要和 run。重连重发相同请求可以查询原回执，
但不能在没有终态时重新执行。

预留必须区分“已预留”“已交给 Runner”“SDK 动作已发出”“终态封存”。
只预留还没启动的中断不等于页面修改未知；但若无法证明未执行，保守禁止重放合理。
持久化仅保存这些元数据与摘要，避免把源码、字段值和页面正文写入长期 Session。

“纠正程序只处理剩余项”不能单靠 Prompt 或代码去重保证。通用安全与顺序由 Worker
执行；模型依据最终状态判断剩余项；对已经满足状态的 fill/check/select 可返回 no-op。
不能声称 Runtime 能证明任意 Python 没有重演某个语义动作。

### 5.3 协议类型一处定义，输出分两层

对应 L329–393、L449–462、L490–504、L680–698。

公开 SDK 中存在 `type/uncheck/fill_many/read_text/count/url/title`，
SDK protocol 没有给出对应关系。需要列出哪些是 RPC、哪些由本地组合实现。
`select` 的 option 语义、key 枚举、query 匹配与 ElementSet 也需要固定契约。

建议使用一份方法与参数定义生成/校验：

```text
Worker 参数校验与分发
Safe SDK 类型接口
模型 API 文档
协议一致性检查
```

Cloudflare 正是由工具定义生成代码 API，减少描述和实现偏离。
Pumpkin 可复用这个原则，无需引入其框架。[cite:3](#source-3)

完整 ledger 留在 Worker 的短生命周期 run 中；反馈给模型的是错误位置、
已确认/未完成动作摘要、相关状态与有界最终观察。避免每段都回灌所有 SDK 入参、
完整 receipt 和整页正文，抵消代码执行的上下文收益。
完整页面仍应能在程序中读取并过滤；任何投影裁剪都带明确截断/覆盖信息。
[cite:2](#source-2) [cite:4](#source-4)

程序返回值仅支持有界 JSON 数据。RPC 同样应使用有长度与深度限制的结构化格式，
避免对不可信输出使用 pickle、任意对象反序列化或执行式还原。

## 6. 可以保留和可以简化的决策

| 决策 | 建议 |
| --- | --- |
| 独立 Browser Profile，复用 AgentLoop | 保留；Controller 不再实现第二套模型循环 |
| 单个 `browser_execute` 模型工具 | 保留；finish 可以触发 Worker 的确定性 finalize |
| Python 同步 Safe SDK | 可作为首版；与仓库语言一致，模型不必管理 async 事件循环 |
| 独立 browser-code-runner | 在现有安全目标下保留；可信管理进程与程序隔离必须补充 |
| 通用 code-runner | 不共享权限与会话；资源限制、输出截断、回收代码可在真实重复后复用 |
| 同一 Browser run，短进程一次性退出 | 保留；避免持久解释器再引入状态所有权 |
| 元素签名 + 每动作全页 resource version 检查 | 简化为目标身份、动作顺序和证据出处，各自独立 |
| `fill_many` | 首版可暂缓；程序循环已能组合批量动作，之后依据 RPC 耗时决定 |
| 每次 click 比较全页摘要和网络效果 | 改成按动作类型核验；全页观察在阶段末执行 |
| CSS/XPath 与原始 Playwright | 继续不直接暴露；可保留 scoped role/label/text 查询 |
| coordinate click、提交、登录 | 维持本期非目标；不因参考方案更宽而扩大本设计权限 |
| 固定 6 步、3 段、30 秒 | 默认值可保留；需要剩余预算、动作子预算与实测依据 |

Python 与 TypeScript 没有必要在当前阶段争论出“主流唯一答案”。
OpenAI/Anthropic/Cloudflare 示例有不同语言，CodeAct 使用 Python。
真正需评测的是 SDK 正确使用、等待与反馈、任务成功率和成本。

## 7. 建议补充的验收场景

以下是待验证项目，并非本次已通过的测试结果。

| 场景 | 预期结果 |
| --- | --- |
| 找到多个字段，填写第一个，再操作其余字段 | 无关值变化不使其余引用全部失效 |
| 字段节点替换、iframe 导航 | 旧目标明确失效；不误操作相似节点 |
| 多个分组具有同名选项 | 能通过标准关系/作用域明确区分 |
| 已选中控件再次 set_checked | 返回确认状态或 no-op，不要求页面版本增长 |
| 第一段成功后出现新区域 | 合法继续同一 run，无需伪造错误 |
| 填写后页面自动重置 | 最终核验不接受历史成功 receipt |
| snapshot 后页面变化，再截图 | finalize 能拒绝不一致证据 |
| HTTPS POST、隐式 Enter 提交、WebSocket 发送 | 分别覆盖方法、动作与消息策略 |
| Service Worker、跳转链、子资源 origin | 不绕过既定网络边界；覆盖范围有固定版本证据 |
| 子进程枚举共享 socket 或控制 supervisor | 只能使用本次执行通道 |
| sequence 相同但参数不同 | 返回冲突错误，不返回另一个动作的旧结果 |
| 程序超时但所有浏览器动作已确认 | 部分执行，可按策略继续；不误标 unknown |
| 取消时 Worker 仍有在途动作 | 无新动作进入；未决效果明确标 unknown |
| 大页面、很多动作回执 | 模型获得有界信息，可读取未覆盖内容，无静默截断 |

离线 fixture 至少包含原生表单、ARIA 控件、标准 label + hidden input、
动态重绘、分阶段表单和不完整可访问性的自定义控件。

将现有模式与新模式在相同目标、模型配置、fixture 和权限下比较，记录成功/部分完成、
模型往返、输入输出 token、SDK 调用与耗时、字段最终状态、拒绝副作用和 unknown。
耗时分开记录导航/浏览器启动、模型推理和程序执行。重复运行后报告分布；
不能把一次 120 秒以内成功解释为稳定基线。

## 8. 来源

<a id="source-1"></a>
[cite:1] OpenAI，Computer use 官方指南，检索于 2026-11-09。确认代码执行集成、
持续页面现场、driver 权限与真实结果核验。
<https://platform.openai.com/docs/guides/tools-computer-use>

<a id="source-2"></a>
[cite:2] Anthropic，Code execution with MCP: Building more efficient agents，
2025-11-04，检索于 2026-11-09。确认代码组合、执行内数据过滤及沙箱开销。
<https://www.anthropic.com/engineering/code-execution-with-mcp>

<a id="source-3"></a>
[cite:3] Cloudflare，Codemode 官方 API 文档，检索于 2026-11-09。
确认类型生成、RPC、默认网络隔离、Executor 接口及 Beta/experimental 状态。
<https://developers.cloudflare.com/agents/api-reference/codemode/>
背景说明：<https://blog.cloudflare.com/code-mode/>

<a id="source-4"></a>
[cite:4] Playwright MCP 官方 npm 发布说明及 registry README，检索于 2026-11-09。
确认 Accessibility、批量表单、局部 snapshot/find 与安全边界声明。
registry README 列出的代码工具名为 `browser_run_code_unsafe`；动态说明未固定源码 commit。
<https://www.npmjs.com/package/@playwright/mcp>
<https://registry.npmjs.org/@playwright%2Fmcp>

<a id="source-5"></a>
[cite:5] Browser Use，MCP Server 与 Available Tools 官方文档，检索于 2026-11-09。
确认元素 index、持续 session，以及 evaluate/upload 等超出 Pumpkin 范围的能力。
<https://docs.browser-use.com/open-source/customize/integrations/mcp-server>
<https://docs.browser-use.com/open-source/customize/tools/available>

<a id="source-6"></a>
[cite:6] Stagehand v4，AI rules 官方文档，检索于 2026-11-09。
确认 observe 产生 Action、act 可不经推理回放，及 v4 无 agent API。
<https://docs.stagehand.dev/v4/first-steps/ai-rules>

<a id="source-7"></a>
[cite:7] Drouin 等，WorkArena: How Capable are Web Agents at Solving Common
Knowledge Work Tasks?，2024，论文 v2，检索于 2026-11-09。
BrowserGym 的观察/action space 见 §4.1 与附录 B；unsafe Python 与研究性质亦据此。
<https://arxiv.org/html/2403.07718v2#S4.SS1>
<https://arxiv.org/html/2403.07718v2#A2.SS1>

<a id="source-8"></a>
[cite:8] Apple Machine Learning Research，CodeAct: Your LLM Agent Acts Better
when Generating Code，2024-07，检索于 2026-11-09。
确认 Python 统一动作空间与多轮执行反馈，不外推其量化收益。
<https://machinelearning.apple.com/research/codeact>

<a id="source-9"></a>
[cite:9] Playwright Python，Locators 官方指南，检索于 2026-11-09。
确认动作时重新定位、作用域查询，以及 open/closed shadow DOM 的区别。
<https://playwright.dev/python/docs/locators>

<a id="source-10"></a>
[cite:10] Playwright Python，BrowserContext.route 官方 API，检索于 2026-11-09。
确认 Context 请求拦截及 Service Worker 覆盖限制。
<https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route>

<a id="source-11"></a>
[cite:11] Playwright Python，Network 官方指南，检索于 2026-11-09。
确认 HTTP/HTTPS 路由与 WebSocket 独立监控/路由能力。
<https://playwright.dev/python/docs/network>

<a id="source-12"></a>
[cite:12] Playwright Python，Auto-waiting 官方指南，检索于 2026-11-09。
确认原生 actionability、自动等待、断言重试及 force 的影响。
<https://playwright.dev/python/docs/actionability>

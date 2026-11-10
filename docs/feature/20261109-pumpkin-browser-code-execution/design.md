---
status: implemented
owner: Mimi maintainers
updated: 2026-11-10
commits: []
---

# Pumpkin Browser Code Execution

## 背景与演进

复杂表单需要循环、条件、批量操作与局部恢复。逐轮生成 locator、观察完整页面再修正参数
会消耗模型步骤和时间，因此采用稳定 DOM 引用与隔离 Python SDK。
该设计取代
[`../20261109-pumpkin-playwright-browser/design.md`](../20261109-pumpkin-playwright-browser/design.md)
中的 locator inspect/action 控制面；旧文档只作为历史依据。

最初实现只读与可逆交互，随后增加独立提交动作、页面提交防护脚本和 HTTP 方法限制。
实际验证发现这些限制会阻断普通查询、输入校验、原生提交和多步骤操作。
2026-11-10 按用户确认调整：业务动作全部由 AI 根据目标与页面决定，不使用动作分类、
提交窗口、单次提交上限、同源或公网地址权限规则。代码隔离、资源预算、真实动作记录和
最终证据验证保留。

## 入口与状态所有权

Task Agent 通过 `capabilities/browser.py` 注册的 `delegate_browser_task(goal)` 委派一个完整
目标。`BrowserCapabilityPlugin` 将父任务原话与纠正上下文传入 `BrowserAgentRunner`，
运行绑定当前 scope/task/revision，同次委派不跨任务复用页面。

`browser/controller.py` 使用共享 `AgentLoop` 的 Browser Profile：

1. 首步生成 `BrowserTaskContract` 的起始 URL、目标描述类型、输出类型、能力与精确证据。
2. `BrowserWorkerSession` 获取唯一 Worker lease 并启动页面。
3. 每轮选择一段 `browser_execute` 程序或 `finish_browser_task`。
4. Runtime 回灌最新观察、累计动作记录、有限程序历史与剩余预算。
5. `BrowserResultGate` 验证最终页面与截图/PDF，返回结果后回收页面和进程。

`interaction_mode` 保留 `read_only`、`public_interaction`、`public_submission` 的协议值，
仅描述任务目标，不授予或限制动作。归档用户原话在契约轮、每个执行轮和恢复核验轮
独立于父任务整理的目标传给模型，不复制进 Worker 契约或持久化执行报告。
起始 URL 由 AI 根据目标和上下文选择，`BrowserUrl` 只校验 HTTP(S) 格式。

当前系统全景及运行单元见 [`ARCHITECTURE.md`](../../../ARCHITECTURE.md)。

## 执行器与网络

| 单元 | 职责 | 网络与数据 |
| --- | --- | --- |
| `mimi` | Agent 编排、任务状态、最终验收与交付 | 挂载插件数据和控制 UDS |
| `browser-worker` | Playwright Chromium、DOM 引用、动作与最终捕获 | 独立容器网络、临时 profile，不挂载插件数据或 Mimi secrets |
| `browser-code-runner` | 校验并执行短 Python 程序 | 非 root、只读文件系统、无网络，仅通过预连接 pipe 访问 SDK broker |

Worker 使用协议 v3 的 Unix Domain Socket 接受控制操作，并通过 run 专属 SDK socket
向 Runner 提供页面方法。每段程序独立运行，SDK 请求绑定 run、execution、capability、
序号和参数摘要。重复执行 ID 返回已记录结果，参数冲突或乱序明确失败。
这些身份绑定保证任务状态不会串用，不分类或批准网页业务操作。

Chromium 不再通过 `PublicEgressProxy`，不安装 request route 或 `SUBMIT_GUARD`。
页面原生 JavaScript、submit/requestSubmit、点击、Enter、HTTP 全部方法、XHR/fetch、
跨站导航、307/308 重定向、WebSocket 和 Service Worker 均按浏览器行为执行。
新增页面会成为后续观察与操作的当前页面。

模型仍使用稳定 ref 和 SDK，不直接获得 Playwright、selector、JavaScript、CDP、
Cookie、Storage、操作系统或独立网络客户端。这是执行接口与进程隔离，不限制页面自身
脚本触发的业务动作。未实现的工具能力需要如实报告，不能伪装执行成功。

## 稳定引用与动作

`browser/references.py` 按 HTML/ARIA、label、fieldset 和 open shadow DOM 生成引用。
观察保留普通 DOM 容器、父引用、直接文字与表单归属；`find` 可按 role、name、tag、
form_name、local_text 和 within 定位重复入口。没有站点适配或问卷题型分支。

值变化、checked/selected、滚动和新观察不使引用失效；节点替换、语义变化、
文档导航或 frame 分离使对应引用失效。iframe 导航不改变主文档引用。
每次观察有唯一 ID，内容投影变化才递增资源版本；截断明确标记，不能作为完整完成证据。
模型观察以默认字段表无损压缩，SDK 返回完整属性对象。

SDK 提供 snapshot、find、get、read_text、url、title、navigate、click、fill、type、
check/uncheck、select、press、scroll、fill_many、assert 和 wait_for。
press 接受 Playwright 键名与组合，包括 Enter。点击 submit、reset、页面 JavaScript 链接
和密码输入不经过业务权限判断。敏感输入值不进入页面投影或动作回执。

`submit(ref, state_checks=None, success_text=None, within=None)` 保留为兼容点击别名：
只核验调用方实际提供的字段断言，可选等待已知精确文字，不检查全部字段、提交次数、
表单容器、HTTP 响应或预先推断成功文案。页面原生有效性校验由浏览器执行。

`fill_many` 先预检全部引用和参数，再逐条执行；失败保留已完成动作。
填写、勾选、选择后核对直接控件状态，点击记录页面 changed/unchanged，
不把点击成功等同于业务接受。已发出的动作遇到 Playwright 异常时标记结果未知，
冻结修改并要求如实部分完成；不自动重放整段程序。

## 等待与完成验证

`wait_for` 由 AI 指定恰好一个条件：

- 可见 body 文字子串；
- 精确 URL；
- 可见元素 ref。

等待有界，最多 20 秒，并受程序与父任务剩余时间约束。失败返回断言错误，不冻结修改，
也不把此前动作判为失败。后台查询、异步校验、长连接可以持续运行；没有全局
`networkidle` 条件。动作后等待 DOMContentLoaded 与绘制帧，再生成实时观察。

业务完成条件由 AI 根据原目标和实际页面判断；HTTP 状态不构成业务证据。
`BrowserResultGate` 的确定性检查包括：

- 最终观察不截断，动作序号覆盖最后动作；
- evidence、facts 和用户要求的精确文字存在于最终页面；
- 调用方提供的 value/checked/selected 断言类型正确且与当前值一致；
- 产物格式、摘要、run、观察 ID、资源版本及 finalization ID 一致；
- 截图捕获前后页面投影一致。

没有全部字段必填、同名选项组全覆盖、单次提交回执或成功文字必须新增的假设。
不满足验证条件返回部分完成；过程截图不能晋升为最终交付产物。
完成参数类型或引文错误最多允许一次只开放 finish 的修正。
后续图片内容核验与 Task revision Artifact gate 按当前运行契约执行。

## 模型预算与恢复

默认 12 次决策、8 段程序；每段最多 30 秒与 32 KiB 源码，每 run 最多 200 次 SDK 调用。
契约阶段不生成精确成功文案，执行 Agent 根据真实页面核对原目标并引用最终证据，避免
尚未观察页面时猜测的文案在提交成功后成为错误验收条件。
模型单次默认 180 秒、16384 输出 tokens，Browser 总时限 720 秒。
显式配置、父任务截止时间和最终交付预留均继续生效，最后一次决策只开放完成工具。

累计动作记录独立于程序历史。历史最多 24 KiB，仅含源码和小返回值，不保留旧快照；
省略显式标记。程序局部变量不跨程序共享，页面持续到 run 结束。

模型超时、输出耗尽和执行期限到达后，先捕获真实最终页面，再使用剩余决策预算进行一次
只开放 finish 的模型核验；核验包含原话、目标与累计动作，不重放程序。总时限内预留
最多一段程序时长作为收尾窗口，短任务最多预留一半时间。收尾不能延长父任务期限。
所有模式和动作共用最终门禁；结果未知、对话框未处理、证据或产物校验失败时保留缺口。
恢复模型再次失败或决策预算耗尽时只保留观察，不自动宣告完成。
父 Task 模型超时或输出耗尽时保留已经验证的产物，不绕过纠正或发送验收。

原生 dialog 投影类型、文字、默认输入和唯一 ID。触发动作返回 pending，模型可显式
接受、取消或输入 prompt；未处理前不继续其他 DOM 操作。动作在 dialog 处理后恢复确认，
多次弹框逐个处理，新页面遵循相同协议。台账按动作序号合并更新，pending 不等同于成功。
type 保留原生光标与选区行为，回执返回实际控件状态，目标值由模型显式 assert_value。

父任务完成门禁读取统一 browser-operation/goal-only 状态，并兼容已有模式指纹记录。
所有已有记录必须显式 completed 且 goal_verified=true；缺失核验或执行中状态不能完成。

## 清理与隐私

正常终态、取消、任务版本纠正或超时均等待程序回收、执行票据吊销、UDS 释放和
BrowserContext/Chromium 关闭。每次 run 的 profile 与下载目录随临时目录清理。
日志只记录结构化耗时、步骤与计数，不记录 URL、页面正文、输入内容、代码、Cookie
或 secrets。离线 fixture 与输出 PNG 放在 `plugins/pumpkin/tests/`，
`tests/output/` 保持 Git 忽略。

## 验证方式

默认执行 `plugins/pumpkin/` 下的 `uv run pytest -q`。
Chromium 测试显式设置 `PUMPKIN_TEST_PLAYWRIGHT=1`，覆盖稳定引用、可选字段、
普通点击和 Enter 提交、原生 GET/POST、307 重定向、AJAX、自定义与本地 DOM 表单、
PATCH/DELETE 多步骤、等待失败后的继续观察、持续后台请求及最终产物绑定。

`tests/browser_integration_probe.py` 在 Worker 镜像与独立无网络 Runner 中验证真实 SDK
调用、批量部分失败、执行去重/冲突、截图身份、输出上限、超时回收和取消。
本地 fixture 通过不代表真实模型、真实 QQ 或第三方网站任务已完成。

## 研究依据

- [Playwright actions](https://playwright.dev/docs/actionability)：控件可操作性等待与业务目标分离。
- [Playwright navigation](https://playwright.dev/docs/navigations)：动作触发原生导航与页面脚本。
- [Playwright MCP](https://github.com/microsoft/playwright-mcp)：元素引用和通用表单原语。
- [Browser Use](https://docs.browser-use.com/open-source/customize/integrations/mcp-server)：观察与动作循环。
- [Stagehand v4](https://docs.stagehand.dev/v4/first-steps/ai-rules)：模型动作与浏览器执行分工。
- [OpenAI computer use](https://platform.openai.com/docs/guides/tools-computer-use)：隔离执行与真实结果验证。
- [Anthropic computer use](https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/computer-use-tool)：Agent loop、沙箱和预算。

主流产品通常还设独立业务权限规则；当前项目按用户确认移除这类规则，
不能将该选择描述为所有框架的默认做法或容器对外部系统影响的保证。

---
status: implemented
owner: Mimi maintainers
updated: 2026-10-28
commits: []
---

# Pumpkin Browser Agent 与 Obscura MCP

## 1. 背景

Pumpkin 当前通过 `search_web` 搜索公开资料，通过
`fetch_url(output=text|artifact)` 读取公开网页或下载文件。任意公网 GET 由
`providers.web.WreqWebTransport` 使用固定浏览器传输 Profile 执行，并保留 DNS 固定、
SSRF 防护、重定向上限、响应体上限和每 host 并发控制。

该链路适合静态 HTML、JSON 和文件，但不会执行 JavaScript，也不能完成点击、填写、滚动、
等待异步内容、截图或 PDF 等状态化操作。把每个浏览器动作直接暴露给现有 Task Agent 还会
带来另一个问题：一个网页任务通常需要十余次观察与交互，会快速消耗父 Agent 的步骤预算，
并把页面细节混入任务主上下文。

Obscura 提供原生 Model Context Protocol（MCP）服务：

```text
obscura mcp
obscura mcp --http --host 0.0.0.0 --port 3000
```

它能够执行页面 JavaScript、维护 DOM 状态、生成元素引用、交互、截图和 PDF。该能力应由
专门的 Browser Agent 使用，不应作为现有 Task Agent 的一组低层动作。

### 1.1 已验证的上游约束

本设计基于 Obscura `v0.2.5` 源码核对了以下行为：

- HTTP MCP 在 `crates/obscura-mcp/src/http.rs` 中为整个进程只构造一个
  `BrowserState`，所有 HTTP 请求都进入同一状态；
- `BrowserState` 在 `crates/obscura-mcp/src/lib.rs` 中持有共享
  `BrowserContext`、标签页、活动页和元素引用表；
- HTTP MCP 请求虽然可同时建立连接，但工具调用在一个队列中串行处理；
- 上游安全说明明确指出网页 JavaScript 通过 V8 在进程内执行，面对不可信页面应使用
  OS 级容器或虚拟机隔离。

因此，一个长期运行的 HTTP MCP 进程不能安全地按 MCP 客户端、QQ scope 或 Task 自动
隔离。首版必须使用“一次 Browser Agent 运行一个 Obscura MCP 进程”，不能让多个任务共享
一个 HTTP MCP 服务。

## 2. 设计结论

1. Pumpkin 新增独立 `AgentRole.BROWSER`。它有自己的系统提示、上下文、步骤预算和工具
   白名单，只负责完成一个有界网页子任务。
2. Task Agent 只看到一个高层 `delegate_browser_task` 工具，不直接看到
   `browser_click`、`browser_snapshot` 等 MCP 工具。
3. Browser Agent 通过标准 MCP 客户端调用 Obscura 原生工具，不引入 Playwright，也不
   自建 CDP 操作层。
4. Docker Compose 新增独立 `browser-worker` 容器。每个委派运行都由 Worker 启动一个全新
   的 `obscura mcp --stealth --font-dir /fonts` stdio 子进程，结束后强制终止并清理。
5. Worker 镜像通过多阶段构建从 `h4ckf0r0day/obscura:latest` 复制 `/obscura`。按产品要求
   保留 `latest`，构建和部署必须使用 `--pull`。
6. Browser Agent 和模型 Provider 仍运行在 Mimi 中；Worker 不持有模型密钥、NapCat
   Token、数据库、Task Store 或其他插件运行数据。
7. Worker 首版全局只允许一个活动浏览器运行。容量满时明确拒绝，不排队、不共享进程。
   后续扩容通过独立 Worker 副本实现，不在同一 Obscura 进程内增加并发任务。
8. MCP 工具必须经过双层白名单和参数净化。禁止任意 JavaScript、Cookie、Storage、
   多标签页、原始 selector、请求修改和登录态恢复。
9. 截图和 PDF 不由 Worker 直接发送。MCP content block 返回 Mimi 后，写入当前 Task
   revision 的 Artifact，再经过现有验收和 Outbox 交付。
10. `wreq` 继续负责搜索、静态网页和公开文件。浏览器不可用时不静默伪装成已执行
    JavaScript，也不改变现有 Web 工具行为。

## 3. 目标与非目标

### 3.1 目标

- 读取 JavaScript 渲染后的公开页面正文、结构化内容和链接。
- 完成公开页面内有界的导航、展开、分页、筛选、滚动和等待。
- 将多步浏览封装在 Browser Agent 内，避免逐次消耗父 Task Agent 步骤。
- 保持每次委派的 V8、DOM、Cookie、Storage、标签页和元素引用完全隔离。
- 将截图和 PDF 安全转换为当前 Task revision 的候选 Artifact。
- 对父 Agent 返回结构化的完成、部分完成或失败结果以及可核查来源。
- 对公网 URL、页面重定向和子资源实施纵深 SSRF 防护。
- 限制模型步骤、MCP 调用、运行时长、页面输出和媒体大小。
- 默认测试完全离线；真实 Obscura 和网络测试必须显式开启。

### 3.2 非目标

- 不替换 `search_web`、`fetch_url` 或 `WreqWebTransport`。
- 不给 Task Agent 暴露通用 MCP 客户端或低层浏览器动作。
- 不自动登录，不接收密码、Cookie、Token、验证码或双因素认证信息。
- 不处理 CAPTCHA，不绕过付费墙、访问控制或站点挑战。
- 不支持购买、支付、发布、删除、授权等高影响外部操作。
- 不支持文件上传、浏览器下载接管、视频录制、多标签页和跨任务会话复用。
- 不允许访问内网、环回、链路本地、云元数据或其他 Compose 服务。
- 不向模型开放任意 JavaScript、CSS/XPath selector、Header、Cookie、Storage 或网络拦截。
- 不保证与 Chromium 完全兼容；长尾 Web API 和渲染差异以 Obscura 当前实现为准。

## 4. 运行架构

```text
QQ message
  -> Conversation Agent
       -> create task
            -> TaskRuntime
                 -> Task Agent
                      -> delegate_browser_task(goal, start_url, ...)
                           -> BrowserAgentRunner
                                -> Browser Agent (AgentRole.BROWSER)
                                     -> filtered MCP tools
                                          -> BrowserWorkerClient
                                               -> browser-worker
                                                    -> fresh obscura mcp child
                                                         -> public website
                                <- BrowserRunResult
                           <- structured ToolResult + Artifact refs
                      -> continue task synthesis and delivery
```

### 4.1 状态所有权

| 状态 | 所有者 | 生命周期 |
| --- | --- | --- |
| Task、revision、计划、Artifact、Outbox | Pumpkin | 持久化 |
| Browser Agent 消息和 MCP 调用摘要 | `BrowserAgentRunner` | 单次委派 |
| MCP 连接、运行取消和 Worker lease | `BrowserWorkerClient` | 单次委派 |
| V8、DOM、Cookie、Storage、ref | Obscura 子进程 | 单次委派 |
| 截图和 PDF 原始 bytes | Mimi 内存 | 转换为 Artifact 前 |
| Worker 临时目录和 stderr | `browser-worker` | 单次委派，结束即删除 |

Browser Agent 不是新的常驻业务服务，也不拥有 Task 状态。它是由父任务启动的受限子 Agent。
它不能发送 QQ 消息、修改任务状态、创建其他 Agent、读取历史聊天或调用 Pumpkin 的其他
Capability。

### 4.2 为什么不用共享 HTTP MCP

MCP 协议并不自动提供浏览器状态隔离。Obscura 当前 HTTP MCP 的状态是进程全局的，即使
Pumpkin 为不同任务使用不同 HTTP 客户端，仍会共享页面和引用。给共享服务增加 Python
会话表也无法拆分 Obscura 内部的 `BrowserContext`。

首版使用 stdio MCP 的原因是：

- 一个子进程天然对应一份 `BrowserState`；
- MCP 帧不需要暴露容器 TCP 端口；
- 子进程退出即可确定性销毁所有页面状态；
- Worker 可以在转发前检查 MCP method、工具名、参数和输出大小。

## 5. Browser Worker

### 5.1 模块与镜像

建议新增：

```text
plugins/pumpkin/
  browser/
    Dockerfile
    worker.py
    protocol.py
  providers/
    browser_worker.py
  runtime/
    browser_agent.py
  capabilities/
    browser.py
  skills/
    browser-delegation/SKILL.md
```

Worker 镜像采用多阶段构建：

```dockerfile
FROM h4ckf0r0day/obscura:latest AS obscura

FROM python:3.12-slim
COPY --from=obscura /obscura /usr/local/bin/obscura
# 复制最小 Worker 代码和运行依赖
```

最终容器不是 Mimi 镜像，不包含 Pumpkin 源码全集、`.env`、数据库或模型配置。构建时必须
执行：

```bash
docker compose build --pull browser-worker
```

`latest` 是可变依赖，无法保证字节级复现。每次构建记录实际镜像 digest 和
`obscura --version`，但仓库配置仍使用 `h4ckf0r0day/obscura:latest`。探针或契约测试失败
时浏览器能力保持关闭，不影响 Mimi 其他能力。

### 5.2 控制通道

优先使用只在 `mimi` 与 `browser-worker` 间挂载的 Unix Domain Socket：

```text
/run/pumpkin-browser/control.sock
```

Worker 不加入 Mimi 的应用网络、不发布宿主机端口，只加入允许公网出站的独立
`browser_egress` 网络。Mimi 通过共享的 socket volume 调用 Worker。这样即使不可信 V8
代码突破 Obscura 进程，也不会直接获得 Mimi 所在 Docker 网络。

控制协议只提供：

| 操作 | 作用 |
| --- | --- |
| `create_run` | 校验容量并启动一个 stdio MCP 子进程 |
| `mcp_request` | 转发一条经过校验的 MCP JSON-RPC 请求 |
| `cancel_run` | 终止子进程并清理临时目录 |
| `get_run` | 获取无内容的状态和资源计数 |
| `health` | 检查 Worker 和 Obscura 二进制兼容性 |

socket 使用最小文件权限；协议仍携带 Worker 认证 Token 和随机 run capability。Token 只存在
于容器环境，启动子进程时使用显式环境白名单，不能继承 Mimi 或 Worker 的认证环境。

### 5.3 子进程生命周期

每次 `create_run`：

1. 校验全局活动数为零，否则返回 `BROWSER_CAPACITY_EXHAUSTED`。
2. 创建权限为 `0700` 的临时目录，不挂载持久化 profile。
3. 以固定参数启动：

   ```text
   /usr/local/bin/obscura mcp --stealth --font-dir /fonts
   ```

4. 通过 stdin/stdout 交换 MCP JSON-RPC；stderr 进入有界环形缓冲，不与协议流混合。
5. 完成 MCP `initialize` 和 `tools/list`，确认所需工具及 Schema 存在。
6. 到达终态、取消、超时、MCP 断连或 Worker shutdown 时先发送 `browser_close`，随后
   `SIGTERM`，短暂等待后 `SIGKILL`。
7. 等待并检查退出码，关闭 pipe，删除临时目录，释放唯一 lease。

任何清理步骤失败都必须记录并继续完成剩余回收，不能因一次异常留下可复用会话。

### 5.4 容器边界

- `cap_drop: [ALL]`、`no-new-privileges:true`、非特权运行。
- 固定 CPU、内存、PID、文件大小和临时目录上限。
- 只读根文件系统；仅 `/tmp`、Worker runtime 和字体目录按需挂载。
- 不挂载 Docker socket、项目目录、数据目录或 Mimi secrets。
- 保持 Obscura 私网拒绝，禁止 `--allow-private-network`。
- Worker 仅允许一个子进程，禁止 Obscura 自行派生不受控服务。

## 6. 父 Agent 工具契约

### 6.1 `delegate_browser_task`

Task Agent 只注册一个按需工具：

```text
delegate_browser_task(
  goal,
  start_url,
  expected_output,
  interaction_mode="read_only",
  artifact_mode="none",
  idempotency_key
)
```

字段使用强类型枚举和 dataclass 校验：

| 字段 | 约束 |
| --- | --- |
| `goal` | 具体、可验收的网页子任务，最多 1000 字符 |
| `start_url` | 一个公开 HTTP(S) URL，必须通过 `PublicUrlGuard` |
| `expected_output` | `facts`、`summary`、`links`、`screenshot` 或 `pdf` |
| `interaction_mode` | `read_only` 或 `public_interaction` |
| `artifact_mode` | `none`、`screenshot` 或 `pdf` |
| `idempotency_key` | 父任务内稳定键，防止重复启动同一委派 |

固定属性：

- `allowed_roles={AgentRole.TASK}`；
- `skill_name="browser-delegation"`；
- `read_only=false`，因为页面交互可能产生无法完全证明的外部副作用；
- `parallel_safe=false`；
- `completion_required=true`；
- 独立 `BROWSER_DELEGATE` 预算，默认单 Task 最多 2 次；
- 当前 Task、scope 和 revision 必须有效。

父 Agent 不能传 Browser system prompt、MCP 工具名、selector、Cookie、Header、代理或任意
模型参数。`BrowserAgentRunner` 从可信配置构造这些内容。

### 6.2 委派时机

Skill 指导 Task Agent：

1. 静态资料优先 `search_web` 和 `fetch_url`。
2. 只有需要 JavaScript 渲染、页面交互、页面截图或 PDF 时才委派。
3. 一次委派应描述完整目标，不把每个点击拆成一次父工具调用。
4. Browser Agent 返回部分完成后，只有目标发生实质变化或需要一次明确的新尝试时才使用
   第二次委派。
5. 登录、验证码、秘密输入或高影响操作不应委派。

## 7. Browser Agent

### 7.1 运行上下文

`BrowserAgentRunner` 创建全新的模型会话，只提供：

- 父工具输入中的目标、起始 URL、输出要求和交互模式；
- 当前日期、运行预算和外部内容低信任说明；
- 过滤后的 Obscura MCP 工具；
- 一个终态 `finish_browser_task` 工具。

不提供 QQ 原消息历史、群成员、父 Agent 隐藏上下文、Task Store、代码执行、Web Search、
文件系统、图片生成或消息发送能力。Browser Agent 的结果由 Runner 校验后才返回父 Agent。

Browser Agent 复用 Pumpkin 的统一 `ModelProvider` 接口和模型凭据，但使用独立
`AgentProfile`。建议默认沿用 Task Agent 的模型，设置
`PUMPKIN_BROWSER_REASONING_EFFORT=low`；模型名称、推理深度、超时和 Token 上限均由可信
配置注入，父 Agent 不能在委派参数中覆盖。模型凭据只留在 Mimi，不传给 Worker。

### 7.2 内部循环

典型流程：

```text
browser_navigate
  -> browser_snapshot / browser_markdown
  -> browser_interactive_elements
  -> browser_click / browser_fill / browser_scroll
  -> browser_wait_for_text
  -> browser_markdown / browser_links
  -> browser_screenshot / browser_pdf
  -> finish_browser_task
```

Browser Agent 默认最多 20 次 MCP 工具调用、24 个模型 Step、180 秒总时长。MCP 调用预算
属于子 Agent，不占父 Task Agent 的低层工具预算；父 Agent 只消耗一次委派调用。

达到 Token、Step、MCP 或时间上限时，Runner 不丢弃已取得结果，而是生成
`partially_completed`，保留已验证 facts、links 和已成功写入的 Artifact。

### 7.3 MCP 工具白名单

首版允许：

| 工具 | 附加限制 |
| --- | --- |
| `browser_navigate` | URL 必须先经 `PublicUrlGuard` |
| `browser_snapshot` | 截断正文和元素数量 |
| `browser_markdown` | 截断字符数 |
| `browser_links` | 限制数量，重新校验 URL |
| `browser_interactive_elements` | 截断数量和字段 |
| `browser_click` | 只接受当前 snapshot 的 `ref` |
| `browser_fill` | 只接受安全 `ref`，限制值长度 |
| `browser_type` | 只接受安全 `ref`，限制值长度 |
| `browser_press_key` | 仅允许受控按键，不接受 selector |
| `browser_scroll` | 有界距离，不接受 selector |
| `browser_wait_for_text` | 有界超时和文本长度 |
| `browser_screenshot` | 固定格式和像素上限 |
| `browser_pdf` | 固定参数和字节上限 |
| `browser_close` | 幂等 |

明确拒绝：

- `browser_evaluate`、`browser_wait_for_function`；
- `browser_storage_state` 及恢复登录态的工具；
- `browser_tab_new/list/switch/close`；
- Cookie、Storage、Header、请求拦截、网络修改类工具；
- `browser_fill_form`，首版逐字段预检，避免批量绕过；
- `browser_select_option` 和 `browser_extract`，当前上游 Schema 依赖 selector，无法满足
  首版只接受安全 `ref` 的契约；
- 所有不在编译期白名单内的新上游工具。

Worker 启动时允许 `tools/list` 用于兼容探针，但实际 `tools/call` 同时由
`BrowserAgentRunner` 和 Worker 检查。上游 `latest` 新增工具不会自动获得权限。

### 7.4 参数净化与元素预检

- click、fill、type 和 select 只允许 `ref`，删除并拒绝 `selector`。
- 操作 ref 前先读取当前 interactive metadata；ref 过期时要求重新 snapshot，不做文本
  模糊匹配。
- fill/type 拒绝 password、file、hidden、不可见、禁用和无法确认类型的控件。
- 输入值不得来自配置 Secret、Cookie、Token 或父 Agent 未显式提供的数据。
- 按键只允许 `Enter`、`Escape`、`Tab`、方向键、`PageUp`、`PageDown`、`Home` 和 `End`。
- `read_only` 模式禁用 fill、type、select，以及提交型控件的 click/Enter。
- `public_interaction` 只允许不需要身份和秘密的公开筛选或页面状态操作。无法证明是否会产生
  持久副作用时停止并返回限制，不根据关键词猜测安全性。
- click、press、select、fill 或 type 后必须刷新快照并重新校验最终 URL；URL 不符合公网
  策略时立即冻结交互、关闭 run 并返回 `BROWSER_URL_REJECTED`。

## 8. 结果契约

Browser Agent 必须通过 `finish_browser_task` 返回：

```text
BrowserRunResult
  status: completed | partially_completed | failed
  summary: str
  facts: list[BrowserFact]
  links: list[BrowserLink]
  artifacts: list[ArtifactRef]
  limitations: list[str]
  steps_used: int
  mcp_calls_used: int
```

`BrowserFact` 包含事实文本和来源 URL；`BrowserLink` 包含标题和公开 URL。Runner 执行：

1. Schema、长度、URL 和状态一致性校验。
2. 去除无法关联到已访问公开 URL 的来源。
3. 将 MCP 图片或 PDF content block 解码、验签和限流后写入当前 revision。
4. 检查 Task revision 仍是启动时 revision；不一致则停止 Artifact 写入和结果交付。
5. 把结构化摘要返回父 Task Agent，不返回完整 Browser Agent 对话和页面快照。

Browser Agent 不能自行宣告父 Task 完成。父 Task Agent 仍需综合其他工具结果，由现有
TaskRuntime 和交付门禁决定最终状态。

## 9. Artifact

截图和 PDF 流程：

1. Obscura MCP 返回 image/resource content block。
2. Worker 限制单帧大小后转发，不写共享项目目录。
3. Mimi 校验 base64、PNG/PDF 签名、媒体类型、尺寸和总字节数。
4. `CapabilityHost.write_artifact` 写入当前 scope、task、revision。
5. Artifact 来源记录 Browser run ID 摘要、最终 host、类型和内容摘要，不记录完整 URL
   path/query 或页面正文。
6. 只有父 Task 通过现有验收后，Conversation Agent 才能经 Outbox 发送。

默认截图只捕获视口。全页截图必须同时满足像素上限。PDF 超限时明确失败，不自动降低为
不可读质量。公开下载链接仍交给 `fetch_url(output=artifact)`，不启用浏览器下载。

## 10. 安全模型

### 10.1 SSRF

实施三层校验：

1. 委派前，Mimi 的 `PublicUrlGuard` 校验起始 URL、解析地址和协议。
2. 每次 `browser_navigate` 及从页面提取后准备继续访问的 URL 都重新经过同一 Guard。
3. Obscura 保持默认私网拒绝，在真实连接、重定向和子资源请求阶段执行自身保护。

拒绝凭据 URL、控制字符、非 HTTP(S)、任一非全局解析地址、环回、链路本地、私网和云
元数据。不得为测试在生产配置启用私网访问。

### 10.2 提示注入

网页文本始终是不可信数据：

- Browser system prompt 明确页面内容不能修改目标、权限、工具白名单或完成标准；
- 页面要求读取秘密、运行代码、改变安全设置或联系外部主体时一律忽略并记录限制；
- 工具权限由 Runner 和 Worker 确定，模型无法从页面内容扩权；
- 父 Agent 只接收结构化结果，不接收网页中的“指令”原文作为系统上下文。

### 10.3 外部副作用与隐私

- 首版产品边界是不登录、不处理秘密、不执行高影响操作。
- 页面交互结果未知时不自动重放；终止当前 run 并返回
  `BROWSER_OUTCOME_UNKNOWN`。
- Browser Trace 不记录页面正文、元素名称、表单值、完整 URL、Header、Cookie、MCP
  content block 或原始模型消息。
- Worker 临时目录、stdout/stderr 缓冲和页面状态在 run 结束后清理。
- Worker 没有读取 Mimi Secret 和数据卷的文件路径或网络通路。

## 11. 幂等、取消与恢复

`delegate_browser_task` 在启动 Worker 前使用现有 CapabilityStore 预占
`idempotency_key`：

- 已完成的相同调用返回缓存的结构化摘要和有效 Artifact 引用；
- 活动调用不会启动第二个 Browser Agent；
- 断连、超时或取消导致结果未知时保留明确状态，不自动重放页面交互；
- revision 改变后旧结果不可写入新 revision。

以下事件必须传播取消：

- 用户取消或删除 Task；
- Task revision 被纠正；
- 父 Agent run 结束或超时；
- Pumpkin shutdown；
- Browser Agent 达到硬预算；
- Worker 或 MCP 心跳失败。

取消路径为：

```text
TaskRuntime cancel
  -> BrowserAgentRunner.cancel
       -> cancel model loop
       -> BrowserWorkerClient.cancel_run
            -> terminate obscura child
            -> wipe temp state
```

Mimi 或 Worker 重启后不恢复浏览器会话。恢复中的父 Task 可基于任务状态决定是否发起新的
委派，但不能自动重放旧调用。

## 12. 预算与并发

| 配置 | 建议默认值 | 说明 |
| --- | ---: | --- |
| `PUMPKIN_TASK_BROWSER_DELEGATE_LIMIT` | 2 | 单 Task 的高层委派次数 |
| `PUMPKIN_BROWSER_AGENT_MAX_STEPS` | 24 | 子 Agent 模型步骤 |
| `PUMPKIN_BROWSER_MCP_CALL_LIMIT` | 20 | 单次委派 MCP 调用数 |
| `PUMPKIN_BROWSER_REASONING_EFFORT` | `low` | 子 Agent 独立推理深度 |
| `PUMPKIN_BROWSER_RUN_TIMEOUT_SECONDS` | 180 | 单次委派总时长 |
| `PUMPKIN_BROWSER_MCP_TIMEOUT_SECONDS` | 30 | 单次 MCP 调用上限 |
| `PUMPKIN_BROWSER_MAX_CONCURRENCY` | 1 | 首版 Worker 活动 run |
| `PUMPKIN_BROWSER_MAX_CONTENT_CHARS` | 12000 | 单次模型可见页面内容 |
| `PUMPKIN_BROWSER_MAX_LINKS` | 200 | 单次结果链接数 |
| `PUMPKIN_BROWSER_MAX_SCREENSHOT_PIXELS` | 12000000 | 截图像素上限 |
| `PUMPKIN_BROWSER_MAX_ARTIFACT_BYTES` | 20971520 | 单个截图或 PDF 上限 |

父 Task 的 `PUMPKIN_TASK_MAX_STEPS` 无需为每次点击全局放大。容量满时返回
`BROWSER_CAPACITY_EXHAUSTED`，不在内存中无限排队。若未来需要并发，按 Worker 副本进行
一 run 一容器或一 run 一进程的显式路由。

## 13. 错误契约

| 错误码 | 可重试 | 含义 |
| --- | --- | --- |
| `BROWSER_NOT_CONFIGURED` | 否 | Worker 或模型配置不完整 |
| `BROWSER_UNAVAILABLE` | 是 | Worker 或 MCP 暂时不可用 |
| `BROWSER_INCOMPATIBLE` | 否 | 当前 `latest` 缺少所需工具或 Schema |
| `BROWSER_CAPACITY_EXHAUSTED` | 是 | 活动 run 已达到上限 |
| `BROWSER_ARGUMENT_INVALID` | 否 | 委派或 MCP 参数非法 |
| `BROWSER_URL_REJECTED` | 否 | URL、协议或地址不安全 |
| `BROWSER_TOOL_REJECTED` | 否 | 工具不在白名单或参数越权 |
| `BROWSER_ELEMENT_REJECTED` | 否 | 控件类型、ref 或交互模式不允许 |
| `BROWSER_AGENT_LIMIT_REACHED` | 否 | 子 Agent 达到步骤或 MCP 预算 |
| `BROWSER_TIMEOUT` | 视阶段 | run 或 MCP 调用超时 |
| `BROWSER_OUTCOME_UNKNOWN` | 否 | 页面副作用结果未知，禁止盲重试 |
| `BROWSER_PROCESS_FAILED` | 是 | Obscura 异常退出或协议损坏 |
| `BROWSER_OUTPUT_INVALID` | 否 | 子 Agent 终态结果不符合 Schema |
| `BROWSER_CAPTURE_TOO_LARGE` | 否 | 截图或 PDF 超限 |
| `BROWSER_ARTIFACT_FAILED` | 否 | Artifact 校验或写入失败 |
| `BROWSER_REVISION_CHANGED` | 否 | 运行期间 Task revision 已变化 |

MCP、V8、子进程和模型原始异常只进入受控内部异常链。ToolResult 和用户消息使用稳定、友好
且不暴露内部路径或 Token 的说明。

## 14. 可观测性

记录单行事件：

- `pumpkin_browser_run_started`；
- `pumpkin_browser_mcp_call_finished`；
- `pumpkin_browser_run_finished`；
- `pumpkin_browser_process_reaped`。

允许字段包括 run ID 摘要、Task ID 摘要、revision、工具枚举、duration、状态、错误码、
步骤数、MCP 调用数、输入/输出字符数、Artifact 类型和大小、子进程退出码、Obscura 版本
和镜像 digest。

禁止记录完整 URL path/query/fragment、页面正文、元素名称、ref、表单值、Header、
Cookie、Token、截图/PDF、MCP 原始帧、模型原始消息和 stderr 原文。

聚合指标包括运行成功率、部分完成率、容量拒绝、超时、进程强杀、平均 MCP 调用、Artifact
失败、URL 拒绝、结果未知和每个 Obscura 版本的契约通过率。

## 15. 启动与降级

- Pumpkin 启动时构造 `BrowserWorkerClient`，但 Worker 不可用不阻止其他插件和能力启动。
- Worker 健康检查验证二进制、启动最小 MCP 子进程、`initialize`、`tools/list` 和关闭。
- Browser Skill 只有配置完整时进入 Catalog；服务临时故障时工具返回可诊断错误。
- `latest` 工具或 Schema 不兼容时设置 Browser capability unhealthy，不尝试绕过 Gateway。
- `fetch_url`、搜索、代码、图片和音频能力完全不依赖 Worker。
- Pumpkin shutdown 先拒绝新委派，再取消 Browser Agent，最后要求 Worker 回收子进程。
- Worker shutdown 必须等待或强杀子进程并检查退出码，不留下后台会话。

## 16. 实施阶段

### 阶段 1：Worker 与 MCP 契约

- 新增独立镜像、socket 控制协议、资源限制和进程回收。
- 接入 stdio MCP initialize、tools/list、tools/call。
- 实现双层工具白名单、参数净化和输出上限。
- 验证每 run 新进程、私网拒绝、取消和 `latest` 兼容探针。

### 阶段 2：Browser Agent 与父工具

- 新增 `AgentRole.BROWSER`、`BrowserAgentRunner` 和独立 Prompt。
- 新增 `delegate_browser_task`、委派预算和强类型输入。
- 实现结构化终态、部分完成、幂等和 revision 校验。
- 接入 TaskRuntime 取消和 shutdown 链路。

### 阶段 3：Artifact

- 将截图和 PDF MCP content block 转换为当前 revision Artifact。
- 增加格式、像素、字节和来源校验。
- 验证中文、Emoji、非空像素和真实发送 stub。

### 阶段 4：黑盒评测与上线

- 从正常 NapCat adapter、TaskRuntime、父 Agent、子 Agent、MCP、Artifact 和 Outbox 路径
  完整评测。
- 首版保持并发 1 和严格权限，根据脱敏指标决定是否增加 Worker 副本。

每阶段完成后更新本文状态。在用户明确批准设计前不修改实现。

## 17. 测试方案

### 17.1 离线单元测试

1. 委派工具仅对 Task Agent 可见，Browser Agent 看不到父工具和其他 Capability。
2. 输入枚举、URL、长度、幂等键和 revision 校验拒绝未知或多余字段。
3. Browser Agent 上下文不包含 QQ 历史、Secret、父 Agent 隐藏消息和文件系统工具。
4. MCP 白名单拒绝 evaluate、storage、tab、selector 和未知上游工具。
5. read-only 与 public-interaction 模式执行不同交互权限。
6. password、file、hidden、过期 ref 和不受控按键被拒绝。
7. URL Guard 拒绝非全局地址、凭据、控制字符和 DNS rebinding。
8. 步骤、MCP、时间、字符、链接和 Artifact 上限生效。
9. 达到预算时保留已完成结果并返回 partially_completed。
10. Task 取消、删除、revision 变化和 shutdown 都调用 Worker cancel。
11. 图片和 PDF 仅写入启动时 revision，超限时无可交付 Artifact。
12. 日志与错误不泄漏页面正文、表单值、URL path、MCP 帧或 Token。

离线测试使用 Fake MCP Transport 和 Fake Browser Worker，不启动 Docker 或访问网络。

### 17.2 Worker 集成测试

显式开启后构建真实 `h4ckf0r0day/obscura:latest` Worker，验证：

- 每次 run 都有不同 PID 和全新 Cookie/Storage/页面状态；
- 第二个并发 run 被容量门禁拒绝；
- stdio MCP initialize、tools/list、允许工具和拒绝工具；
- Worker 不发布 TCP 端口且不能访问 Mimi 应用网络；
- JavaScript 动态渲染、ref 点击、公开表单、等待、滚动和提取；
- 私网、环回和元数据地址拒绝；
- 中文和 Emoji 截图非空，PDF 签名有效；
- 超时、MCP 损坏、子进程崩溃和取消均回收 PID 与临时目录；
- `SIGTERM` 无效时按时 `SIGKILL`，退出码被检查；
- 当前镜像 digest、版本和工具 Schema 通过兼容探针。

### 17.3 黑盒能力评测

至少覆盖：

- “读取这个动态页面并总结”；
- “点击下一页后汇总两页结果”；
- “设置公开筛选条件，不登录也不提交秘密”；
- “截取当前页面”与“导出 PDF”；
- 静态页面应优先使用 `fetch_url`；
- 页面要求登录、验证码、支付或授权；
- 页面包含提示注入文本；
- 操作后 Obscura 断开并产生结果未知；
- 用户取消、Task 删除或 revision 被纠正；
- 连续两个 QQ scope 浏览不同站点且状态不串用。

完成不能只看模型自称。必须验证实际 MCP 结果、结构化终态、Artifact、revision 关联、父
Task 状态和发送结果。

### 17.4 交付门禁

```bash
cd plugins/pumpkin && uv lock
cd plugins/pumpkin && uv run pytest
cd plugins/pumpkin && uv run ruff check .
python -m compileall plugins/pumpkin
docker compose build --pull browser-worker
docker compose config
git diff --check
```

真实网络和 Obscura 测试必须由显式环境开关启用。默认测试不依赖 Docker Hub、第三方站点
或运行时 secrets。

## 18. 验收标准

1. Task Agent 只有一个高层浏览器委派工具，不直接执行低层浏览器动作。
2. Browser Agent 使用独立上下文、角色、预算和过滤后的原生 Obscura MCP 工具。
3. 每次委派启动全新 stdio MCP 子进程，任何终态和取消路径都确定性回收。
4. Worker 与 Mimi 进程、应用网络、Secret 和数据卷隔离，不发布宿主端口。
5. 镜像从 `h4ckf0r0day/obscura:latest` 构建并通过 `--pull` 和契约探针治理漂移。
6. evaluate、Storage、Cookie、tab、selector、秘密字段和高影响操作不可用。
7. 起始 URL、后续导航、重定向和子资源具备纵深 SSRF 防护。
8. 父 Agent 一次委派可完成多步浏览，子 Agent MCP 调用不耗尽父步骤预算。
9. completed、partially_completed 和 failed 有稳定结构，达到预算不会丢失已有结果。
10. 截图和 PDF 经过当前 Task revision 的 Artifact 与交付门禁。
11. Worker 或 `latest` 不兼容时只禁用 Browser capability，不影响 `wreq` 和其他能力。
12. 离线测试、Worker 集成测试、黑盒评测、Compose 校验和敏感信息检查通过。

## 19. 风险与回滚

- **`latest` 漂移**：上游可能无预告改变 CLI 或 MCP Schema。通过构建 `--pull`、记录
  digest、启动探针和显式 `BROWSER_INCOMPATIBLE` 控制，不静默放宽白名单。
- **V8 逃逸**：Obscura 在进程内执行不可信 JavaScript。独立 Worker、隔离网络、无 Secret、
  无数据卷、最小权限和资源限制是上线硬门禁。每 run 新子进程只保证正常状态隔离，不能
  抵御已成功控制整个 Worker 容器的 V8 RCE；检测到进程异常或完整性异常后必须将 Worker
  标记 unhealthy 并替换容器。更强多租户隔离需演进到一 run 一容器或一 run 一虚拟机。
- **状态泄漏**：共享 HTTP MCP 会跨任务共享状态。每 run 新建 stdio 子进程，禁止进程池
  和 profile 复用。
- **提示注入**：网页可伪装成系统指令。子 Agent Prompt、结构化输出和双层工具权限保持
  不受页面控制。
- **外部重复操作**：断连后无法证明点击是否生效。结果未知时终止且禁止自动重放。
- **资源耗尽**：恶意页面可能占用 CPU、内存或 PID。使用容器限制、调用预算、总超时和
  强制回收。
- **父子 Agent 语义偏差**：子 Agent 可能错误理解目标。父工具要求明确 goal 和
  expected_output，父 Agent 仍负责核验与最终综合。
- **兼容性不足**：目标站点可能依赖未实现 Web API。返回事实性限制；适合静态抓取的内容
  仍由 `fetch_url` 处理。

该特性不引入数据库迁移。回滚时移除 Browser Agent、委派 Capability、Worker Client、
Skill、配置和 Compose 服务即可；现有 Web、Task、Artifact 和持久化数据保持兼容。通用
幂等存储中的历史委派记录自然失效，不执行破坏性清理。

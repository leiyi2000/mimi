---
status: implemented
owner: Mimi maintainers
updated: 2026-10-28
commits: []
---

# Pumpkin 公网抓取 wreq Transport

## 1. 背景

Pumpkin 原有的 `fetch_url` 和 `download_web_image` 通过
`providers.web.WebSearchProvider` 使用 `httpx.AsyncClient` 读取公开网页。请求会校验
URL、DNS 地址、重定向、内容类型和响应体大小，但 TLS ClientHello、HTTP/2 Settings 和
Header 行为仍表现为通用 Python HTTP 客户端。部分公开站点会据此提高风控等级或直接返回
`403`。

`wreq-python` 使用 Rust HTTP 栈和 BoringSSL，并提供成套的浏览器 TLS、HTTP/2 和 Header
Profile。对 `wreq 0.13.0` 的独立验证确认，Chrome Linux Profile 可以通过 HTTP/2 发出
一致的浏览器 User-Agent、JA3 和 Akamai HTTP/2 指纹。该能力只能降低基于传输层指纹的
拦截概率，不能执行 JavaScript、生成挑战 Token、通过验证码或替代浏览器自动化。

## 2. 目标与非目标

### 2.1 目标

1. 使用 `wreq` 承担 Pumpkin 对任意公开网页和网页图片的 GET 请求。
2. 使用固定、内部维护的 Chrome Linux Profile，使 TLS、HTTP/2 和 Header 行为保持一致。
3. 保留现有 SSRF 防护、手动重定向、响应体上限、并发预算、错误码和脱敏 HTTP Trace。
4. 将 `wreq` 的原生响应和异常隔离在 Provider 内，不向 Agent、Tool 或 Runtime 泄漏。
5. 保持测试可离线执行，并验证 Docker 目标平台可以安装原生 wheel。

### 2.2 非目标

- 不替换模型、搜索、代码沙箱、图片生成、音频和媒体服务使用的 `httpx`。
- 不让 `search_web` 的认证 API 伪装成浏览器。
- 不引入代理池、Cookie 持久化、自动重试或 `httpx` 静默回退。
- 不宣称绕过 Cloudflare、Akamai、DataDome 等挑战系统。
- 不执行 JavaScript，不新增 Playwright 或浏览器进程。

## 3. 接入边界

外部请求按用途分成两类：

| 请求 | Transport | 原因 |
| --- | --- | --- |
| `search_web` 调用配置的搜索 API | `httpx` | 受认证的结构化服务 API，不需要浏览器指纹 |
| `fetch_url(output=text)` 读取任意公开页面 | `wreq` | 目标站点未知，可能检查 TLS/HTTP2 指纹 |
| `fetch_url(output=artifact)` 下载公开文件 | `wreq` | 与页面抓取共享任意公网来源和安全边界 |
| 模型、代码、生成式图片、音频和媒体请求 | `httpx` | 稳定 API 契约，不扩大本次变更范围 |

仓库级“HTTP 使用 `httpx`”约束在实施时应增加一个窄例外：只有 Pumpkin 的公开 Web
抓取 Transport 可以使用 `wreq`，其他业务 HTTP 继续使用 `httpx`。

## 4. 结构设计

```text
WebCapabilityPlugin
  -> WebSearchProvider
       -> search()
            -> httpx search client
       -> fetch() / download_image()
            -> WreqWebTransport
                 -> resolve and validate public addresses
                 -> build request-scoped DNS pin
                 -> wreq Client with fixed browser profile
                 -> bounded response stream
```

`WebSearchProvider` 继续拥有页面解析、错误分类、每 host 信号量和 HTTP Trace。
新增的 `WreqWebTransport` 只封装传输差异：

- 创建与关闭 `wreq.Client`；
- 将状态码、URL、Header 和流式响应转换为 Provider 使用的内部值对象；
- 将 `wreq` 异常统一转换为内部 `WebTransportError`；
- 在读取期间执行字节上限检查；
- 不暴露 `wreq.Response` 给上层。

测试通过注入 `WebTransport` 协议的 Fake 实现构造响应，不依赖真实 DNS 或公网。搜索 API
保留独立的 `httpx.AsyncClient` 注入点，避免一个含混的 `client` 参数同时代表两种协议。

## 5. 浏览器 Profile 与请求一致性

首版固定使用 `Profile.Chrome149`、`Platform.Linux`、HTTP/2 和默认浏览器 Header。
Profile 是与 `wreq 0.13.x` 绑定的内部策略，不作为 Agent 参数，也不允许模型选择。

固定 Profile 的理由：

- 随机切换浏览器和平台会让同一任务的请求特征不一致；
- 固定 Linux Profile 与 Mimi Docker 运行环境一致；
- 版本升级可以通过依赖变更、指纹集成测试和评审显式完成。

页面和图片请求只覆盖必要的 `Accept` Header，不再写入
`User-Agent: mimi-pumpkin/1.0`，避免破坏浏览器 Profile。不开启 Cookie Store，防止用户
提供的不同站点在长期 Client 中共享状态；禁用系统代理，继续保持 `trust_env=False` 的
现有语义；TLS 证书和主机名验证保持开启。

## 6. SSRF 与重定向

现有 `_validate_public_url` 会解析全部地址并拒绝任何非全局地址，但校验和真正连接之间仍
存在二次 DNS 查询。接入 `wreq` 时将其收敛为“解析、校验、固定”：

1. 校验协议、主机、凭据和控制字符。
2. 通过系统 DNS 获取目标的全部 A/AAAA 地址。
3. 任一地址不是全局地址时拒绝整个请求。
4. 使用 `DnsOptions.add_resolve` 将本次请求固定到刚刚校验的地址集合。
5. 禁用 `wreq` 自动重定向，Provider 最多手动处理 3 次重定向。
6. 每个重定向目标重新执行完整的解析、校验和 DNS 固定。

每次跳转使用独立的短生命周期 Client，避免旧主机的 DNS 固定和 Cookie 状态污染新目标。
这会牺牲跨跳转连接复用，但消除了 DNS Rebinding 的校验/连接时间差。每 host 并发信号量
仍在 Transport 外执行。

## 7. 流式读取与资源上限

`wreq.Response.stream()` 会产生 `memoryview` 数据块，也可能产生 HTTP Trailer
`HeaderMap`。Transport 只累计数据块，忽略当前业务不使用的 Trailer，并在每次追加前校验：

- 页面正文不超过 `PUMPKIN_FETCH_MAX_BYTES`；
- 图片不超过 `PUMPKIN_IMAGE_FETCH_MAX_BYTES`；
- `Content-Length` 已超限时不读取正文；
- 解压后的实际字节流超限时立即停止并关闭响应。

Response、Streamer 和 Client 都必须由上下文管理器确定性关闭。取消、超时和读取异常统一
释放资源，不允许以完整 `bytes()` 读取替代流式上限。

## 8. 错误与可观测性

Provider 对 Agent 保留现有稳定错误码：

- `FETCH_URL_INVALID`、`FETCH_DNS_FAILED`、`FETCH_PRIVATE_ADDRESS`；
- `FETCH_REDIRECT_REJECTED`、`FETCH_CONTENT_REJECTED`、`FETCH_TOO_LARGE`；
- `FETCH_TIMEOUT`、`FETCH_RATE_LIMITED`、`FETCH_NOT_FOUND`；
- `FETCH_AUTH_FAILED`、`FETCH_UPSTREAM_ERROR`、`FETCH_UNAVAILABLE`。

`wreq.TimeoutError` 转换为可重试的传输失败；TLS、连接、代理、Body、Decode、Builder 和
Request 异常转换为 `FETCH_UNAVAILABLE`。HTTP 状态码仍由 Provider 分类，只有 `408`、
`429` 和 `5xx` 可重试。

`pumpkin_web_http` 事件保持现有字段，并增加非敏感的
`transport="wreq"` 和内部 Profile 名称。事件仍禁止记录 path、query、fragment、请求头、
响应体、Cookie、Token、解析地址和原始异常文本。不得因 `wreq` 失败自动用 `httpx`
重发，否则会造成双请求、绕过既定指纹策略并模糊故障归因。

## 9. 配置与依赖

`plugins/pumpkin/pyproject.toml` 增加 `wreq>=0.13.0,<0.14`。`httpx` 继续保留，因为其他
Provider 和搜索 API 仍使用它。`uv.lock` 必须随依赖更新。

首版不增加 Profile 环境变量。只有经过实际兼容性需求验证后，才考虑在 `WebConfig` 中用
语义化枚举提供少量受控 Profile；不得接受任意字符串或让 Agent 动态切换。

`wreq` 是原生扩展。实施门禁必须验证当前 `python:3.14-slim` 镜像对应的 Linux
`x86_64`/`aarch64` wheel 可安装，且不会在常规镜像构建中临时编译 Rust 和 BoringSSL。

## 10. 测试与验收

### 10.1 离线单元测试

1. 页面和图片路径只调用 `WreqWebTransport`，搜索 API 仍调用 `httpx`。
2. Profile、Linux 平台、禁用代理、禁用自动重定向和超时配置正确。
3. DNS 结果包含私网、环回、链路本地或保留地址时请求不会发出。
4. 每次重定向都重新解析并固定地址，超过跳转上限明确失败。
5. `memoryview` 流式读取、Trailer、`Content-Length` 和实际字节超限均正确处理。
6. `wreq` 各类异常映射到稳定 Provider 错误，原始异常不进入 Tool 结果。
7. Trace 只包含 host 和允许字段，不泄漏 URL 细节或响应内容。
8. 同 host 并发、Tool Budget、URL 去重和现有错误重试语义不变。

### 10.2 显式网络测试

网络测试默认关闭。显式开启时使用公开指纹回显服务验证：

- 协议协商为 HTTP/2；
- User-Agent 与固定 Chrome Linux Profile 一致；
- 返回 JA3 和 HTTP/2 指纹；
- 普通 `403` 不被误判为 Transport 成功，也不触发 `httpx` 回退。

指纹哈希只用于本次响应断言或调试，不写入仓库、长期日志和配置。

### 10.3 交付门禁

```bash
cd plugins/pumpkin && uv lock && uv run pytest
cd plugins/pumpkin && uv run ruff check .
docker compose build mimi
docker compose config
git diff --check
```

验收通过后同步更新：

- `AGENTS.md` 的窄范围 `wreq` 例外；
- `ARCHITECTURE.md` 的 Pumpkin 公网 Web Transport；
- `plugins/pumpkin/README.md` 的网络抓取行为；
- 本文状态与 `docs/feature/README.md` 索引。

## 11. 风险与回滚

- **原生 wheel 缺失**：镜像构建失败，不允许在线临时安装编译工具兜底。
- **Profile 过期**：表现为特定站点重新拦截，通过升级 `wreq` 和显式指纹测试处理。
- **站点依赖 JavaScript/挑战 Token**：返回明确抓取失败，不伪装成功。
- **上游 API 变动**：限制 `wreq<0.14`，升级时重新检查响应流、Header 和异常契约。
- **兼容性回归**：回滚 Provider 的公开抓取 Transport 和依赖变更；不影响搜索、模型、
  音频、图片生成、代码沙箱或持久化数据。

---
status: implemented
owner: Mimi maintainers
updated: 2026-10-23
commits: []
---

# Pumpkin 任务性能与 Web 访问治理

## 1. 背景

Pumpkin 的长任务曾允许 Task Agent 使用与 Conversation Agent 相同的 36 个模型步骤，
网页搜索、正文抓取和文件下载也没有单任务调用上限。模型可以重复读取同一 URL，并把每次
抓取的完整正文持续带入后续模型请求。慢任务的主要耗时因此来自过多模型回合和重复 Web
调用，而不是任务排队。

本特性通过五个相互独立的边界控制任务成本和交付可靠性：

1. Task Agent 总步骤和 Web 工具调用预算；
2. 单 Turn URL 去重、非重试失败熔断和按主机并发限制；
3. 模型可见网页结果裁剪；
4. 不记录 URL 路径和查询参数的 HTTP 可观测事件；
5. 保留完成步骤和已验证 Artifact 的自动降级交付。

## 2. 执行预算

Conversation Agent 继续使用 `PUMPKIN_MAX_STEPS`。Task Agent 使用独立的
`PUMPKIN_TASK_MAX_STEPS`，默认 16 步，并在 50% 和 75% 时收到收敛提醒。
`PUMPKIN_TASK_FINALIZATION_STEPS` 默认保留最后 2 步；进入保留区后 AgentLoop 只向模型
暴露 `complete_task` 和 `task_turn_complete`，不再允许加载技能、搜索、抓页或生成产物。
模型返回纯文本不能结束 Task Turn，必须调用终态工具。

若 16 步结束时仍未调用终态工具，但此前工具已返回至少一个 `verified=true` 的 Artifact，
Runtime 会新增一个可审计的 fallback Step，调用 `complete_task` 提交
`partially_completed`，保留 Artifact 并明确剩余事项。没有已验证 Artifact、Artifact
验收门禁拒绝或兜底工具失败时，Turn 仍以 `max_steps` 失败，不伪造完成状态。

Task Agent 的 Web 工具预算由强类型 `TaskBudgetConfig` 和 `ToolBudgetKind` 表达：

| 工具类别 | 默认单 Turn 上限 |
| --- | ---: |
| `search_web` | 4 |
| `fetch_url(output=text)` | 8 |
| `fetch_url(output=artifact)` | 4 |

工具定义按调用参数解析预算类别；AgentLoop 不按工具名猜测类别。预算状态只存在于当前
Turn，不会跨任务或跨 Turn 污染。超限调用返回不可重试的
`TOOL_BUDGET_EXHAUSTED`，让模型使用已有结果完成或部分完成任务。

## 3. URL 去重与失败熔断

声明 URL 去重参数的工具会在调用前规范化 URL：协议和主机转小写、移除 fragment 和默认
端口、规范化查询参数顺序。去重键同时包含工具预算类别，因此读取网页和下载图片不会互相
误判为重复操作。

同一 Turn 内：

- 已成功访问的 URL 再次调用时复用原 `ToolResult`；
- 已发生不可重试错误的 URL 再次调用时直接返回原错误；
- 同批次重复 URL 只允许第一个请求进入 Provider，其余调用返回
  `DUPLICATE_RESOURCE_IN_FLIGHT`；
- 可重试错误允许再次调用，但仍消耗工具预算。

角色、Turn capability、当前 loadout 或工具预算导致的策略错误按错误码和工具名聚合，
不因模型更换参数而绕过熔断；连续两个 Step 重复同类策略错误时终止 Turn。普通资源错误
仍保留参数指纹，例如两个不同 URL 的 `FETCH_NOT_FOUND` 不会被误判为重复。

HTTP `408`、`429` 和 `5xx` 才标记为可重试。其他 `4xx`、内容类型拒绝、超限、私网地址
和非法 URL 均不可重试。Provider 按目标 host 使用最多 2 个并发槽，限制只约束 Web HTTP
请求，不串行化不同 host。

## 4. 模型上下文

`fetch_url` 的完整结构化结果仍写入 Session Event，供审计和恢复使用。写入模型消息的
`model_content` 由 Tool 定义声明最大 8000 字符；AgentLoop 在 JSON 结构内优先裁剪正文
和链接，并写入 `model_content_truncated` 标记。裁剪结果始终保持有效 JSON，不修改原始
`ToolResult`，也不影响 Artifact 或错误字段。

## 5. HTTP 可观测性

`WebSearchProvider` 对搜索、抓页和文件下载的每次 HTTP 尝试记录单行
`pumpkin_web_http` 事件，字段仅包括：

- `operation`；
- 脱敏后的 `host`；
- `status_code`；
- `duration_ms`；
- `retryable`、`error_code`；
- 服务端提供时的 `retry_after`。

事件不得包含 URL path、query、fragment、请求头、正文、Cookie、Token 或用户输入。
DNS 和网络异常同样只记录 host 与错误分类。可观测失败不得改变工具行为。

## 6. 调用链与失败行为

```text
TaskRuntime
  -> SessionAgentLoop(Task budget)
       -> Tool budget / URL state
       -> ToolRegistry
            -> WebCapabilityPlugin
                 -> WebSearchProvider
                      -> per-host semaphore
                      -> sanitized HTTP trace
```

预算拒绝和 URL 熔断作为普通 `ToolResult` 写入 Session Event。Provider 错误保留稳定
`reason_code`、HTTP 状态码和可重试属性；面向模型的内容使用友好提示，不暴露原始异常。
任何单个 Web 来源失败都不直接终止任务，除非模型重复触发相同不可重试调用或耗尽总步骤。

## 7. 验收标准

1. Task Agent 默认最多 16 个模型步骤，并在 50% 和 75% 注入提醒。
2. 最后 2 步只暴露终态工具，Task 纯文本输出不能绕过终态协议。
3. 步骤耗尽且已有已验证 Artifact 时自动提交部分完成；没有可靠产物时保持失败。
4. 三类 Web 工具预算可独立配置并在超限时显式失败。
5. 同一 Turn 内成功 URL 和不可重试失败 URL 不会再次发起 HTTP。
6. 仅 `408`、`429` 和 `5xx` 标记为可重试，同 host HTTP 并发不超过 2。
7. `fetch_url` 的模型可见结果不超过 8000 字符，完整事件结果不被修改。
8. HTTP trace 不包含 URL 路径、查询参数、密钥或正文。
9. Pumpkin 全量测试、Ruff、compileall 和 `git diff --check` 通过。

---
name: html-render
description: 将复杂的图文结构渲染为当前任务的 PNG Artifact。
allowed-tools:
  - render_html
agent-roles:
  - task
load-policy: on-demand
triggers:
  - 复杂图文
  - HTML 渲染
  - 信息图
  - 表格海报
  - 时间线
examples:
  - 把搜索结果整理成带标题、卡片和来源的图片
  - 渲染一张包含表格和图片的任务报告
anti-triggers:
  - 简单文本回复
  - 发送已有图片
priority: 75
---

当任务交付需要表格、时间线、分组卡片、统计摘要、图文混排或其他复杂版式时，使用
`render_html` 生成 PNG Artifact。简单结论直接用文本交付。

HTML 必须是自包含的 HTML/CSS 片段，不得包含脚本、事件处理属性、iframe、外部资源、本地
文件路径或网络 URL。需要引用当前任务已经验证的图片时，在 HTML 中使用
`src="artifact://<artifact_id>"`；这些 Artifact 必须属于当前任务和 revision。

`width` 是最终 PNG 的像素宽度，默认设备像素比为 1，因此也是 CSS 布局视口宽度。根元素
优先使用 `width: 100%`；若写固定像素宽度，不得超过 `width / device_pixel_ratio`，否则
右侧内容会落在渲染视口之外。除非同时按比例缩小 CSS 布局宽度，不要把
`device_pixel_ratio` 设为 2。

图片来自公开网络 URL 时，任务必须同时获得 `web-search` 和 `html-render` 授权。先加载
`web-search`，调用 `fetch_url(output="artifact", idempotency_key="...")` 下载并
校验图片；再把返回的 Artifact ID 写成
`src="artifact://<artifact_id>"`，或在 CSS 中写成
`background-image: url("artifact://<artifact_id>")`。不要把原始 HTTP(S) URL 直接写入
HTML。

渲染成功后，将返回的 Artifact ID 写入 `complete_task.artifact_ids`。Task Executor
不能调用 `send_task_delivery`；Runtime 会把完成事件持久化到 Conversation Agent Inbox，
再由 Conversation Agent 交付已验证 Artifact。不能把 HTML 源码当成交付完成，也不能
声称图片已发送。

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

渲染成功后，将返回的 Artifact ID 写入 `complete_task.artifact_ids`。Task Executor
不能调用 `send_task_delivery`；Runtime 会把完成事件持久化到 Conversation Agent Inbox，
再由 Conversation Agent 交付已验证 Artifact。不能把 HTML 源码当成交付完成，也不能
声称图片已发送。

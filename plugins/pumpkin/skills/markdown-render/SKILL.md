---
name: markdown-render
description: 将结构化 Markdown 渲染为当前任务的 PNG Artifact。
allowed-tools:
  - render_markdown
agent-roles:
  - task
load-policy: on-demand
triggers:
  - Markdown 渲染
  - 结构化报告
  - 旅行攻略
  - 清单
  - 表格报告
examples:
  - 把旅行攻略整理成包含表格和配图的 Markdown 图片
  - 把调研结论渲染成深色 Markdown 报告
anti-triggers:
  - 需要自由布局的复杂海报
  - 简单文本回复
priority: 74
---

当任务交付需要标题、引用、列表、表格、代码块、来源链接和简单图文混排时，使用
`render_markdown` 生成 PNG Artifact。需要自由定位、复杂卡片或精细视觉布局时改用
`html-render`。

Markdown 原始 HTML 不会执行。普通 HTTP(S) 链接可以作为来源保留，但图片只允许使用
`![说明](artifact://<artifact_id>)`，并且 Artifact 必须属于当前任务和 revision。

图片来自公开网络 URL 时，任务必须同时获得 `web-search` 和 `markdown-render` 授权。
先加载 `web-search`，调用
`fetch_url(output="artifact", idempotency_key="...")` 下载并校验图片，再把返回的
Artifact ID 写入 Markdown。不要把 HTTP(S)、本地路径或 data URL 直接写入 Markdown
图片语法。

渲染成功后，将返回的 Artifact ID 写入 `complete_task.artifact_ids`。Task Executor
不能调用 `send_task_delivery`；Runtime 会在任务完成验收后交付 Artifact。不能把
Markdown 源码当成交付完成，也不能声称图片已发送。

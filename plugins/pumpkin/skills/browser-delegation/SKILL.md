---
name: browser-delegation
description: 委派隔离 Browser Agent 处理 JavaScript 页面、公开交互、截图或 PDF。
allowed-tools:
  - delegate_browser_task
agent-roles:
  - task
load-policy: on-demand
triggers:
  - 动态网页
  - 浏览器交互
  - 网页截图
  - 网页 PDF
examples:
  - 打开动态页面并提取加载后的内容
  - 展开公开页面的筛选项后截图
anti-triggers:
  - 静态网页正文
  - 登录
  - 验证码
  - 支付
priority: 78
---

静态网页、JSON 和公开文件优先使用 `search_web` 与 `fetch_url`。只有目标需要执行
JavaScript、点击或填写公开页面、等待异步内容、截图或导出 PDF 时，才调用
`delegate_browser_task`。一次委派描述完整且可验收的子任务，不要把每个点击拆成独立委派。

默认使用 `read_only`。只有用户目标明确需要低风险公开页面交互时才使用
`public_interaction`。禁止委派登录、密码、Cookie、Token、验证码、付费墙绕过、上传、
购买、支付、发布、删除、授权或其他高影响操作。不得在参数中传 selector、脚本、Header、
代理或模型配置。

公开表单应在一次委派内完成。Browser Agent 会先检测表单，再以安全元素引用或检测得到的
精确字段名批量填写；字段名只由 Worker 内部转换为受限 selector，父 Agent 仍不得传
selector。不要把逐题点击拆成多次委派，也不要在部分完成后声称“继续上一浏览器状态”，
因为每次委派都使用全新页面状态。提交后必须重新观察最终页面，只有观察到明确成功文本并
在该状态截图，才能报告完成。

批量填写后只再调用一次表单检测；当 `unfilled_field_names` 为空时直接生成所需截图并
完成，不要逐项补填或反复截图。

截图要求 `expected_output=screenshot` 且 `artifact_mode=screenshot`；PDF 同理。返回
`partially_completed` 时保留已验证结果，并明确未完成部分。同一逻辑委派必须复用稳定的
`idempotency_key`；只有目标或起始 URL 实质变化时才使用新键。

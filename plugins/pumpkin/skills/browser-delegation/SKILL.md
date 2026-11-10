---
name: browser-delegation
description: 委派隔离 Browser Controller 处理 JavaScript 页面、交互、提交、截图或 PDF。
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
priority: 78
---

静态网页、JSON 和公开文件优先使用 `search_web` 或 `fetch_url`。只有目标需要
JavaScript、公开页面交互、截图或 PDF 时，才调用 `delegate_browser_task`。

调用时只传 `goal`。`goal` 应完整说明：

- 要处理的公开页面；
- 用户要达成的最终状态；
- 需要返回的事实、链接、截图或 PDF；
- 用户明确要求的截图范围、成功状态和禁止提交等限制。

不要传步骤、点击顺序、selector、脚本、Header、Cookie、幂等键、内部模式或验收参数。
不要把一个目标拆成多次委派，也不要在部分完成后尝试继续旧页面。Browser Controller
会自行规划、观察并生成受限 Python 程序，通过 Safe Browser SDK 连续操作和核验页面。
父 Task 不生成或传递脚本；浏览器程序不接触原始 Playwright、JavaScript、Cookie、
Storage、网络接口或文件系统。

浏览器业务动作由 AI 根据完整目标和当前页面决定，不使用登录、提交、支付等动作分类门禁，
也不按 HTTP 方法或域名范围限制页面请求。SDK 支持的动作和可交付产物仍以实际工具为准。
用户要求提交时保留完整提交目标和截图要求，不改写为只读或仅填写。
用户禁止提交时保留该限制。具体表单结构与成功条件由 Browser Controller 观察确定。

工具返回 `completed` 时，结果已通过最终页面证据门禁。返回
`partially_completed` 或 `failed` 时，直接向用户说明已确认结果和缺口，不重新委派。

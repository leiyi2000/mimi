---
name: browser-delegation
description: 委派隔离 Browser Controller 处理 JavaScript 页面、公开交互、截图或 PDF。
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

禁止委派登录、秘密、验证码绕过、上传、购买、支付、发布、删除、授权或其他高影响操作。
可逆页面交互必须来自用户原话的明确授权，不能由父 Agent 自行扩大；当前不支持提交。

工具返回 `completed` 时，结果已通过最终页面证据门禁。返回
`partially_completed` 或 `failed` 时，直接向用户说明已确认结果和缺口，不重新委派。

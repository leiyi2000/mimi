---
status: implemented
owner: Mimi maintainers
updated: 2026-11-06
commits: []
---

# Pumpkin 现成图片发现与任务原意

## 搜索请求

`capabilities/web.py` 装配 `WebSearchProvider`，`search_web` 继续通过 `httpx`
调用配置的搜索 API。请求只发送 `query` 和 `max_results`。同一查询的有界对照发现，
附带固定 `zone`、`language` 时返回无关学术资料，移除后返回相关页面；不将这两个
未经当前契约确认的字段继续传入。响应中的 `results` 必须是数组，错误结构明确失败。
搜索成功仅表示获得候选，不表示结果满足用户需求。

## 原始请求与执行摘要

Conversation Agent 通过 `runtime/tools.py` 的 `create_task` 契约整理目标、约束和
交付要求，只填写用户提出或确认的内容。`runtime/task_commands.py` 从当前 Inbox 对应
归档消息创建任务，持久化仍沿用现有 source message IDs，无新增数据库字段。

`TaskRuntime.run` 每次执行从归档恢复 `original_user_requests`，与 `task_handoff`
分开展示，即使恢复后的 handoff 没有携带消息也保留原话。Task Prompt 明确原话与用户纠正
优先于助手摘要；技能可用不代表允许改变交付方式。查找失败后改变为创作需要用户确认。
此边界通过消息结构和模型契约表达，不使用动词关键词分支或新的来源策略枚举。

## 图片候选与预览

`WebSearchProvider._parse` 保留原有 links，并从 HTML 的 `img`、`source` 提取
独立 `PageImage`。相对 URL 按最终页面地址解析，只保留 HTTP(S)，按 URL 去重，
最多返回 30 个图片候选及 alt 文本。网页正文和候选都是低信任资料。AgentLoop 裁剪长页面
时优先保留部分图片候选，避免长链接使全部内容被省略；完整工具结果不变。

Task Agent 使用现有 `fetch_url(output=artifact)` 下载候选。公网 DNS、重定向、
响应大小和 URL 预算继续由现有 Provider 与 AgentLoop 控制。图片登记前由 Pillow
验证可解码格式和尺寸，损坏或超限图片明确失败；预览处理在线程中执行，不阻塞事件循环。
预览最长边为 1280 像素，动画只取首帧转换成 PNG。原始文件以原有 ArtifactService
原子策略写入，保留来源 URL 指纹和下载策略，动画不被替换成预览。

下载结果通过已有 `ToolResult.images` 向模型提供视觉输入，不新增任意路径读取工具。
web-search Skill 指导 Agent 核对图片主体、文字与用途，再使用已有交付验收工具。
可解码只证明文件有效，语义相关性由 Agent 判断；首帧不能证明所有动画帧的内容。
幂等重复下载仍返回已登记 ID，不重新联网下载。

## 交付与失败

候选仍必须通过当前 task、scope、revision 的 Artifact 交付门禁，Task Executor 不直接
发送 QQ 消息。终态由既有 TaskEvent、Conversation Agent 与 Outbox 链路表达并交付。
无关搜索结果、无合适图片或来源不可访问时，Agent 应说明缺口或部分完成，不把相关页面
声明成已交付图片，也不自行生成替代品。没有增加自动浏览器回退、第三方图片搜索 SDK、
缓存或跨插件依赖。

## 验证范围

固定测试覆盖搜索请求字段、非法响应结构、图片与普通链接分离、长页面候选保留、
GIF 首帧视觉输入、损坏图片拒绝，以及恢复任务时保留原话。网络对照显式执行，
普通测试不依赖搜索服务。全量插件测试验证相邻任务、媒体、浏览器和交付链路。
真实模型的语义判断与 QQ 用户端展示仍需真实任务验收。

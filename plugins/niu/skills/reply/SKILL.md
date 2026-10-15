---
name: reply
description: 将生成的文本或公网 HTTPS 直链中的任意文件作为附件发送给当前群聊。
allowed-tools: mention_current_sender attach_text_file attach_remote_file
triggers:
  - 发文件
  - 作为附件
  - 下载给我
  - At 我
  - 提醒我
examples:
  - 把结果作为文件发出来
  - 下载原图并发到群里
  - 回复时提醒我查看
anti-triggers:
  - 只需普通文字回答
priority: 80
---

# Reply Attachment

`mention_current_sender` 是默认不 At 之上的例外：仅当群消息较多、回复可能难以定位，或确实
需要明确提醒当前发言者查看时，才调用一次。连续对话、紧邻提问的直接回答或普通闲聊不要调用。

用户明确要求文件、附件、导出或下载时，调用 `attach_text_file`。只附加本轮已经生成或整理
出的文本内容，不读取宿主文件。只有工具返回 `FILE_ATTACHED` 后才能确认文件已附加。

已有公开 HTTPS 文件直链时，调用 `attach_remote_file` 下载并发送原始二进制，不要只把
链接贴给用户，也不要声称附件工具只支持文本。该工具不限制文件类型，但链接必须公开可
访问，且文件不能超过部署配置的附件大小限制。默认使用 `send_as=auto`：常见图片扩展名
直接作为 QQ 图片发送，其他内容作为文件发送；用户明确要求原文件时使用 `send_as=file`，
无扩展名图链可用 `send_as=image`。下载内容不会转码。搜索结果若是详情页、Wiki 页或
文章页，先用 `fetch_url` 读取其 `links` 并找到原图/下载直链，不要直接把页面 URL 交给
附件工具。需要运行代码生成 PDF、Office、压缩包或其他二进制文件时，改用 `code` 或
`workspace` Skill 生成并导出。

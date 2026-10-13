---
name: reply
description: 将生成的文本内容作为 TXT、Markdown、JSON 或 CSV 文件发送给当前群聊。
allowed-tools: mention_current_sender attach_text_file
---

# Reply Attachment

最终文本默认不 At。只有群消息较多、回复可能难以定位，或确实需要明确提醒当前发言者查看时，
才调用一次 `mention_current_sender`。连续对话、紧邻提问的直接回答或普通闲聊不要调用。

用户明确要求文件、附件、导出或下载时，调用 `attach_text_file`。只附加本轮已经生成或整理
出的文本内容，不读取宿主文件。只有工具返回 `FILE_ATTACHED` 后才能确认文件已附加。

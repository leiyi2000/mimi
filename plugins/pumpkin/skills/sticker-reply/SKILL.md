---
name: sticker-reply
description: 在轻量、情绪明确的聊天场景中，从受控活跃库选择并发送一个表情包。
allowed-tools:
  - search_stickers
  - send_sticker
  - remove_quoted_sticker
agent-roles:
  - chat
load-policy: on-demand
triggers:
  - 轻松回应
  - 情绪回应
  - 祝贺
  - 安慰
  - 调侃
  - 引用表情并要求移除
examples:
  - 用一个合适的表情回应群友的玩笑
  - 在简短祝贺时发送表情
anti-triggers:
  - 严肃说明
  - 任务状态
  - 失败提示
  - 需要精确信息
priority: 85
---

表情包适合简短、轻量且情绪明确的聊天回应。先根据当前语义选择一个 category，调用
`search_stickers`；再根据候选的 summary、tags 和 tone 选择一个 `sticker_id`，调用
`send_sticker`。

不构造 sticker_id、媒体路径、URL、Base64 或 NapCat 参数。没有合适候选或不确定表情
含义时，改用普通文字。严肃信息、任务状态、失败说明和需要精确信息的回答必须使用文字。
每轮最多发送一个表情；表情可以单独发送，不要连续发送多个，也不要用表情替代必要事实。
发送成功后，把返回的 message_ids 纳入 `chat_turn_complete.sent_message_ids`。

用户明确要求移除表情包时，只能在其引用原始单个图片或扩展表情消息时调用
`remove_quoted_sticker`。不要传入或猜测 sticker_id、消息 ID、路径和媒体地址；目标和
引用结构由 Runtime 从当前消息中确定。群内任何用户都可以请求移除；未引用或引用多个
媒体时，根据工具错误简短说明，不尝试其他删除方式。

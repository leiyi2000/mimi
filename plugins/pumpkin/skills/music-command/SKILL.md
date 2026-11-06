---
name: music-command
description: 用户明确要求当前助手点歌时，代发 Music 插件已开放的真实点歌指令。
allowed-tools:
  - send_music_command
agent-roles:
  - chat
load-policy: on-demand
triggers:
  - 点歌
  - 播放歌曲
  - 来一首歌
examples:
  - 南瓜，点歌 Feel Special
  - 给我点一首月亮的脸
anti-triggers:
  - 用户直接发送点歌命令
  - 朗读文本
  - 生成配音
  - 创作音乐
priority: 85
---

只有用户明确要求当前助手点歌时才使用本 Skill。提取歌曲名和可选歌手名作为 `query`，
调用一次 `send_music_command`。Runtime 会从 Music 的命令清单读取并校验允许自动调用的
点歌触发词，再向当前会话发送一条真实可见的纯文本指令。

用户没有指向当前助手、而是直接发送现有 `点歌 <歌曲名>[-歌手名]` 命令时，不代发；
Music 插件会直接处理。朗读、配音和文字转语音属于音频生成任务，创作音乐也不属于本
Skill。

工具成功只表示点歌指令已经发出，不表示音乐搜索、播放地址或卡片签名已经成功。调用成功
后不要再发送确认、解释或完成承诺；将返回的 `message_ids` 原样提交给
`chat_turn_complete`，使用 `outcome=replied` 和 `continuation=closed`。Music 插件随后
发送的音乐卡片或错误提示才是真实终态。

同一次点歌必须复用稳定的 `idempotency_key`。发送结果未知时不得换键重试，避免重复点歌。

---
name: music-command
description: 用户明确要求当前助手点歌时，直接回复 Music 插件的点歌指令。
allowed-tools:
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

只有用户明确要求当前助手点歌时才使用本 Skill。提取歌曲名和可选歌手名，使用
`reply_and_complete` 直接回复一条纯文本 `点歌 <歌曲名>[-歌手名]`，其中
`response_mode=casual_chat`、`continuation=closed`。不要添加确认、解释、标点或其他文字。

用户没有指向当前助手、而是直接发送现有 `点歌 <歌曲名>[-歌手名]` 命令时，不回复；
Music 插件会直接处理。朗读、配音和文字转语音属于音频生成任务，创作音乐也不属于本 Skill。

该回复只触发 Music 插件继续搜索和发送卡片，不代表音乐卡片已经生成。Music 插件随后
发送的音乐卡片或错误提示才是真实终态。

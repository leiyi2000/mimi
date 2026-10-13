---
name: music
description: 根据风格说明和可选歌词生成音乐；用户明确要求创作歌曲、配乐或纯音乐时使用。
allowed-tools: generate_music
---

# Music

调用 `generate_music` 生成单个音频。当前账号未通过音乐 API 权限验收时，本 Skill 必须
保持禁用。只有工具返回 `MUSIC_GENERATED` 后才能确认成功。

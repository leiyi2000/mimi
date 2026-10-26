---
name: audio-render
description: 使用已配置的语音模型将文本合成为音频 Artifact。
allowed-tools:
  - generate_audio
  - save_default_voice
agent-roles:
  - task
load-policy: on-demand
triggers:
  - 生成语音
  - 文字转语音
  - 朗读
  - 音频合成
  - 调整音色
examples:
  - 把这段文字读出来
  - 生成一段旁白音频
  - 把当前声音调得更温柔并设为默认
anti-triggers:
  - 发送已有音频
  - 音色克隆
  - 音色设计
priority: 60
---

音频生成只能作为当前任务的已验证 Artifact 交付。先确认文本、音色、模型、语速和输出
格式，再调用工具。工具使用当前语音适配器的同步合成接口，支持 mp3、wav 和 flac；
生成结果会被解码、格式校验并写入当前任务 Artifact 目录。返回的 Artifact ID 才能用于
任务交付，交付时会转换为 NapCat `record` 消息段。

文本长度、语速、音量、音调、采样率、码率和声道必须使用工具允许的范围。模型服务未配置、
返回空音频、格式校验失败或合成失败时必须明确报告失败，不得伪造音频 URL。
`voice_modify` 可调整低沉/明亮、柔和/力量、浑厚/清脆及声音效果；
`timbre_weights` 可混合 2 至 4 个已有音色，且不能同时传单个 `voice_id`。

使用一套全局默认音色。普通调整只用于本次生成；只有用户明确要求以后默认使用时，
才调用 `save_default_voice` 原子保存完整默认配置。音色克隆、音色设计和异步长文本合成
不属于本 Skill。
同一次逻辑合成必须复用稳定的 `idempotency_key`；超时、恢复或重复工具轮次不得换键。
只有用户明确要求重新合成，或文本、音色与参数发生实质变化时才使用新键。

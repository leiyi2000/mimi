---
name: audio-render
description: 使用已配置的语音模型将文本合成为音频 Artifact。
allowed-tools:
  - generate_audio
agent-roles:
  - task
load-policy: on-demand
triggers:
  - 生成语音
  - 文字转语音
  - 朗读
  - 音频合成
examples:
  - 把这段文字读出来
  - 生成一段旁白音频
anti-triggers:
  - 发送已有音频
  - 音色克隆
  - 音色设计
priority: 60
---

音频生成只能作为当前任务的已验证 Artifact 交付。先确认文本、音色、模型、语速和输出格式，
再调用工具。工具使用当前语音适配器的同步合成接口，支持 mp3、wav 和 flac；生成结果会被解码、
格式校验并写入当前任务 Artifact 目录。返回的 Artifact ID 才能用于任务交付，交付时会转换为
NapCat `record` 消息段。

文本长度、语速、音量、音调、采样率、码率和声道必须使用工具允许的范围。模型服务未配置、
返回空音频、格式校验失败或合成失败时必须明确报告失败，不得伪造音频 URL。音色克隆、音色
设计和异步长文本合成不属于本 Skill。
同一次逻辑合成必须复用稳定的 `idempotency_key`；超时、恢复或重复工具轮次不得换键。
只有用户明确要求重新合成，或文本、音色与参数发生实质变化时才使用新键。

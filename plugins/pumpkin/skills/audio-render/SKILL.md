---
name: audio-render
description: 使用已配置的语音模型将文本合成为音频 Artifact。
allowed-tools:
  - generate_audio
  - clone_voice
agent-roles:
  - task
load-policy: on-demand
triggers:
  - 生成语音
  - 文字转语音
  - 朗读
  - 音频合成
  - 调整音色
  - 音色克隆
examples:
  - 把这段文字读出来
  - 生成一段旁白音频
  - 把当前声音调得更温柔
  - 用我发的授权录音克隆一个音色
anti-triggers:
  - 发送已有音频
  - 音色设计
  - 未授权的音色克隆
priority: 60
---

音频生成只能作为当前任务的已验证 Artifact 交付。Agent 始终通过统一的
`generate_audio` 参数结构表达文本、音色和可选效果；Runtime 负责把请求交给已配置的
Provider，并在当前 Provider 无法实现某项设置时返回明确错误。不得自行判断或探测
Provider。生成结果会经过格式校验并写入当前任务 Artifact 目录；返回的 Artifact ID
才能用于任务交付，交付时转换为 NapCat `record` 消息段。

文本长度、语速、音量、音调、采样率、码率和声道必须使用工具允许的范围。模型服务未配置、
返回空音频、格式校验失败或合成失败时必须明确报告失败，不得伪造音频 URL。
`voice_modify` 可调整低沉/明亮、柔和/力量、浑厚/清脆及声音效果；
`timbre_weights` 可混合 2 至 4 个已有音色，且不能同时传单个 `voice_id`。

合成参数只影响本次生成，不提供 Agent 可调用的默认配置保存工具。

Qwen Local 支持 `clone_voice`。参考音频必须来自当前任务源消息中的 `record` 或音频
`file` 段，不能提交 Base64、任意 URL 或本地路径。只有用户明确说明已获得说话人授权时，
才将 `consent_confirmed` 设为 true；不得自行推定。ICL 模式必须提供与录音逐字一致的
`reference_text`；无法提供时只能在用户接受质量较弱的前提下选择 x-vector 模式。克隆前
克隆成功后 Runtime 会原子记录该音色，后续未显式指定 `voice_id` 的合成自动使用最新
克隆音色。若音色 ID 已存在，直接向用户说明并请用户提供新 ID，不枚举本地全部音色。
音色设计、删除音色和异步长文本合成不属于本 Skill。
同一次逻辑合成必须复用稳定的 `idempotency_key`；超时、恢复或重复工具轮次不得换键。
只有用户明确要求重新合成，或文本、音色与参数发生实质变化时才使用新键。

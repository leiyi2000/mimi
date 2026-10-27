---
status: implemented
owner: Mimi maintainers
updated: 2026-10-27
commits: []
---

# Pumpkin Qwen 本地语音 Provider

## 1. 目标

Pumpkin 当前把 MiniMax TTS、MiniMax ASR、公共音频模型和音色配置集中在同一 Provider
模块。本特性新增 Qwen3-TTS 本地能力，并把音频子域整理为 `providers/audio/` 包：

- `MinimaxAudioProvider`：保留现有 MiniMax 合成、音色混合、情绪和变声能力；
- `QwenLocalAudioProvider`：调用 Qwen3-TTS 本地服务，支持授权音色克隆和 WAV 合成。

同一进程只装配一个 TTS Provider。Provider 切换不改变 Task、Artifact、幂等、Outbox 或
NapCat `record` 交付链路。MiniMax ASR 独立配置，不随 TTS Provider 切换。

## 2. 外部服务

Qwen3-TTS 使用 Apple Silicon MPS，作为宿主机独立进程运行，不进入 Mimi 镜像，也不由
Pumpkin 启停。Pumpkin 使用以下接口：

```text
POST /v1/audio/speech
{
  "input": "需要合成的文本",
  "voice_id": "已注册的音色 ID",
  "language": "Chinese | English | Auto"
}
-> audio/wav

POST /v1/voices
{
  "voice_id": "新音色 ID",
  "reference_audio_base64": "<Runtime 编码>",
  "reference_text": "与参考录音完全一致的文本",
  "x_vector_only_mode": false,
  "consent_confirmed": true
}
-> VoiceSummary

```

默认使用 ICL 克隆，要求参考文本与录音一致。`x_vector_only_mode=true` 时可省略参考文本，
但克隆效果通常较弱。本地服务固定返回 PCM WAV，不支持 MP3、FLAC、语速、音量、音调、
情绪、混音或效果器。

Mimi 容器通过配置的宿主机地址访问服务；插件在宿主机单独运行时使用本机地址。代码不自动
猜测部署环境，不保存本地服务的模型权重或音色档案。

## 3. Provider 分层

`providers/audio/` 按职责拆分：

```text
providers/audio/
  __init__.py       对外统一导出
  contracts.py      公共请求、结果、错误和 Provider 协议
  minimax.py        MiniMax TTS 与 ASR
  qwen.py           Qwen Local TTS 与音色克隆
  profile_store.py  最近克隆音色的原子持久化
```

`capabilities/audio.py` 根据强类型 `AudioProviderKind` 创建实现，不再依赖名为
`AudioProvider` 的 MiniMax 具体类。Agent 固定看到 `generate_audio` 和 `clone_voice`
两个工具；Provider 选择、默认输出和不支持参数的判断全部留在 Runtime 与适配器内部。

## 4. 配置

| 环境变量 | MiniMax | Qwen Local |
| --- | --- | --- |
| `PUMPKIN_AUDIO_PROVIDER` | `minimax` | `qwen-local` |
| `PUMPKIN_AUDIO_API_BASE_URL` | 必填 | 必填 |
| `PUMPKIN_AUDIO_API_KEY` | 必填 | 不使用 |
| `PUMPKIN_AUDIO_MODEL` | 必填 | 不使用，模型归本地服务 |
| `PUMPKIN_AUDIO_VOICE_ID` | 可选 | 必填，启动默认音色 |
| `PUMPKIN_AUDIO_LANGUAGE` | 不使用 | 可选，默认 `Chinese` |
| `PUMPKIN_AUDIO_TIMEOUT_SECONDS` | 适用 | 适用 |
| `PUMPKIN_AUDIO_MAX_TEXT_CHARS` | 适用 | 适用，且不得超过服务端 1000 字符 |

`AudioProviderKind` 和 `QwenAudioLanguage` 使用字符串枚举。已选择 Provider 但缺少必填配置、
语言非法或文本上限超过 Provider 硬边界时，启动配置校验明确失败；不得静默回退。

MiniMax ASR 继续使用 `PUMPKIN_AUDIO_TRANSCRIBE_*` 并装配
`MinimaxAudioTranscriptionProvider`。Qwen TTS 与 MiniMax ASR 可以同时启用。

## 5. 合成工具

`generate_audio` 对所有 Provider 使用同一工具名和同一 schema。统一请求可表达模型、
单音色或混音、语速、音量、音调、情绪、采样率、码率、格式、声道、语言、语言增强和
变声参数。Agent 不读取 Provider 类型，也不根据 Provider 改变调用方式。

Runtime 将统一请求映射到已装配 Provider。Provider 无法实现的显式参数在 HTTP 请求前返回
`AUDIO_SETTING_UNSUPPORTED`，不能静默忽略。MiniMax 调节参数只影响当前合成，不提供
Agent 可调用的默认参数保存工具。

`data/voice_profile.json` 只记录 Qwen 最近一次成功克隆并激活的音色：

```json
{
  "version": 2,
  "profiles": {
    "qwen-local": {
      "voice_id": "configured-voice",
      "language": "Chinese"
    }
  }
}
```

Qwen 尚未克隆音色时使用环境配置的启动音色。文件写入继续使用同目录临时文件、`fsync`、
`os.replace` 和父目录同步。历史 MiniMax 配置可被兼容读取，但合成链路不再使用或更新它。

## 6. 音色克隆

`clone_voice` 始终属于统一 Agent 工具契约。当前 Provider 不支持克隆时返回稳定的
`VOICE_CLONE_UNSUPPORTED`；支持时从当前任务源消息中的语音或音频文件创建音色。

`clone_voice` 只接收 `message_id`、`segment_index` 或 `segment_path`、`voice_id`、
`reference_text`、`x_vector_only_mode`、`consent_confirmed` 和 `idempotency_key`。
工具不接收 Base64、任意 URL 或宿主机路径。

Runtime 复用消息媒体的作用域和任务源授权：

1. `message_id` 必须属于当前 Task 的源消息或已授权补充消息；
2. 指定段必须是 `record` 或可识别为音频的 `file`；
3. 媒体继续受下载超时、私网地址和大小上限保护；
4. Runtime 在内存中编码 Base64，并直接交给 Provider；
5. 原始音频和 Base64 不写入 Session Event、日志、Artifact 或音色配置文件。

推荐参考音频为 10 至 30 秒、单人、无背景音乐、低混响。`consent_confirmed` 只有在用户
明确说明已获得说话人授权时才能为 `true`；缺少明确授权时返回
`VOICE_CLONE_CONSENT_REQUIRED`。ICL 模式缺少逐字参考文本时返回
`VOICE_CLONE_REFERENCE_TEXT_REQUIRED`。

本地服务成功创建音色后，Runtime 自动原子更新最近克隆音色；后续未显式传入 `voice_id`
的合成直接使用它。如果指针写入失败，克隆音色仍已存在，工具返回部分成功事实和音色 ID，
不能自动重试创建。克隆使用独立幂等键；请求结果未知时不得换键重试。

Pumpkin 不开放音色枚举或删除工具。音色 ID 冲突由 `VOICE_PROFILE_EXISTS` 明确返回；删除
会影响所有会话，继续由本地服务管理员执行。

## 7. 失败映射

Qwen Provider 使用 `httpx.AsyncClient`，禁用环境代理和重定向，并设置音频超时：

| 条件 | 错误码 | 可重试 |
| --- | --- | --- |
| 连接失败、超时、HTTP 503 | `AUDIO_UNAVAILABLE` | 是 |
| 合成音色不存在（HTTP 404） | `AUDIO_VOICE_NOT_FOUND` | 否 |
| 请求参数非法（HTTP 400/422） | `AUDIO_SETTING_INVALID` | 否 |
| 克隆音色 ID 已存在（HTTP 409） | `VOICE_PROFILE_EXISTS` | 否 |
| 克隆输入无效 | `VOICE_CLONE_INPUT_INVALID` | 否 |
| 缺少授权确认 | `VOICE_CLONE_CONSENT_REQUIRED` | 否 |
| HTTP 429 | `AUDIO_RATE_LIMITED` | 是 |
| 其他 5xx | `AUDIO_SYNTHESIS_FAILED` | 是 |
| 空响应、非 WAV、超过 Artifact 上限 | `AUDIO_INVALID_RESPONSE` | 否 |

错误不包含服务原始异常、响应正文、参考音频、音色档案路径或请求文本。成功响应必须校验
`Content-Type` 和 RIFF/WAVE 文件头，不能只凭 HTTP 200 登记 Artifact。

MiniMax Provider 保持现有 HTTP 状态和业务 `base_resp.status_code` 双重校验，拆分模块不能
改变既有错误码和重试语义。

## 8. 调用链

```text
Task Executor
  -> load_skill(audio-render)
  -> generate_audio
  -> AudioCapabilityPlugin
  -> unified request mapping and provider validation
  -> MinimaxAudioProvider | QwenLocalAudioProvider
  -> GeneratedAudio
  -> ArtifactService.write(audio)
  -> complete_task
  -> DeliveryService
  -> NapCat record
```

音色克隆链路：

```text
Task source message with record / audio file
  -> clone_voice(message reference, consent, clone options)
  -> task source authorization
  -> MessageMediaProvider.fetch
  -> bounded audio bytes
  -> QwenLocalAudioProvider.create_voice
  -> POST /v1/voices
  -> atomic latest-cloned-voice update
  -> VoiceSummary
```

Provider 只在启动阶段选择。一次任务内不自动降级或跨 Provider 重试，避免不同音色、格式或
内容复用同一幂等键。

## 9. 验收

1. MiniMax 现有 MP3、WAV、FLAC、音色混合和变声测试保持通过。
2. Qwen 合成请求只包含 `input`、`voice_id` 和 `language`，有效 WAV 可登记并交付。
3. Qwen 的 404、422、429、503、空响应和非 WAV 映射为明确错误。
4. Qwen schema 不暴露 MiniMax 专属参数，手工注入专属参数也会被拒绝。
5. 版本 1 默认音色文件只迁移到 MiniMax；两个 Provider 的默认音色互不覆盖。
6. Qwen 可从当前任务已授权的语音或音频文件创建 ICL 和 x-vector 音色。
7. 克隆音频、Base64 和本地档案路径不进入模型上下文、日志或 Pumpkin 持久化。
8. 未确认授权、跨任务媒体、非音频、重名音色和结果未知均明确失败且不重复创建。
9. Qwen TTS 与 MiniMax ASR 可以同时装配。
10. `.env.example`、插件 README、音频 Skill 和设计档案保持一致。
11. Pumpkin 全量测试、Compose 配置校验和 `git diff --check` 通过。

## 10. 非目标

- 不把 Qwen 模型、MPS 依赖或音色权重加入 Mimi 仓库和镜像；
- 不允许未明确确认说话人授权的音色克隆；
- 不把参考音频、Base64 或 Qwen 音色权重持久化到 Pumpkin；
- 不由 Pumpkin 删除 Qwen 音色；
- 不为 Qwen 增加转码、语速、音调或音频后处理；
- 不实现流式 TTS、长文本分段、Provider 自动降级或运行时热切换；
- 不改变 MiniMax ASR 和 QQ 入站语音读取流程。

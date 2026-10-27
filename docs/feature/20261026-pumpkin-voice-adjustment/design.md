---
status: implemented
owner: Mimi maintainers
updated: 2026-10-26
commits: []
---

# Pumpkin 音色调整

> 后续变更：`20261027-pumpkin-qwen-audio-provider` 移除了 Agent 可调用的全局默认参数保存
> 工具。MiniMax 调节参数现仅作用于单次合成；`voice_profile.json` 只用于记录 Qwen 最近
> 一次成功克隆的音色。本文件保留最初设计背景。

## 目标

南瓜使用一套全局统一音色，不按用户、群聊或私聊区分。扩展现有 `generate_audio`：

- 调整低沉/明亮、柔和/力量、浑厚/清脆；
- 添加空旷回音、礼堂广播、电话失真或机器人效果；
- 混合 2 至 4 个已有 MiniMax 音色。

当前 Token 已实测支持以上能力。音色设计和音色克隆返回业务码 `2061`，本次不接入。

## 全局配置

全局音色保存在 `data/voice_profile.json`，只包含 Voice ID、混音权重和声音参数，不保存
Token 或音频。所有语音默认读取该文件；本次生成显式参数可以临时覆盖默认值。

只有明确要求“设为南瓜默认音色”时才更新文件。写入使用同目录临时文件，完整写入后执行
`flush`、`fsync` 和 `os.replace`，最后同步父目录。写入失败或进程中断时保留上一版完整
文件或完整新文件，不会留下半写文件，也不静默重置损坏配置。

## 参数

```text
voice_modify:
  pitch: -100..100
  intensity: -100..100
  timbre: -100..100
  sound_effects:
    spacious_echo
    auditorium_echo
    lofi_telephone
    robotic

timbre_weights:
  - voice_id: string
    weight: 1..100
```

单个 `voice_id` 和 `timbre_weights` 二选一。混音包含 2 至 4 个不同音色。非法范围、未知
效果、重复音色和参数冲突必须在请求 MiniMax 前明确失败。

## 边界

- 不克隆或保存群友声音；
- 不接入文本生成新音色；
- 不改变群语音识别；
- 不修改现有 Artifact、幂等和 QQ `record` 交付流程。

## 验收

1. 单音色调整、声音效果和多音色混合可生成有效音频。
2. 全局默认音色可原子保存并在重启后继续使用。
3. 写入中断或失败不会破坏上一版配置。
4. MiniMax HTTP 200 下的业务错误不会被误判为成功。
5. Pumpkin 全量测试、Compose 校验和 `git diff --check` 通过。

---
status: draft
owner: Mimi maintainers
updated: 2026-10-26
commits: []
---

# Pumpkin 音色调整

## 1. 目标

扩展现有 `audio-render` 能力，让南瓜生成语音时可以：

* 调整声音的低沉/明亮、柔和/力量和浑厚/清脆；

* 添加空旷回音、礼堂广播、电话失真或机器人效果；

* 按权重混合 2 至 4 个已有 MiniMax 音色。

当前 MiniMax Token 已通过真实短音频验证上述三类能力。音色设计和音色克隆返回业务码
`2061`，不在本次范围内。

## 2. 工具契约

继续使用 `generate_audio`，保留 `voice_id`、语速、音量、音调、情绪和输出格式等现有
参数，新增：

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

`voice_id` 和 `timbre_weights` 二选一。混音必须包含 2 至 4 个不同的非空 Voice ID。
`voice_modify` 的数值和效果使用强类型模型与枚举校验；非法范围、未知效果、重复音色或
参数冲突在外部请求前失败。

单次生成可通过 `profile_name` 使用已保存预设。显式传入的调整参数覆盖预设中的对应值；
用户未明确要求保存时，临时调整不写入持久化文件。

新增预设工具：

| 工具                     | 行为                   |
| ---------------------- | -------------------- |
| `list_voice_profiles`  | 只读列出当前用户的预设和默认项      |
| `save_voice_profile`   | 保存或覆盖当前用户的命名预设，可设为默认 |
| `delete_voice_profile` | 删除当前用户的命名预设          |

保存和删除工具必须携带 `idempotency_key`。预设名称长度限制为 1 至 32 个字符，每个用户
最多保存 20 个预设；删除当前默认预设后清空默认指针，不隐式选择其他预设。

生成参数解析顺序固定为：

```text
本次显式参数
  > profile_name 指定的预设
  > 当前用户的默认预设
  > 内置默认音色和参数
```

## 3. 音色预设文件

音色预设不进入数据库，保存在 Pumpkin 运行数据目录的 `voice_profiles.json`。文件只保存
可复用的音色参数，不保存 Token、音频内容或供应商响应：

```json
{
  "version": 1,
  "profiles": {
    "<scope_id>:<sender_id>": {
      "default": "日常",
      "items": {
        "日常": {
          "voice_id": "male-qn-qingse",
          "speed": 1.0,
          "volume": 1.0,
          "pitch": 0,
          "voice_modify": {}
        }
      }
    }
  }
}
```

预设按 `scope_id + sender_id` 隔离。群成员只能读取和修改自己的预设，不能改变其他成员或
全局默认音色。只有用户明确要求“保存这个音色”或“以后默认用这个声音”时才写文件。

`VoiceProfileStore` 负责读取、校验和写入。更新流程必须：

1. 使用进程内异步锁串行化读改写；
2. 在目标文件同目录创建临时文件；
3. 写入完整 JSON 后执行 `flush()` 和 `os.fsync()`；
4. 使用 `os.replace()` 原子替换目标文件；
5. 对父目录执行 `fsync()`，确保目录项持久化；
6. 任一步失败时删除临时文件并保留上一版文件。

进程在替换前中断时最多遗留可清理的临时文件，正式文件仍是上一版；替换完成后中断时正式
文件是完整新版本。启动读取发现 JSON 损坏、版本不支持或字段非法时必须明确报错并拒绝
覆盖，不能静默重置用户预设。目标文件权限为 `0600`。

现有 `PUMPKIN_AUDIO_*` 环境变量继续负责 Provider、密钥和模型配置，不写入预设文件。
`data/` 已有持久化卷且处于 Git 忽略范围，不需要数据库迁移。

## 4. 调用链

```text
Task Agent
  -> load_skill(audio-render)
  -> 可选读取或保存当前用户的 VoiceProfile
  -> generate_audio(profile_name / explicit settings)
  -> AudioCapabilityPlugin 解析结构化参数
  -> AudioProvider 校验并调用 /v1/t2a_v2
  -> 校验音频格式和大小
  -> 当前 revision Audio Artifact
  -> NapCat record 交付
```

生成、幂等、Artifact 验收和 QQ 发送行为保持不变。工具仍只对 Task Agent 开放。

## 5. 失败行为

* 调整值超出 `[-100, 100]`：`AUDIO_VOICE_MODIFY_INVALID`；

* 未知声音效果：`AUDIO_VOICE_EFFECT_INVALID`；

* 混音数量、权重、Voice ID 或重复项非法：`AUDIO_TIMBRE_WEIGHTS_INVALID`；

* 同时使用单音色和混音：`AUDIO_VOICE_SELECTION_CONFLICT`；

* 预设不存在或不属于当前用户：`VOICE_PROFILE_NOT_FOUND`；

* 预设文件损坏或版本不支持：`VOICE_PROFILE_INVALID`；

* 预设原子写入失败：`VOICE_PROFILE_WRITE_FAILED`；

* MiniMax HTTP 200 但 `base_resp.status_code != 0`：`AUDIO_SYNTHESIS_FAILED`。

错误只向用户返回稳定错误码和友好提示，不暴露供应商原始响应、Token 或内部 Voice ID。

## 6. 非目标

* 不上传或克隆群友声音；

* 不接入文本描述生成新音色；

* 不改变群语音识别；

* 不把音色预设写入数据库；

* 不保存音频样本、试听音频或 Token；

* 不修改异步长文本与流式合成。

## 7. 验收

1. 单音色可以应用三项声音调整和四种声音效果。
2. 2 至 4 个已有音色可以按权重混合。
3. 参数冲突和所有非法输入在 HTTP 请求前明确失败。
4. MiniMax 业务错误不能被误判为成功。
5. 原有单音色生成参数和 Artifact 交付保持兼容。
6. 用户可保存、读取和设为默认自己的命名预设，其他用户无法访问。
7. 写入中断或 `os.replace()` 失败时，上一版正式文件保持字节完整。
8. 损坏或未知版本文件不会被静默覆盖。
9. `audio-render` Skill 和 Pumpkin README 准确说明支持边界。
10. Pumpkin 全量测试、Compose 配置校验和 `git diff --check` 通过。

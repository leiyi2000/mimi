---
status: implemented
owner: Mimi maintainers
updated: 2026-11-06
---

# Pumpkin 代发安全聊天指令

## 1. 背景

`commands.toml` 使用 `agent = true` 标识允许自动化调用方代为调用的无身份副作用指令，
但当前插件运行时只校验命令名唯一性，Help 目录也没有保留该字段。Pumpkin 因而看不到
Music 已开放的 `点歌` 能力，只能把点歌请求改写成语音生成或网页海报任务。

本特性先打通 Music 点歌这一条最小链路。用户明确向 Pumpkin 提出点歌请求时，Pumpkin
发送一条真实的 `点歌 <歌曲名>[-歌手名]` 文本消息，由 Music 插件沿用现有命令处理、
网易云搜索、播放地址查询和 QQ 音乐卡片签名链路。

## 2. 目标与非目标

目标：

- Pumpkin 通过按需 Chat Skill 代发 Music 已声明 `agent = true` 的 `点歌` 指令。
- 复用 Music 的查询、共享登录态、签名和用户可见错误，不复制音乐业务代码。
- 代发消息经 Pumpkin Outbox 持久化并保持幂等。
- Music 能处理 NapCat 对机器人自发文本产生的受支持事件形态。
- 普通用户直接发送 `点歌` 时仍由 Music 独立处理，不重复触发 Pumpkin。

非目标：

- 不把点歌改造成 Task Executor 任务或 Artifact。
- 不允许模型构造任意插件名、管理命令或原始 OneBot 消息。
- 不在 Pumpkin 内读取网易云 Cookie、签名密钥或调用 Music 外部接口。
- 不建立通用跨插件 RPC、共享业务代码或新的后台服务。
- 不改变 Music 登录、登出和账号信息命令的权限。

## 3. 调用链

```text
用户明确请求 Pumpkin 点歌
  -> Conversation Agent 发现 music-command Skill
  -> send_music_command(query, idempotency_key)
  -> Runtime 从 Music commands.toml 解析点歌命令
  -> 校验 agent = true
  -> Pumpkin Outbox 发送纯文本“点歌 <query>”
  -> NapCat 产生机器人自发消息事件
  -> Music 解析纯文本命令
  -> MusicService 搜索、取播放地址与详情、签名
  -> Music 发送 QQ Json 音乐卡片或明确错误
```

`send_music_command` 只返回代发文本的发送结果和消息 ID。它不把“命令已发送”等同于
“音乐卡片已发送”，Conversation Agent 不追加完成承诺。Music 后续的卡片或错误消息是
该命令的真实终态。

## 4. 能力与所有权

### 4.1 Skill

新增 `plugins/pumpkin/skills/music-command/SKILL.md`：

- 角色只允许 `chat`。
- 唯一工具是 `send_music_command`。
- 触发场景是用户明确向 Pumpkin 点歌。
- 用户直接使用现有 `点歌` 命令但没有指向 Pumpkin 时不代发。
- “朗读、配音、生成音频”属于 `audio-render`，不进入本 Skill。

普通点歌是短时确定性命令，不创建 Task，也不向 Task Executor 暴露该 Skill。

### 4.2 命令清单

Pumpkin 只读取 `commands.toml` 这一稳定元数据契约，不导入 Music 代码。运行时按插件
key `music` 和命令名解析实际触发词，并要求：

- 清单存在且结构有效；
- `agent` 明确为 `true`；
- 命令不是管理命令；
- 查询参数非空且长度受限。

校验失败时工具明确报错，不退化成语音生成、网页搜索或图片渲染。

### 4.3 状态所有权

- Music 继续拥有网易云 Cookie、搜索、播放地址和签名行为。
- Pumpkin 只拥有代发命令的 Outbox 记录与本轮回复账本。
- NapCat 继续拥有 QQ 消息发送和机器人自发事件。
- 两个插件不共享数据库、Cookie、Python 模块或运行时对象。

## 5. 消息与幂等

`send_music_command` 使用当前 scope 和模型提供的稳定 `idempotency_key` 写入 Outbox，
source 标记为 `agent_command`。重复工具调用复用同一 Outbox 结果，不重复点歌。

工具只能发送一个纯文本段，文本由 Runtime 使用清单中的命令名和受限查询参数拼接。模型
不能传 CQ 码、消息段、目标群号、用户 ID 或命令名。

Pumpkin 已在 Ingress 归档机器人自身消息后停止处理，因此代发文本不会再次唤醒
Conversation Agent。Music 发送的 Json 卡片也不匹配文本点歌命令，不形成循环。

## 6. Music 事件兼容

Music 当前只匹配单一 `Text` 的 `MessageEvent`。实现时先确认当前 NapCat SDK 对机器人
自发消息的类型：

- 若仍为 `MessageEvent`，复用现有处理器；
- 若为独立的 message-sent 事件类型，显式加入同一文本命令分发；
- 未知事件或混合媒体消息继续忽略。

事件兼容只扩展点歌入口，不放宽登录、登出和账号信息命令的管理员边界。

## 7. 失败行为

- 命令清单未开放 Agent 调用：Pumpkin 明确拒绝，不发送文本。
- Outbox 发送失败：本轮报告点歌指令未发出。
- Outbox 发送结果未知：不自动重发，避免重复点歌。
- Music 搜索、会员播放地址或签名失败：沿用 Music 的现有用户提示。
- 未观察到自发消息回流时，不声称卡片会生成；保留发送事实并通过测试或受控环境验证事件。

## 8. 验证

Pumpkin 测试：

- Skill 只对 Chat Agent 可见。
- 工具只允许 Music 的 `agent = true` 点歌命令。
- 命令参数为空、过长或含控制字符时拒绝。
- 同一幂等键只产生一条 Outbox 消息。
- 发送成功后记录回复消息 ID，且不创建 Task。
- 机器人自发消息被 Pumpkin 归档但不再次进入 Agent。

Music 测试：

- 用户纯文本点歌继续成功。
- 机器人自发纯文本点歌进入相同处理器。
- Json 卡片不触发命令循环。
- 管理命令权限不因事件兼容而放宽。
- 搜索、会员限制和签名失败仍返回现有提示。

交付验证：

```bash
cd plugins/pumpkin && uv run pytest
cd plugins/music && uv run pytest
git diff --check
```

真实群聊验证会发送可见消息，必须在明确允许的测试群中单独执行，不作为默认测试的一部分。

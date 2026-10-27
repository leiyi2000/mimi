---
status: implemented
owner: Mimi maintainers
updated: 2026-10-27
commits: []
---

# Pumpkin 表情包收集与发送设计

## 1. 目标

Pumpkin 从所有群聊消息中异步收集可复用表情包，维护一个跨群共享的活跃表情库，并向
Conversation Agent 提供受控的表情包检索和发送 Skill。

本设计采用以下产品约束：

- 活跃表情库全局最多 50 个，不按群分别计算；
- 活跃库未满时，合格且不重复的表情可以直接进入活跃库；
- 活跃库满后，每个自然日最多替换 10 个表情；
- 被替换的活跃表情移动到归档目录，不再提供给 Agent；
- 归档默认不自动删除，也不占用 50 个活跃名额；
- 收集、下载、分类和替换不阻塞消息归档与正常对话；
- Agent 只能使用 Runtime 返回的 `sticker_id`，不能构造媒体路径、URL、Base64 或
  NapCat `mface` 参数。

“后台进程”在首版中指由 Pumpkin 生命周期管理的持久化异步 worker，而不是新增独立
OS 进程。这样可以复用当前配置、数据库和媒体安全边界，并避免引入第二个插件运行单元。
worker 的队列和状态落库，进程退出或异常后可恢复。

## 2. 设计原则

1. `MessageIngress` 只做无网络的候选登记，不能等待媒体下载或模型分类。
2. 表情资产属于 Pumpkin，不依赖 Yasuo 或其他插件的私有归档实现。
3. 收集和发送使用同一份受验证资产；Agent 不接触来源消息和内部路径。
4. 分类负责表达语义，Runtime 负责容量、配额、幂等、安全和发送权限。
5. 普通群消息继续遵循安静参与策略。收集表情不能唤醒 Agent，表情 Skill 也不能绕过
   `ParticipationDecision` 主动插话。
6. 下载、分类或文件操作失败必须有明确状态和错误码，不能静默丢失候选。
7. 文件写入使用同目录临时文件、`fsync`、`os.replace` 和父目录同步。

## 3. 支持的入站表情

首版识别两类候选：

| 消息段 | 处理 |
| --- | --- |
| `mface` | 直接登记为候选，下载原始动画或图片后分类 |
| `image` | 登记为待判定候选，由分类器判断是否为可复用表情包 |

`face` 是 QQ 内置静态表情，不需要收集；文件、语音、JSON 和转发容器本身不进入表情库。
合并转发和引用内嵌媒体首版不收集，避免一个普通消息递归产生大量后台任务。机器人自身
发送的媒体、私聊媒体和已经由 Pumpkin 发出的表情都不收集，防止自循环。

候选必须经过现有 `MessageMediaProvider` 的 URL、私网、超时、大小、格式和像素限制。
分类器使用受限预览：静态图片使用规范化预览，GIF/WebP 等动画只使用首帧；发送时保留
经过验证的原始文件，避免把动画永久降级为静态图。

## 4. 分类与安全

新增 `StickerClassifier` 协议。默认实现复用已配置的视觉模型端点，但使用独立的结构化
分类请求，不创建 Conversation Turn，不写入 Session Event，也不触发聊天工具。

分类结果使用领域枚举而不是散落字符串：

```text
StickerCategory
  GREETING
  AGREEMENT
  CELEBRATION
  AMUSEMENT
  AFFECTION
  COMFORT
  SURPRISE
  CONFUSION
  EMBARRASSMENT
  SADNESS
  ANGER
  DISAGREEMENT
  TEASING
  SILENCE
  OTHER

StickerSafety
  ALLOWED
  REJECTED_PERSONAL
  REJECTED_SENSITIVE
  REJECTED_ADVERTISEMENT
  REJECTED_NOT_STICKER
  REJECTED_UNREADABLE
```

分类器返回：

```text
StickerClassification
  category
  tags
  summary
  tone
  confidence
  safety
```

`tags`、`summary` 和 `tone` 只描述画面表达，不包含发送者、群号、消息 ID、URL 或本地
路径。真人照片、可识别个人信息、二维码、广告、敏感内容、普通截图和普通照片不得进入
跨群共享活跃库。分类不可用或响应无法通过 schema 校验时，候选保持可重试失败状态，不以
`OTHER` 静默接纳。

## 5. 去重与资产状态

下载成功后同时计算：

- 原始文件 SHA-256，用于精确去重；
- 规范化首帧感知哈希，用于识别转码、缩放或轻微压缩后的近似重复。

精确或近似重复不会新增活跃名额，也不消耗每日替换配额，只更新已有资产的
`seen_count`、`last_seen_at` 和出现范围计数。已归档资产再次出现时可以成为待激活候选，
但重新进入活跃库仍视为一次替换。

领域状态分为：

```text
StickerCandidateStatus
  PENDING
  PROCESSING
  ACCEPTED
  REJECTED
  RETRYABLE_FAILED
  TERMINAL_FAILED

StickerAssetStatus
  STAGED
  ACTIVE
  ARCHIVED
  QUARANTINED
```

`QUARANTINED` 用于数据库记录存在但文件缺失、损坏或哈希不匹配的资产。此状态不允许
发送，也不自动删除证据。

## 6. 持久化模型

新增独立 Repository，不把收集策略放进 `PumpkinStore` 组合入口：

```text
StickerCandidateRow
  candidate_id
  source_message_id
  segment_index
  status
  attempt_count
  next_attempt_at
  asset_id
  error_code
  created_at / updated_at

StickerAssetRow
  sticker_id
  status
  content_sha256
  perceptual_hash
  content_type
  file_extension
  relative_path
  category
  tags
  summary
  tone
  confidence
  seen_count
  scope_count
  use_count
  first_seen_at / last_seen_at
  activated_at / last_used_at / archived_at

StickerReplacementRow
  replacement_id
  local_date
  activated_sticker_id
  archived_sticker_id
  reason
  created_at

StickerSendRow
  send_id
  sticker_id
  scope_id
  outbox_id
  status
  created_at / sent_at
```

候选唯一键为 `(source_message_id, segment_index)`；资产 SHA-256 唯一。每日替换数通过
`StickerReplacementRow.local_date` 在数据库事务内统计，不使用进程内计数。自然日按
`Asia/Shanghai` 计算，数据库时间仍保存 UTC。

来源消息只用于候选幂等、重试和审计。Skill 返回值以及 Agent 上下文不暴露来源 scope、
发送者或消息 ID。

## 7. 文件布局

所有文件位于 Pumpkin 的持久化 `data/`：

```text
data/stickers/
  active/<sticker_id>.<ext>
  staged/<sticker_id>.<ext>
  archive/YYYY-MM-DD/<sticker_id>.<ext>
  quarantine/<sticker_id>.<ext>
```

写入新文件时先在目标目录创建临时文件并完成格式、大小和哈希校验，再原子替换为正式
路径。替换过程先持久化 replacement intent，再执行同文件系统内的 `os.replace`，最后在
数据库事务中切换资产状态。启动恢复会对账 intent、状态和文件位置，修复中断在任意一步
的替换；不会出现同一资产同时为 ACTIVE 和 ARCHIVED。

归档目录不自动清理。后续如需容量治理，应增加独立保留策略和显式管理入口，不能让每日
替换顺带删除历史文件。

## 8. 后台 worker

`StickerCollectorWorker` 由 `PumpkinRuntime.start()` 启动、`close()` 取消并等待退出。
单个 worker 串行领取候选，避免多个分类请求同时争用 50 个名额和每日配额。

消息处理链路：

```text
NapCat group message
  -> MessageIngress
  -> archive(message)
  -> enqueue sticker candidates in one local DB operation
  -> normal participation decision

StickerCollectorWorker
  -> claim pending candidate
  -> MessageMediaProvider.fetch
  -> validate and persist staged media
  -> exact / perceptual dedup
  -> StickerClassifier.classify
  -> reject | activate | stage for replacement
```

候选领取使用显式租约。进程退出后，过期的 `PROCESSING` 候选恢复为 `PENDING`。网络、
限流和模型暂时不可用进入带退避的 `RETRYABLE_FAILED`；媒体过期、格式非法、安全拒绝和
确定性 schema 错误进入终态。日志只记录 candidate ID、状态、错误码和耗时，不记录图片、
群聊正文、URL 或模型原始响应。

## 9. 容量与替换

活跃资产少于 50 个时，新的合格唯一资产直接从 `STAGED` 进入 `ACTIVE`，不计入每日替换。

活跃资产达到 50 个后：

1. 合格候选保留为 `STAGED`；
2. worker 在事务内检查当天 replacement 数量；
3. 当天已替换 10 个时停止替换，但继续收集、去重和分类候选；
4. 未达上限时，对候选和活跃库执行确定性评分；
5. 只有候选质量严格高于最低分活跃资产，且不会降低分类覆盖时才替换；
6. 被替换资产移动到当日归档目录，候选移动到 active 目录；
7. 写入 `StickerReplacementRow` 后，当日计数增加 1。

评分只使用持久化事实：

- 分类置信度；
- 出现次数和出现范围；
- 最近出现时间；
- 当前分类是否缺失或过度重复；
- 活跃资产的使用次数和最近使用时间。

模型只负责单个媒体的语义分类，不直接决定删除哪个资产。没有合格替换目标时继续保留
候选，不为了凑满每日 10 个而强制替换。`STAGED` 候选按质量和时间有界保留，超出队列
上限时只删除尚未激活的 staged 文件，不影响 active 或 archive。

## 10. 表情包发送 Skill

新增 on-demand、仅 Conversation Agent 可用的 `sticker-reply` Skill：

```text
search_stickers(category, tags?, limit?)
  -> active sticker metadata

send_sticker(sticker_id, idempotency_key)
  -> Outbox send result

remove_quoted_sticker(idempotency_key)
  -> removed | already_removed | blocked_only
```

`search_stickers` 只查询 `ACTIVE` 资产，最多返回 8 个候选：

```text
sticker_id
category
tags
summary
tone
recently_used
```

不返回文件路径、来源群、来源成员、URL、Base64、哈希或归档资产。Agent 根据当前已接受
消息的语义选择 `category`，再从候选的 summary 和 tone 中选择 `sticker_id`。

`send_sticker` 执行以下硬校验：

1. 当前角色必须是 Conversation Agent；
2. 当前 Inbox 来源必须是用户消息，任务事件 Turn 不具备表情发送 capability；
3. `sticker_id` 必须仍为 `ACTIVE`，文件必须存在且哈希匹配；
4. 当前 Turn 最多发送一个表情，并计入 `max_reply_messages`；
5. 发送必须经过持久化 Outbox 和幂等键，不能直接调用 NapCat；
6. 只有 Outbox 明确 `SENT` 后才增加 `use_count` 和写入 `StickerSendRow.sent_at`；
7. 发送结果为 `UNKNOWN` 时不得盲目重发。

发送统一使用本地已验证媒体作为 NapCat `image` 段，不复用可能过期的远端 URL 或
`mface.key`。`NapCatSender` 继续把本地文件转换为 `base64://`，因此 NapCat 容器不需要
访问 Pumpkin 数据卷。

Skill 指导 Agent：

- 表情用于简短、轻量、情绪明确的聊天回应，可以单独发送；
- 严肃信息、任务状态、失败说明、需要精确信息的回答优先使用文字；
- 不确定含义时不发送，不连续发送多个表情，不用表情替代必要事实；
- 表情发送后仍通过 `chat_turn_complete` 正常结束本轮。

这些是 Agent 的语义选择原则；安静参与、Turn capability、回复预算和 Outbox 幂等仍由
Runtime 强制执行。

`remove_quoted_sticker` 不接受 `sticker_id`、消息 ID、路径或媒体 URL。Runtime 从当前
群消息已展开的引用载荷中解析唯一顶层 `image` 或 `mface`，下载后使用与收集链路相同的
内容及感知哈希定位资产。群内任何成员都可以请求移除，不要求其为原消息发送者。匹配资产
进入不可发送的 `REMOVED` 终态并清理文件；媒体尚未入库时也会写入哈希墓碑，后续 worker
在分类前直接拒绝，防止同一或近似内容重新进入跨群共享库。

## 11. 工具与能力边界

新增 `TurnCapability.STICKER_REPLY`。`ConversationTurnPolicy` 仅对
`AgentInboxSource.USER_MESSAGE` 且不是纯称呼消息的 Turn 授权。Task Event、Task
Executor、地址为空的控制 Turn 和未被参与策略接受的普通群聊都不能加载或执行
`send_sticker`。

`search_stickers` 是只读工具，但仍只属于 `sticker-reply` Skill。`send_sticker` 和
`remove_quoted_sticker` 是写工具，要求幂等键并由工具执行层再次检查 `STICKER_REPLY`
capability。模型即使手工构造工具调用也不能绕过权限或指定任意删除目标。

## 12. 失败语义

| 条件 | 错误码 | 行为 |
| --- | --- | --- |
| 没有可用活跃表情 | `STICKER_LIBRARY_EMPTY` | Agent 改用普通文字 |
| 分类无匹配结果 | `STICKER_NOT_FOUND` | 不猜测或构造 ID |
| ID 不存在或已归档 | `STICKER_NOT_ACTIVE` | 重新搜索一次 |
| 文件缺失或哈希不匹配 | `STICKER_ASSET_INVALID` | 资产进入 quarantine |
| 当前 Turn 未授权 | `STICKER_SEND_NOT_AUTHORIZED` | 不发送 |
| 当前 Turn 已发过表情 | `STICKER_TURN_LIMIT_REACHED` | 不再发送 |
| 回复预算不足 | `REPLY_BUDGET_EXCEEDED` | 复用现有回复预算错误 |
| Outbox 明确失败 | `OUTBOX_SEND_FAILED` | 可由当前幂等语义处理 |
| 发送结果不确定 | `OUTBOX_SEND_UNKNOWN` | 不自动重发 |

后台失败不向群聊发送提示。worker 连续异常由 Runtime 暴露为后台任务失败，不能让 worker
静默退出；表情能力失败不影响普通聊天、任务执行或现有媒体检查。

## 13. 启动与关闭

启动顺序：

```text
initialize database
  -> reconcile sticker files and interrupted replacements
  -> recover candidate leases
  -> discover Skills
  -> start Outbox
  -> start StickerCollectorWorker
  -> recover Agent inbox and tasks
```

关闭顺序先停止领取新候选，再等待当前文件原子写入或数据库事务结束，最后关闭模型
Provider 和数据库。`wait_for_idle()` 在测试中同时等待 worker 与 Outbox，后台异常由
`raise_background_failures()` 统一暴露。

## 14. 验收

1. 未唤醒的群消息仍只归档，但其中的 `mface` / `image` 候选会被异步登记。
2. 私聊、机器人自身消息、内置 `face` 和嵌套转发媒体不会进入候选队列。
3. 收集链路不等待下载和模型分类，分类失败不影响正常 Conversation Turn。
4. 精确重复和近似重复不会增加活跃数量，也不消耗替换配额。
5. 活跃库从 0 增长到 50 时不产生 replacement 记录。
6. 活跃库满后，同一 `Asia/Shanghai` 自然日最多产生 10 条 replacement 记录。
7. 被替换文件进入对应日期 archive，active 中始终不超过 50 个。
8. 进程在下载、文件移动和数据库状态切换各阶段退出后，重启均可恢复一致状态。
9. 分类拒绝真人照片、隐私内容、广告、普通截图和非表情图片。
10. Agent 只能检索 active 元数据并使用有效 `sticker_id`，不能读取内部路径和来源。
11. Task Event、Task Executor、未接受的群消息和纯称呼 Turn 无法发送表情。
12. 单 Turn 最多发送一个表情，且表情计入既有回复条数预算。
13. 表情经 Outbox 发送；成功、失败和结果未知保持既有幂等语义。
14. 媒体缺失或损坏会进入 quarantine，并返回明确错误，不降级为伪造成功。
15. Pumpkin 全量测试、`ruff`、Compose 配置校验和 `git diff --check` 通过。

## 15. 实现影响面

用户确认设计后，预计修改：

- `core/domain.py`：表情领域枚举、记录和 `STICKER_REPLY` capability；
- `core/config.py`：仅在确有部署差异时增加分类 worker 配置；
- `persistence/models.py`、`persistence/repositories/`：候选、资产、替换和发送记录；
- `runtime/ingress.py`：归档后无网络候选登记；
- `runtime/stickers.py`：worker、替换、恢复和文件一致性；
- `runtime/chat_tools.py`、`runtime/tools.py`：检索和发送工具；
- `runtime/turn_policy.py`：表情发送 capability；
- `runtime/app.py`：依赖装配和生命周期；
- `skills/sticker-reply/SKILL.md`：语义选择和工具说明；
- `tests/`：收集、分类、容量、配额、恢复、权限、Outbox 和防循环测试；
- 插件 README、根架构文档和特性索引：实现完成后同步当前事实。

## 16. 非目标

- 不为表情包新增聊天命令或管理页面；
- 不让 Agent 浏览归档、按任意资产 ID 删除或修改分类；
- 不从私聊、历史数据库全量回扫或其他插件目录收集媒体；
- 不允许表情收集唤醒 Conversation Agent；
- 不让分类模型直接选择被替换资产；
- 不在首版自动删除归档文件；
- 不新增独立 OS 服务或跨插件公共表情服务。

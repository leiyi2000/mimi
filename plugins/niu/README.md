# Niu

群聊 Agent 插件。发送 `南瓜 <内容>` 或 `@机器人 <内容>` 开启当前群的短时连续话题，
之后群友无需重复前缀即可追问或补充。文本触发前缀可通过 `NIU_TRIGGER_PREFIX` 修改。

## 能力

- 按群串行处理消息，不同群受控并发。
- 显式唤醒后开启群级连续话题。普通群消息经轻量相关性门控判定为忽略、静默吸收或立即
  回复；只 @ 其他成员和其他插件命令不会进入 Niu。话题空闲 120 秒或存活 15 分钟后自动
  结束，不依赖用户主动退出。
- 每次新的显式唤醒形成新 `topic_id`，并把紧邻的上一话题压缩为有界检查点。相关请求可
  承接目标、事实、结果和未完成项，无关请求会忽略检查点；旧原文不会直接混入新用户消息。
- 通过统一 Model Provider 使用 MiniMax、GPT 或 Claude 理解文本、图片、QQ 表情并完成
  多轮 Tool Use；Agent 不依赖厂商 SDK 响应类型。
- 固定提示词拆分为安全、任务执行、群聊表达和 NapCat 协议四层；能力目录、记忆、激活
  Skill 和任务状态按独立 section 动态注入，群级性格只能调整基础语气。
- 南瓜的基础性格是聪明能干但惦记饭点，闲聊时会嘴硬、接梗、轻轻吐槽，偶尔给群友起
  善意的临时外号；每条回复最多一个人格化笑点，角色感服从任务，不会用吃饭或摸鱼逃避
  执行和验证。
- 保存当前群的结构化性格、群共享事实和当前成员偏好。
- 默认提供 AnySearch 联网搜索，模型按需调用并保留结果标题、完整 URL 和摘要。通过
  Yasuo 专用接口查询当前群图文历史。
- 长耗时或多步骤任务会在执行 Tool 前发布一次简短计划；简单问题直接回答。
- Tool Use 按模型自然停止和 Finish Gate 持续执行，不设置固定调用轮次；回答截断时基于
  已有结果生成尽力而为的部分完成答复。
- 通过独立 `sandbox` 执行 Bash、Python、pip、curl 和 Git；既支持一次性完整脚本，也
  支持在单次 Agent Run 内分步读写、检查和导出文件。工作区在 Run 结束时删除，公网流量
  经过拒绝私网目标的代理，Sandbox 不挂载 Mimi 数据或业务密钥。
- 图片 Provider 已通过真实付费验收并默认启用，支持文生图及单张人物参考图生成新场景；
  音乐因当前账号无 API 权限保持关闭。
- Skill 只随仓库代码发布，不支持搜索、下载或运行时安装。首轮仅注入运行控制和动态
  `mimi` Skill；搜索、记忆、附件、渲染、代码、媒体等能力由目录语义或显式触发词按需
  激活。启动时严格校验 Skill 文件、元数据和 Tool 绑定，配置错误直接失败。
- 普通回复保持口语化，闲聊和简单问答优先一句、40 字以内，默认不 @；模型只在确实需要
  提醒时自主选择 @ 当前发言者。生成结果可发送文本、图片、语音和任意类型文件；公网
  HTTPS 文件直链可下载原始字节，图片默认直接作为 QQ 图片发送，其他内容作为文件附件；
  PDF、Office、压缩包等生成文件可由 Sandbox 导出。多分段、对比或长步骤等复杂回答优先
  通过 pytakumi 渲染成中文长图；自动渲染默认从 200 字开始，纯图片回复不再附加状态说明。
- URL 只在搜索、读取和下载工具之间内部流转，最终群聊正文统一移除 HTTP(S)、`www.` 和
  Markdown 链接；索要图片链接时直接发送图片，不返回 URL。
- 所有群消息中的 `MFace`，以及 NapCat 以 `Image(sub_type=1)` 或
  `Image(file=marketface)` 上报的自定义表情，都会按群去重收集，不要求先 @ 机器人或
  命中文本前缀；每群每天最多后台调用模型分类 5 张，且不阻塞消息处理。模型也可识别
  触发消息中的其他反应图，自主收藏、整理并按语境回复。表情包 Skill 默认激活；庆祝、
  赞同、震惊、无语、调侃等有明确情绪落点的场景会主动检索，严肃求助和连续回复时保持克制。
- 启动时读取各插件 `commands.toml`，动态生成 `mimi` Skill，可调用明确标记为
  `agent = true` 的查询和展示指令，例如 `点歌`、`APEX轮换`；该 Skill 默认注入，
  不依赖模型先完成能力路由。
- 机器人自己发送的群消息在解析和表情收集前被忽略，避免回复或指令再次触发 Niu。

完整 Agent 边界见
[特性设计](../../docs/feature/20261013-niu-agent-loop/design.md)。

## 使用

| 输入 | 说明 |
| --- | --- |
| `南瓜 <内容> [图片/表情]` | 对话，并由模型按需调用当前启用的 Skill |
| `@机器人 <内容> [图片/表情]` | 直接提及机器人发起同样的对话 |
| `<无前缀的相关追问或补充>` | 在活跃话题内由任意群友继续；补充信息可能静默纳入上下文 |
| `南瓜 结束对话` | 可选地提前关闭当前话题；正常情况下会自动超时 |
| `南瓜 看下 APEX 地图轮换` | 由模型调用 Mimi 的 `APEX轮换` 指令 |

活跃话题按群共享，不继承发起人的身份或管理员权限。明确前缀、直接 @ 和引用消息会立即
进入当前处理链；其他消息先使用当前话题摘要和最近两轮做受限分类，低置信或无关消息不会
进入 Niu 历史。每次再次使用 `南瓜` 都会开启新话题，但新一轮可按需读取上一话题的结构化
检查点，因此群友可以直接补充“只在生产环境出现”之类的信息，无需重述全部背景。

图片和表情可以附在触发消息中；Unicode Emoji 作为文本输入，QQ 小表情和商城表情作为
结构化描述输入。当前轮图片以多模态内容发送给模型，短期历史只保留图片摘要，不保存图片
二进制。原始消息中非机器人的 At 会按原位置转换为“群昵称 + QQ ID”；引用消息的发送者、
文本、表情和图片也会作为独立上下文传入。当前消息与引用消息共用图片数量和大小限制。
群历史查询同样可把 Yasuo 已归档图片作为多模态内容交给模型，并保留纯图片消息；返回
图片复用 `NIU_MAX_INPUT_IMAGES` 和 `NIU_INPUT_IMAGE_MAX_BYTES` 限额，缺失、损坏或超限
图片会以省略数量呈现，不会把 Base64 混进历史文本。
若成员先单独发送普通图片，再在缓存有效期内发送一条不含图片的 Niu 触发消息，适配层会
把该成员最近一批图片作为本轮输入并在成功读取后消费；缓存按群和成员隔离。
要求参考人物形象生成新场景时，可直接附图或引用含图消息；模型会从当前消息和引用消息的
统一图片序号中选择一张作为人物主体参考。该能力用于保持人物或角色关键特征，不承诺物体、
构图、文字或像素级编辑完全一致。
普通群消息中的商城表情和 `sub_type=1`/`marketface` 图片会按群去重收集；图片表情由
后台模型补充摘要、情绪和标签，达到容量后随机淘汰一条旧表情。照片、截图等普通图片不会
被动保存。空内容会返回用法提示。

## 关键配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NIU_MODEL_PROVIDER` | `minimax` | 文本模型协议：`minimax`、`openai` 或 `anthropic` |
| `NIU_MODEL` | 随 Provider 变化 | 文本模型名称 |
| `NIU_MODEL_BASE_URL` | 随 Provider 变化 | 文本模型 API 地址 |
| `NIU_MODEL_API_KEY` | 无 | 文本模型 Token，必需 |
| `DEBUG` | `false` | 开启 DEBUG 日志，包括每轮模型输入和输出 |
| `ANYSEARCH_API_KEY` | 无 | AnySearch API Key，必需 |
| `MINIMAX_API_KEY` | 兼容读取 `ANTHROPIC_API_KEY` | MiniMax 图片和音乐 Token |
| `MINIMAX_API_BASE_URL` | `https://api.minimaxi.com/v1` | MiniMax 图片和音乐端点 |
| `NIU_TRIGGER_PREFIX` | `南瓜` | 文本触发前缀 |
| `NIU_CONTINUOUS_CHAT_IDLE_TTL` | `120` | 连续话题空闲超时秒数，范围 `30..600` |
| `NIU_CONTINUOUS_CHAT_MAX_TTL` | `900` | 连续话题最长存活秒数，范围 `120..3600` |
| `NIU_MAX_INPUT_IMAGES` | `4` | 单轮消息及群历史最多输入图片数 |
| `NIU_INPUT_IMAGE_MAX_BYTES` | `5242880` | 单张消息或群历史图片最大字节数 |
| `NIU_AUTO_RENDER_MIN_CHARS` | `200` | 最终正文自动渲染为图片的最少字符数 |
| `NIU_MEDIA_MAX_BYTES` | `20971520` | 生成媒体的下载上限；Sandbox 文件另受其附件上限约束 |
| `NIU_GROUP_HISTORY_ENABLED` | `true` | 启用群历史 Skill |
| `YASUO_API_URL` | `http://127.0.0.1:6273` | Yasuo API 地址 |
| `SANDBOX_TOKEN` | 无 | Mimi 与 Sandbox 间的 Bearer Token，必需 |
| `NIU_SANDBOX_TIMEOUT` | `30` | 单次代码执行秒数，上限 60 |
| `NIU_IMAGE_ENABLED` | `true` | 图片 Skill 开关；当前账号已完成真实验收 |
| `NIU_MUSIC_ENABLED` | `false` | 音乐权限验收后显式开启 |
| `NIU_STICKERS_ENABLED` | `true` | 群表情包收集、整理和回复开关 |
| `NIU_STICKER_MAX_ITEMS` | `200` | 每群最多保存的表情数量 |
| `NIU_STICKER_DAILY_UPDATES` | `30` | 每群每日收藏、整理和删除次数上限 |
| `NIU_STICKER_DAILY_CLASSIFICATIONS` | `5` | 每群每日后台 AI 分类次数上限 |

默认配置使用 MiniMax。切换 GPT 时设置 `NIU_MODEL_PROVIDER=openai`、`NIU_MODEL` 和
`NIU_MODEL_API_KEY`；切换 Claude 时设置 `NIU_MODEL_PROVIDER=anthropic`。OpenAI 兼容
服务可通过 `NIU_MODEL_BASE_URL` 指向自定义端点。旧的 `MINIMAX_MODEL`、
`ANTHROPIC_BASE_URL` 和 `ANTHROPIC_API_KEY` 在 MiniMax 默认模式下继续兼容。

排查 Agent 时可临时设置 `DEBUG=true`。DEBUG 日志按 `run_id`、`round_index` 和
`exchange_id` 串联完整过程，包括每轮提示词、消息、工具定义、Provider 原始响应、
标准化模型输出、工具参数与结果、停止原因、耗时和 Token 用量。Provider 原始响应会保留
模型实际返回的 thinking 等扩展字段。详细事件使用单行 JSON，按 `event=model.input`、
`event=model.provider_response`、`event=model.output`、`event=tool.input` 和
`event=tool.output` 筛选。图片 Base64 只记录媒体字节数，常见凭据字段会脱敏。日志仍可能
包含群聊、记忆和工具结果正文，只应在受控环境临时开启，排查结束后关闭。

其他限额见 [部署规范](../../docs/spec/deploy-spec.md)。运行状态写入 `data/`，不会持久化
完整群聊上下文。

## 验证

```bash
cd plugins/niu
uv run pytest
```

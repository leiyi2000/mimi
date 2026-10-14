# Niu

群聊 Agent 插件。发送 `南瓜 <内容>` 或 `@机器人 <内容>` 触发。文本触发前缀可通过
`NIU_TRIGGER_PREFIX` 修改。

## 能力

- 按群串行处理消息，不同群受控并发。
- 通过统一 Model Provider 使用 MiniMax、GPT 或 Claude 理解文本、图片、QQ 表情并完成
  多轮 Tool Use；Agent 不依赖厂商 SDK 响应类型。
- 固定提示词拆分为安全、任务执行、群聊表达和 NapCat 协议四层；能力目录、记忆、激活
  Skill 和任务状态按独立 section 动态注入，群级性格只能调整基础语气。
- 保存当前群的结构化性格、群共享事实和当前成员偏好。
- 默认提供 AnySearch 联网搜索，模型按需调用并保留结果标题、完整 URL 和摘要。通过
  Yasuo 专用接口查询当前群历史。
- 长耗时或多步骤任务会在执行 Tool 前发布一次简短计划；简单问题直接回答。
- 工具轮次耗尽或回答截断时，基于已有结果生成尽力而为的部分完成答复。
- 通过独立 `sandbox` 执行 Bash、Python、pip、curl 和 Git；既支持一次性完整脚本，也
  支持在单次 Agent Run 内分步读写、检查和导出文件。工作区在 Run 结束时删除，公网流量
  经过拒绝私网目标的代理，Sandbox 不挂载 Mimi 数据或业务密钥。
- 图片 Provider 已通过真实付费验收并默认启用，支持文生图及单张人物参考图生成新场景；
  音乐因当前账号无 API 权限保持关闭。
- Skill 只随仓库代码发布，不支持搜索、下载或运行时安装。
- 普通回复保持单段、1–3 个短句的聊天语气，默认不 @；模型只在确实需要提醒时自主选择
  @ 当前发言者。生成结果可发送文本、图片、语音，以及 TXT、Markdown、JSON 或 CSV
  文件。多分段、对比或长步骤等复杂回答优先通过 pytakumi 渲染成中文长图，聊天正文只
  保留一句引导。
- 所有群消息中的 `MFace`，以及 NapCat 以 `Image(sub_type=1)` 或
  `Image(file=marketface)` 上报的自定义表情，都会按群去重收集，不要求先 @ 机器人或
  命中文本前缀；每群每天最多后台调用模型分类 5 张，且不阻塞消息处理。模型也可识别
  触发消息中的其他反应图，自主收藏、整理并按语境回复。
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
| `南瓜 看下 APEX 地图轮换` | 由模型调用 Mimi 的 `APEX轮换` 指令 |

图片和表情可以附在触发消息中；Unicode Emoji 作为文本输入，QQ 小表情和商城表情作为
结构化描述输入。当前轮图片以多模态内容发送给模型，短期历史只保留图片摘要，不保存图片
二进制。原始消息中非机器人的 At 会按原位置转换为“群昵称 + QQ ID”；引用消息的发送者、
文本、表情和图片也会作为独立上下文传入。当前消息与引用消息共用图片数量和大小限制。
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
| `ANYSEARCH_API_KEY` | 无 | AnySearch API Key，必需 |
| `MINIMAX_API_KEY` | 兼容读取 `ANTHROPIC_API_KEY` | MiniMax 图片和音乐 Token |
| `MINIMAX_API_BASE_URL` | `https://api.minimaxi.com/v1` | MiniMax 图片和音乐端点 |
| `NIU_TRIGGER_PREFIX` | `南瓜` | 文本触发前缀 |
| `NIU_MAX_INPUT_IMAGES` | `4` | 单条消息最多输入图片数 |
| `NIU_INPUT_IMAGE_MAX_BYTES` | `5242880` | 单张输入图片最大字节数 |
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

其他限额见 [部署规范](../../docs/spec/deploy-spec.md)。运行状态写入 `data/`，不会持久化
完整群聊上下文。

## 验证

```bash
cd plugins/niu
uv run pytest
```

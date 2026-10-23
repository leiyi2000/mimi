# Pumpkin

Pumpkin 是 Mimi 的 QQ 群聊和私聊 AI 插件。每条消息先写入
`data/pumpkin.db`，再进入当前作用域唯一的持久化 Conversation Inbox：

- Conversation Agent 负责自然对话和任务识别；每条用户消息形成一个可恢复 Turn，
  模型与工具过程写入追加式 Session Event。
- 任务由 Conversation Agent 创建，Task Executor 独立执行并只提交 TaskEvent 和
  已验证 Artifact，不具备 QQ 发送权限。
- TaskEvent 使用固定 ID 写入持久化 Conversation Inbox，不占用用户消息 Inbox；启动时
  会对账补投未通知的等待、失败和完成事件。Conversation Agent 根据持久化事实表达终态，
  并只交付该 revision 已显式通过验收的 Artifact。
- 所有聊天和任务交付经持久化 Outbox 发送；正文超过 80 个字符时由 Outbox 自动渲染
  为图片。发送结果不确定时记为 `unknown`，不会盲目重发。
- 任务创建成功后由 Runtime 通过 Outbox 立即发送结合任务目标的开始确认，并直接结束
  当前 Conversation Turn，避免额外模型回合拖慢首响或重复更新任务。
- 删除运行中的任务会先持久化删除状态，再立即取消 Task Agent 和后台执行协程。
- 任务执行失败后只发送事实性失败说明，不会因为普通追问自动重跑；用户明确要求重试或继续
  后，任务才会重新进入队列；内部错误码和原始异常不会出现在用户消息中。
- Conversation Turn 因模型或运行时错误失败时会发送明确回执，不会静默完成。
- 模型上下文保留当前 Turn 和最近 4 个已结束 Turn；完整 Session Event 仍持久化用于恢复。
- 群聊只在 `@` 助手或使用助手名前缀时激活。激活后由 Conversation Agent 判断群消息
  是否仍属于当前话题，不按单个发言人隔离；模型判定话题结束后立即休眠，再次参与需要
  重新明确唤醒。私聊首条有效消息可激活。
- `闭嘴` 会把当前作用域写入持久休眠状态；重启不会自动解除，直到下一次明确
  `@` 或助手名前缀才恢复。其他自然表达由模型理解并决定是否结束当前话题。
- 普通聊天不使用 QQ 引用回复。任务事件保留原请求消息和请求人上下文，由 Conversation
  Agent 在独立 Turn 中定向表达和交付，群内其他成员不会继承任务完成话术。
- 任务状态查询由 Conversation Agent 根据上下文调用 `get_task_status`，Runtime 只校验
  任务作用域、revision 和工具权限，不通过中文关键词改写用户意图。
- 用户否定既有结果后，Conversation Agent 会把纠正关联到原任务并创建新 revision。
  被否定 Artifact 的内容、来源和生成策略会失效；连续两次违反同一纠正约束时任务停止，
  不再继续交付同类错误结果。
- 模型使用 OpenAI-compatible Chat Completions 协议，未配置模型时会明确记录不可用，
  不会伪造回复或任务完成。

## 配置

基础连接沿用 `NAPCAT_HOST`、`NAPCAT_PORT` 和 `NAPCAT_TOKEN`。Pumpkin 相关配置包括：

| 环境变量 | 用途 |
| --- | --- |
| `PUMPKIN_ASSISTANT_NAME` | 动态助手名称，同时作为群聊唤醒前缀 |
| `PUMPKIN_MODEL_PROVIDER`、`PUMPKIN_MODEL` | 默认模型配置 |
| `PUMPKIN_MODEL_BASE_URL`、`PUMPKIN_MODEL_API_KEY` | OpenAI-compatible 端点 |
| `PUMPKIN_CHAT_REASONING_EFFORT`、`PUMPKIN_TASK_REASONING_EFFORT` | MiniMax M3.1 Flash 思考深度；默认聊天 `low`、任务 `max` |
| `PUMPKIN_CHAT_MAX_TOKENS`、`PUMPKIN_TASK_MAX_TOKENS` | 单次模型生成预算，包含 thinking；默认聊天 `4096`、任务 `8192` |
| `PUMPKIN_CHAT_TIMEOUT_SECONDS`、`PUMPKIN_TASK_TIMEOUT_SECONDS` | 单次聊天和任务模型请求超时；聊天默认 15 秒且超时不重试 |
| `PUMPKIN_SINGLE_TASK_TIMEOUT_SECONDS` | 单个任务总执行时限 |
| `PUMPKIN_MAX_STEPS` | Conversation Agent 和 Task Executor 的统一最大模型步骤数，默认 36；运行到 70% 和 85% 时会提醒模型收敛 |
| `PUMPKIN_CHAT_FALLBACK_BASE_URLS`、`PUMPKIN_TASK_FALLBACK_BASE_URLS` | 逗号分隔的备用端点 |
| `PUMPKIN_SEARCH_API_URL`、`PUMPKIN_SEARCH_API_KEY` | 可选网页搜索服务，未配置时明确返回未配置 |
| `PUMPKIN_FETCH_MAX_BYTES`、`PUMPKIN_IMAGE_FETCH_MAX_BYTES` | 网页正文与网页图片的独立下载上限，默认分别为 1 MiB 和 5 MiB |
| `PUMPKIN_CODE_RUNNER_URL`、`PUMPKIN_CODE_RUNNER_TOKEN` | 独立代码沙箱服务，默认使用 Compose 内部地址 |
| `PUMPKIN_IMAGE_PROVIDER`、`PUMPKIN_IMAGE_API_BASE_URL`、`PUMPKIN_IMAGE_API_KEY`、`PUMPKIN_IMAGE_MODEL` | 可选图片生成适配器 |
| `PUMPKIN_AUDIO_PROVIDER`、`PUMPKIN_AUDIO_API_BASE_URL`、`PUMPKIN_AUDIO_API_KEY`、`PUMPKIN_AUDIO_MODEL` | 可选音频生成适配器 |
| `PUMPKIN_MEDIA_TIMEOUT_SECONDS`、`PUMPKIN_MEDIA_MAX_BYTES` | 按需查看消息图片的请求超时和大小上限 |
| `PUMPKIN_DATABASE_URL` | 默认 `sqlite://data/pumpkin.db` |

配置只读取 `PUMPKIN_*` 变量。容器环境中的反引号包裹值会被自动去除。
Conversation Agent 的稳定决策原则和女友式人格由独立 Prompt 常量固定；具体操作指导由
对应工具描述维护，变化的消息、任务事实和能力目录通过结构化动态上下文提供。

完整边界、数据模型和调用链见
[`docs/feature/20261023-pumpkin-harness-alignment/design.md`](../../docs/feature/20261023-pumpkin-harness-alignment/design.md)。

网页搜索、代码执行、图片生成、HTML 图文渲染和音频合成均为按需 Skill。代码执行容器可
正常访问网络并读写自身文件系统，但不会挂载宿主目录或读取 Mimi 的环境变量；普通相对路径
文件会作为执行产物收集。生成图片、音频和代码文件会先登记为候选 Artifact，只有经过当前
revision 的完成验收并满足全部用户纠正条件后，才能通过 NapCat 发送。本地 Artifact 在发送前
转换为 `base64://` 数据，避免依赖 NapCat
容器可见的宿主路径。图片和音频适配器的 Provider、Base URL、API Key、Model 未完整配置时，
对应 Skill 不会进入 SkillCatalog。长文本图片使用完整 CJK 字体，避免因字体子集缺字导致
文字错位或空白。`media-inspection` 也是按需 Skill，模型只有在需要理解
图片时才会现场读取当前作用域的媒体引用；不做后台预下载，引用过期时返回明确错误。引用
消息和合并转发会有限深度解析，超过深度或节点预算会保留截断标记。

## 命令

| 命令 | 说明 |
| --- | --- |
| `闭嘴` | 让当前会话持久休眠，直到再次明确唤醒 |

## 测试

```bash
uv run pytest
```

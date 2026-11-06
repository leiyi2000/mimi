# Pumpkin

Pumpkin 是 Mimi 的 QQ 群聊和私聊 AI 插件。每条消息先写入
`data/pumpkin.db`，再进入当前作用域唯一的持久化 Conversation Inbox：

- Conversation Agent 负责自然对话和任务识别；每条用户消息形成一个可恢复 Turn，
  模型与工具过程写入追加式 Session Event。
- 任务由 Conversation Agent 创建，Task Executor 独立执行并只提交 TaskEvent 和
  已验证 Artifact，不具备 QQ 发送权限。群聊和私聊使用同一套 scope 隔离、任务执行与
  交付链路；私聊结果直接发送，不附加群聊式引用或 `@`。
- TaskEvent 全量持久化；等待输入、失败、暂停和无产物的完成事件使用固定 ID 写入 Conversation
  Inbox，不占用用户消息 Inbox。候选 Artifact、进度和描述更新仅供任务查询与审计，不会
  触发回复；启动时只补投未通知的用户可见事件。Conversation Agent 根据持久化事实表达
  终态。带产物的完成事件由 Runtime 直接交付该 revision 已显式通过验收的 Artifact，
  不等待聊天队列；交付失败会明确提示，恢复时不会重复发送结果未知的交付。
- 所有聊天和任务交付经持久化 Outbox 发送；正文超过 80 个字符时由 Outbox 自动渲染
  为图片。普通聊天只接受文本、`@` 和内置表情段，不解析模型拼接的 CQ 控制码；任务
  图片、文件和语音必须通过已验证 Artifact 交付。发送结果不确定时记为 `unknown`，
  不会盲目重发。
- 任务创建成功后由 Runtime 通过 Outbox 立即发送 Conversation Agent 提供的简短自然确认，
  并关闭当前群聊话题，避免额外模型回合拖慢首响或让后续普通聊天继承任务权限。即使确认
  发送失败或结果未知，已创建任务对应的话题仍保持关闭。内部任务目标不会作为确认文案展示；
  防御性处理空白确认时只发送不含任务内容的短兜底。
- 删除运行中的任务会先持久化删除状态，再立即取消 Task Agent 和后台执行协程。
- 任务执行失败后只发送事实性失败说明，不会因为普通追问自动重跑；用户明确要求重试或继续
  后，任务才会重新进入队列；内部错误码和原始异常不会出现在用户消息中。
- Conversation Turn 因模型或运行时错误失败且本轮尚未成功发送消息时会发送明确回执；
  已有确认回复时保留内部失败事实，但不再补发相互矛盾的失败提示。
- 私聊模型上下文保留当前 Turn 和最近 4 个已结束 Turn；群聊只保留当前 `topic_id` 的
  Turn，TaskEvent 不引入无关聊天。引用已确认的助手消息时只恢复该问题与实际回答作为
  参考，不恢复历史任务权限；没有引用的新唤醒继续隔离旧话题。完整 Session Event 仍持久化用于恢复。
- 群聊只在 `@` 助手、使用助手名前缀或回复助手消息时明确激活，每次明确激活创建新的
  `topic_id`。普通回答、闲聊、沉默、拒绝和失败默认关闭话题；只有助手在可见回复中明确
  等待请求人补充时提交 `requester_input`，或明确邀请群成员参与时提交 `group_open`，
  后续消息才能在默认 5 分钟滑动窗口和 15 分钟硬上限内进入。追问和扩散共用默认 6 Turn
  预算，扩散每轮最多回复一条；排队消息在执行前再次校验话题，已关闭或过期时直接丢弃。
  同一成员仅发送图片、QQ 表情、扩展表情或 emoji 时也只归档，不唤醒模型且不消耗预算。
- 普通文本回复必须声明 `casual_chat`、`professional_answer` 或 `task_status`。
  日常交流不主动扩写成专业分析；明确要求分析、解释、建议或资料时才使用专业回答。回复
  深度由问题决定，不按 `DIRECT`、`FOLLOW_UP` 或 `EXPANSION` 设置字符数限制。
  文本回答优先使用 `reply_and_complete`，一次提交回复与话题状态；Runtime 自动管理发送
  确认和结束。普通正文也经过同一校验发送，空输出不会被标为成功，已发送回复不会重复。
- 用户明确要求 Pumpkin 点歌时，Conversation Agent 可按需加载 `music-command` Skill，
  由 Runtime 从 Music `commands.toml` 校验 `agent = true` 后，经 Outbox 代发一条真实
  `点歌 <歌曲名>[-歌手名]` 文本。该能力不创建任务、不生成音频，也不接触 Music 的
  Cookie、搜索或卡片签名；发送成功只表示指令已发出，Music 的后续卡片或错误才是终态。
- `闭嘴`、`别说了`、`助手名退下` 和 `退下助手名` 会把当前作用域写入持久休眠状态；
  重启不会自动解除，直到下一次明确唤醒。助手自己的回复、任务确认和失败回执不会延长
  会话 TTL。
- 普通聊天不使用 QQ 引用回复。任务事件保留原请求消息和请求人上下文，由 Conversation
  Agent 定向表达文本状态，产物由 Runtime 直接交付，群内其他成员不会继承任务完成话术。
- 任务状态查询由 Conversation Agent 根据上下文调用 `get_task_status`，Runtime 只校验
  任务作用域、revision 和工具权限，不通过中文关键词改写用户意图。
- 用户否定既有结果后，Conversation Agent 会把纠正关联到原任务并创建新 revision。
  被否定 Artifact 的内容、来源和生成策略会失效；验收条件使用稳定 ID，连续两次在同一
  revision 违反同一纠正原因时任务停止，不同原因和新 revision 不会互相累计。
  纠正任务完成前还会独立检查当前候选图片或 UTF-8 正文，单次使用任务模型核验全部条件。
  条件 ID 或执行器的完成声明不能代替内容证据；缺证据、核验失败或无法确认时保留候选文件。
  提交前再次检查 revision、条件和文件摘要。模型核验仍可能误判；原任务无纠正条件时沿用原有验收契约。
- 网页读取或下载的单个来源失败不会直接终止整项任务，可以继续换来源；重复 URL 在当前
  任务 Turn 复用不可重试失败。预算耗尽时保留候选文件，有未核验纠正条件时不自动宣称验收通过。
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
| `PUMPKIN_DIRECT_FOLLOW_UP_WINDOW_SECONDS`、`PUMPKIN_TOPIC_MAX_LIFETIME_SECONDS`、`PUMPKIN_FOLLOW_UP_TURN_BUDGET` | 话题空闲滑动窗口、单次话题硬上限和共享 Turn 预算；默认 300 秒、900 秒、6 Turn |
| `PUMPKIN_STOP_ALIASES` | 可选的逗号分隔停止参与别名；默认包含 `闭嘴`、`别说了` 和动态助手名组合 |
| `PUMPKIN_CHAT_TIMEOUT_SECONDS`、`PUMPKIN_TASK_TIMEOUT_SECONDS` | 单次聊天和任务模型请求超时；聊天默认 60 秒且超时不重试 |
| `PUMPKIN_SINGLE_TASK_TIMEOUT_SECONDS` | 单个任务总执行时限 |
| `PUMPKIN_MAX_STEPS`、`PUMPKIN_TASK_MAX_STEPS`、`PUMPKIN_TASK_FINALIZATION_STEPS` | Conversation Agent 与 Task Executor 的最大模型步骤数默认分别为 10 和 16；任务在 50% 和 75% 时提醒模型收敛，最后 2 步只允许提交完成状态 |
| `PUMPKIN_TASK_WEB_SEARCH_LIMIT`、`PUMPKIN_TASK_WEB_FETCH_LIMIT`、`PUMPKIN_TASK_WEB_DOWNLOAD_LIMIT` | 单个 Task Turn 的搜索、抓页和文件下载上限，默认 8、16、8 |
| `PUMPKIN_TASK_BROWSER_DELEGATE_LIMIT` | 单个 Task Turn 的 Browser Agent 委派上限，默认 4 |
| `PUMPKIN_WEB_DOWNLOAD_MAX_BYTES` | 单个公网文件下载上限，默认 20 MiB |
| `PUMPKIN_CHAT_FALLBACK_BASE_URLS`、`PUMPKIN_TASK_FALLBACK_BASE_URLS` | 逗号分隔的备用端点 |
| `PUMPKIN_SEARCH_API_URL`、`PUMPKIN_SEARCH_API_KEY` | 可选网页搜索服务，未配置时明确返回未配置 |
| `PUMPKIN_SEARCH_TIMEOUT_SECONDS`、`PUMPKIN_SEARCH_MAX_RESULTS` | 单次 Web 请求超时和搜索结果上限，默认 20 秒、12 条 |
| `PUMPKIN_FETCH_MAX_BYTES`、`PUMPKIN_FETCH_MAX_CHARS`、`PUMPKIN_IMAGE_FETCH_MAX_BYTES` | 网页正文下载、提取文本和网页图片上限，默认 2 MiB、60000 字符和 20 MiB |
| `PUMPKIN_WEB_MODEL_CONTENT_MAX_CHARS`、`PUMPKIN_WEB_HOST_CONCURRENCY` | 模型可见单次 Web 结果上限和同 host HTTP 并发，默认 8000 字符、4 并发 |
| `PUMPKIN_BROWSER_ENABLED`、`PUMPKIN_BROWSER_WORKER_SOCKET`、`PUMPKIN_BROWSER_WORKER_TOKEN` | 可选隔离 Browser Agent 开关、UDS 路径和 Worker 认证 Token |
| `PUMPKIN_BROWSER_REASONING_EFFORT`、`PUMPKIN_BROWSER_TIMEOUT_SECONDS`、`PUMPKIN_BROWSER_MAX_STEPS`、`PUMPKIN_BROWSER_MAX_MCP_CALLS` | Browser Agent 独立模型推理深度、总时限、模型步骤和 MCP 调用预算；默认 `low`、300 秒、36、40 |
| `PUMPKIN_CODE_RUNNER_URL`、`PUMPKIN_CODE_RUNNER_TOKEN` | 独立代码沙箱服务，默认使用 Compose 内部地址 |
| `PUMPKIN_IMAGE_PROVIDER`、`PUMPKIN_IMAGE_API_BASE_URL`、`PUMPKIN_IMAGE_API_KEY`、`PUMPKIN_IMAGE_MODEL` | 可选图片生成适配器 |
| `PUMPKIN_AUDIO_PROVIDER`、`PUMPKIN_AUDIO_API_BASE_URL` | 可选 TTS 适配器，支持 `minimax` 和 `qwen-local` |
| `PUMPKIN_AUDIO_API_KEY`、`PUMPKIN_AUDIO_MODEL` | MiniMax TTS 凭据与模型 |
| `PUMPKIN_AUDIO_VOICE_ID`、`PUMPKIN_AUDIO_LANGUAGE` | Qwen 本地 TTS 默认音色与语言 |
| `PUMPKIN_AUDIO_TRANSCRIBE_*` | 可独立启用的 MiniMax 入站语音转写配置 |
| `PUMPKIN_MEDIA_TIMEOUT_SECONDS`、`PUMPKIN_MEDIA_MAX_BYTES` | 按需查看消息图片的请求超时和大小上限 |
| `PUMPKIN_DATABASE_URL` | 默认 `sqlite://data/pumpkin.db` |

配置只读取 `PUMPKIN_*` 变量。容器环境中的反引号包裹值会被自动去除。
Conversation Agent 的稳定决策原则和有个人边界的亲近人格由独立 Prompt 常量固定；它可
以拒绝请求或保持安静，不会为了满足请求强制创建任务，也不会向用户展示内部工作细节。
具体操作指导由对应工具描述维护，变化的消息、任务事实和能力目录通过结构化动态上下文
提供。

完整边界、数据模型和调用链见
[`docs/feature/20261023-pumpkin-harness-alignment/design.md`](../../docs/feature/20261023-pumpkin-harness-alignment/design.md)。
任务预算、URL 熔断、网页上下文和 HTTP 观测设计见
[`docs/feature/20261023-pumpkin-task-performance/design.md`](../../docs/feature/20261023-pumpkin-task-performance/design.md)。
终态交付、稳定验收条件和纠正计数设计见
[`docs/feature/20261023-pumpkin-delivery-gating/design.md`](../../docs/feature/20261023-pumpkin-delivery-gating/design.md)。
纠正内容核验和短链路评测见
[`docs/feature/20261106-pumpkin-evidence-evaluation/design.md`](../../docs/feature/20261106-pumpkin-evidence-evaluation/design.md)。

## 行为回归评测

在插件目录执行 `uv run python evaluate.py --assistant-name eval-assistant`，通过 Fake
NapCat 和独立插件进程运行六类短回归。默认使用脚本模型，报告标记为 `contract_replay`，
仅验证消息、评分和失败回执协议，不能证明模型变聪明。默认报告写入 Git 忽略的
`tests/output/behavior-evaluation.json`；`--case` 可重复指定案例，`--output` 可更改位置。
报告不保存提示词、完整对话或媒体正文。

真实模型模式需显式使用 `--live --env-file .env.dev --assistant-name eval-assistant
--judge-base-url <独立评审端点> --judge-model <评审模型>`，评审密钥通过
`PUMPKIN_EVAL_JUDGE_API_KEY` 提供。入口只继承模型设置，Fake NapCat 和数据目录独立，
不连接真实 QQ，也不继承生产搜索、图片、音频、代码或浏览器服务凭据。
因此现成资源案例也检验工具不可用时是否如实反馈，不能作为生产搜索召回率评测。
评审只读取可见输入、出站消息和实际媒体；没有证据或评审不可用时记为 `unknown`，
不会计为通过。任务型案例观察至 `--timeout` 窗口结束，超时未交付不能推定完成。
失败回执案例使用确定性的模型故障注入，报告单独标记；它不衡量模型推理能力。
这套短回归独立于开放式 50 回合群聊评测，不替代其验收。

## 网页与媒体

`search_web` 使用 `httpx` 访问配置的搜索 API，搜索请求只发送 `query` 和
`max_results`，不固定地区或语言路由。
任务执行上下文独立保留归档用户原话，执行摘要不能擅自扩大目标或增加交付要求。
网页读取会单独返回最多 30 个图片候选及 alt 文本；图片下载在登记前验证解码和尺寸，
并返回最多 1280 × 1280 的首帧视觉预览供 Agent 核对内容。候选链接和可解码状态都不等于
符合任务，仍需显式交付验收；查找现成资源失败时不能自行改为生成。

`fetch_url` 使用固定 Chrome Linux
Profile 的 `wreq` Transport 读取任意公网来源，并通过 `output=text|artifact`
区分网页正文与通用文件 Artifact。不同 URL 的文件支持并发下载，单 host 并发仍受配置
限制；同一 URL 会在 Turn 内去重。抓取仍会拒绝内网地址，逐跳校验并固定 DNS，限制重定向
和响应大小；传输失败不会自动回退到 `httpx`。该能力不执行 JavaScript，也不能通过验证码
或站点挑战。

动态网页、公开页面交互、截图和 PDF 可按需加载 `browser-delegation` Skill。Task Agent
只调用高层 `delegate_browser_task`；每次委派由 `browser-worker` 从
`plugins/pumpkin/browser/` 镜像启动一个全新的 Obscura stdio MCP 进程，结束后强制回收。
Worker 通过共享 Unix Domain Socket 与 Mimi 通信，只加入独立公网出站网络，不读取 Mimi
密钥、数据库或插件数据。首版全局并发为 1，容量满时明确拒绝。启用前设置独立随机 Token，
并使用 `docker compose --profile browser build --pull browser-worker` 构建镜像；未启用时
该 Skill 不进入能力目录，不影响 `search_web` 和 `fetch_url`。

公开表单在单次 Browser Agent 运行内通过安全元素引用或检测得到的精确字段名批量填写。
字段名模式由 Worker 内部转换为受限 selector，模型不能传 selector 或任意 JavaScript。
每次委派仍使用全新页面状态，父 Task 不能通过重新委派续填。交互任务完成前
必须在最后一次页面变更后重新读取页面；`completed` 的证据文本必须能在该最终观察中找到，
截图或 PDF 也必须在同一最终状态生成。失败运行中的过程截图不会登记为 Artifact。Worker
镜像包含 CJK 与 Emoji 字体；MCP 超时和子进程断开使用不同错误码，便于区分页面超时与
浏览器进程故障。

网页搜索、代码执行、图片生成、HTML 图文渲染、Markdown 报告渲染和音频合成均为按需
Skill。Markdown 渲染支持标题、引用、列表、表格、代码块和来源链接；配图只引用当前
revision 的图片 Artifact，公网图片需先经过下载校验。代码执行容器可正常访问网络并读写
自身文件系统，但不会挂载宿主目录或读取 Mimi 的环境变量；普通相对路径文件会作为执行
产物收集。生成图片、音频和代码文件会先登记为候选 Artifact，只有经过当前 revision 的
完成验收并满足全部用户纠正条件后，才能通过 NapCat 发送。本地 Artifact 在发送前转换为
`base64://` 数据，避免依赖 NapCat
容器可见的宿主路径。图片和音频适配器的 Provider、Base URL、API Key、Model 未完整配置时，
对应 Skill 不会进入 SkillCatalog。长文本图片使用完整 CJK 字体，避免因字体子集缺字导致
文字错位或空白。`media-inspection` 也是按需 Skill，模型只有在需要理解
图片时才会现场读取当前作用域的媒体引用；不做后台预下载，引用过期时返回明确错误。引用
消息和合并转发会有限深度解析，超过深度或节点预算会保留截断标记。GIF 图片和动态扩展
表情在交给模型前只提取首帧并转换为 PNG，避免把完整动画编码进模型请求。

Pumpkin 会在后台从所有群聊的顶层 `image` 和 `mface` 消息段收集表情候选。候选经过
安全下载、精确及感知去重、视觉分类和隐私过滤后，进入最多 50 个的跨群共享活跃库；
活跃库满后按北京时间每天最多替换 10 个，被替换文件进入
`data/stickers/archive/YYYY-MM-DD/`。归档不参与发送，也不会自动删除。收集不会唤醒
Conversation Agent。Conversation Agent 可按需加载 `sticker-reply` Skill，根据受控
分类检索 `sticker_id` 并发送一个合适的表情；Task Event、Task Executor 和未被参与策略
接受的普通群消息无权发送。表情与文字共用当前 Turn 的回复条数预算，并统一经过 Outbox。
群内任何成员都可以引用一条只含单个图片或扩展表情的原消息，要求 Pumpkin 从共享库移除
对应表情。移除目标由 Runtime 从引用消息解析，Agent 不能指定任意资产；删除后保留哈希
墓碑，防止相同或近似内容被后台重新收集。

音频合成可选择 MiniMax 或宿主机上的 Qwen3-TTS 本地服务。MiniMax 支持已有音色、语速、
音量、音调、情绪、声音质感、效果器及 2 至 4 个音色混合；Qwen 固定输出 WAV，支持
`Chinese`、`English`、`Auto` 语言和已注册音色。Agent 始终使用统一的音频合成工具；
不支持的设置由 Provider 适配器明确拒绝，不会静默忽略。
完整任务只交付一个音频文件时，Runtime 只发送一条 QQ 语音，不再追加引用、@ 或重复的
“已完成”文字；部分完成、失败和发送结果不确定时仍提供必要说明。

Qwen 可从当前任务源消息中的语音或音频文件创建 ICL 或 x-vector 克隆音色。只有用户明确
确认已获得说话人授权时才允许克隆；参考音频只在内存中发送给本地服务，不进入 Session
Event、日志、Artifact 或 Pumpkin 配置文件。克隆成功后自动成为后续合成使用的最新音色，
该指针通过 `data/voice_profile.json` 原子持久化。Pumpkin 不提供音色枚举或删除操作。
Qwen 服务使用 Apple Silicon MPS，应在宿主机独立运行；Mimi 容器通过配置的宿主机地址
访问它。

## 命令

| 命令 | 说明 |
| --- | --- |
| `闭嘴` / `别说了` / `助手名退下` | 让当前会话持久休眠，直到再次明确唤醒 |

## 测试

```bash
uv run pytest
```

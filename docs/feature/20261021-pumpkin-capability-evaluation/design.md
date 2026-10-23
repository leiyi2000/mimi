---
status: in_progress
owner: Mimi maintainers
updated: 2026-10-21
commits: []
---

# Pumpkin 群聊能力评测设计

## 1. 目标与结论

本评测不是让多个模型互相扮演用户，也不是用固定脚本逐条检查 Pumpkin 是否输出了
某个答案。核心关系只有一条：**模型扮演群聊用户 Agent，Pumpkin 是被测试的群聊助手**。
模型负责像真实群成员一样观察上下文、形成模糊目标、决定是否发消息，并通过真实的
NapCat 消息协议和 Pumpkin 对话；Pumpkin 的回复、任务执行和媒体交付才是测试结果。

一轮测试的最小闭环是：

```text
初始化一轮模糊目标
    -> 用户 Agent 模型观察群内全部可见消息
    -> 用户 Agent 模型选择发送或保持安静
    -> Fake NapCat 投递用户消息
    -> 黑盒 Pumpkin 接收并处理
    -> 记录 Pumpkin 的可见回复、媒体、任务相关对话和交付
    -> 用户 Agent 模型继续观察并决定下一步
    -> 对话轮数耗尽
    -> 收集并整理完整可见对话
    -> 生成 transcript.md 和 report.md
```

本设计的验收重点：

1. 扮演用户 Agent 的模型可以接收其他群成员和 Pumpkin 的全部可见对话。
2. 扮演用户 Agent 的模型每一步都可以发送消息，也可以不发送消息。
3. 用户 Agent 发送的消息使用 NapCat 支持的结构化消息段，覆盖文本、图片、表情包、
   文件、引用、转发、@、语音和包含复杂消息的组合。
4. 用户 Agent 每轮开始时只拿到模糊目标，例如和某位成员聊天、给助手交代任务、随机
   补充任务关键信息或质疑已有结果；它不能直接看到评测期望或 Pumpkin 的内部状态。
5. Pumpkin 是唯一主要被测对象，测试聊天人格、简洁程度、相关性、任务执行、媒体交付
   和自然收敛能力。
6. 每轮有明确的对话次数上限，必须运行到对话轮数耗尽。结束后保留完整消息顺序，渲染
   `transcript.md`，并生成指出 Pumpkin 本轮问题及证据的 `report.md`。

每轮测试必须真实请求配置的模型：至少由一个模型扮演群聊用户 Agent，并由真实运行中的
Pumpkin 使用其配置的模型处理群聊。Fake NapCat 只替代 QQ 网络和账号，不替代模型。
报告先保证可读和完整；指标、语义评审和自动优化建立在这条真实对话采集链路之上，不能
反过来改变群聊用户 Agent 的行为。

### 1.1 非目标

- 不用多个 AI 角色替代真实群聊用户 Agent 的决策。
- 不让群聊用户 Agent 读取 Pumpkin 的 Prompt、Tool Call、隐藏状态、任务标签或评测
  期望。
- 不以固定文案、固定回复数量或“回复越多越好”作为通过条件。
- 不把测试报告写入 Git，不保存真实 QQ 身份、Token、Cookie、模型密钥或真实群聊内容。
- 不通过 Pumpkin 的内部 `AgentLoop`、`PumpkinRuntime`、作用域、工具或数据库接口
  判断测试结果。

## 2. 测试对象和运行边界

### 2.1 Pumpkin 是黑盒被测对象

测试运行时，评测器只把 Fake NapCat 当作群聊入口和出口。Pumpkin 的内部实现对评测器
不可见，评测器不得根据源码、内部日志、数据库或模型 Tool Call 推断结果。

黑盒边界只有：

```text
Fake NapCat inbound
    -> Pumpkin black box
    -> Fake NapCat outbound
```

评测器允许做的事情：

- 通过 Fake NapCat 投递合成群消息、媒体、引用、@ 和转发；
- 观察 Fake NapCat 收到的 Pumpkin 出站消息、媒体和发送结果；
- 记录外部可见的时间、消息顺序、超时、进程错误和最终会话结果；
- 对 Pumpkin 实际发送的合成媒体做文件格式、大小、可读性和内容一致性检查。

评测器禁止做的事情：

- 直接调用 Pumpkin 的内部 Agent、Task Manager、Chat Agent 或工具；
- 读取 Pumpkin 的 Prompt、模型原始响应、Tool Call、归档、数据库、任务状态或内部日志；
- 用内部状态判断“任务已经完成”或“助手应该回复”；
- 在 Pumpkin 没有通过 Fake NapCat 发出消息时，为它补写或推断一条用户可见回复。

Pumpkin 的测试结论必须来自外部可观察行为。Pumpkin 的所有用户可见输出都从 Fake
NapCat 出站事件采集，不能直接读取模型文本代替实际发送结果。

### 2.2 群聊用户 Agent 模型

每轮只启动一个**扮演群聊用户的模型 Agent**。它代表一个当前正在群里的合成成员，
拥有一个合成的 `participant_id`，并可以看到：

- 其他合成成员发送的消息；
- Pumpkin 发送的所有可见消息；
- 引用、@、转发节点和媒体消息的可见内容；
- 当前轮已经经过的逻辑时间、对话次数和自己的历史发言；
- Pumpkin 是否发送了图片、文件、语音或其他交付物，以及发送是否成功。

它看不到：

- 其他成员的隐藏目标和期望标签；
- Pumpkin 的 Prompt、模型响应、内部思路、Tool Call、任务状态内部字段和权限信息；
- 评测器对某条消息标记的 `reply`、`ignore`、`task` 或 `clarify` 结论；
- 没有通过 Fake NapCat 成功发送的内部内容。

这个模型的输出不是评分，也不是对 Pumpkin 的诊断，而是下一步群聊行为。每个决策回合
必须返回：

```text
send(message)
stay_silent(reason)
```

其中 `reason` 只写入内部运行记录，不进入给 Pumpkin 看的群聊上下文。

### 2.3 背景群成员

为了形成多人异步群聊，运行器在每轮开始时使用一个真实模型请求初始化背景成员的行为
准则、话题和发言计划。背景成员不是独立的 AI 评审者，也不负责决定 Pumpkin 对不对；
每个背景成员根据自己的初始化准则自主随机产生至少一条群消息，例如：

- 两名成员讨论与 Pumpkin 无关的日常话题；
- 某名成员回复或引用群聊用户 Agent；
- 任务执行期间有人插话；
- 任务完成后有人追问或确认收到文件。

初始化模型只生成环境配置和背景事件，不作为独立用户 Agent 参与对话；它不能读取
Pumpkin 内部状态，也不能生成 Pumpkin 回复或评测结论。初始化失败、超时或结构化结果
非法时本轮标记为 `aborted`，不得退回写死的背景文案。

### 2.4 角色边界

| 角色 | 实现 | 职责 |
| --- | --- | --- |
| 群聊用户 Agent | 一个配置模型 | 观察群聊、形成目标、发送或不发送消息 |
| Pumpkin 助手 | 黑盒进程及其配置模型 | 处理群聊、聊天、执行任务和交付产物 |
| 环境初始化器 | 一次配置模型请求 | 初始化背景成员行为准则和合成环境事件 |
| 评测器 | 确定性运行器 | 投递事件、限制轮次、记录真实收发并渲染报告 |
| 背景成员 | 初始化器生成的合成身份 | 按自己的行为准则提供多人群聊噪声 |

群聊用户 Agent 模型不能直接调用 Pumpkin 的内部接口，也不能读取评测器的隐藏结论。
评测器不能替用户 Agent 模型决定台词，不能替 Pumpkin 补写回复，不能把用户 Agent
模型的输出直接当作测试结论。Pumpkin 是否正确只能由群聊外部可见的消息和交付结果
判断。

## 3. 一轮测试模型

### 3.1 Round

一轮测试使用一个唯一 `round_id` 和一个固定 `seed`。轮次在开始时一次性初始化，不在
中途根据 Pumpkin 的输出改写初始目标。

```text
Round
  round_id
  seed
  model_config_digest
  group_scope
  participants
  initial_goal
  background_schedule
  turn_budget
  event_budget
  stop_policy
```

`model_config_digest` 只保存 Provider、模型名和配置摘要，不保存密钥或完整请求内容。
扮演群聊用户的模型和 Pumpkin 使用的 Provider 必须通过配置装配，并在每个真实测试
回合中发起模型请求。两者可以使用同一个 Provider，但必须是两个独立的角色上下文：
用户 Agent 模型只生成群成员行为，Pumpkin 模型只生成助手行为。模型请求失败、超时、
限流和返回结构错误都属于本轮测试结果，不能悄悄切换为离线 Stub。

评测默认 `turn_budget` 为 **50**。除专门的短链路回归测试外，真实能力评测必须完整
执行 50 个用户 Agent 决策回合，不能用 1 轮或 8 轮的调试配置替代正式结果。

### 3.2 群友行为和模糊对话目标

本轮的群聊用户不是“为了测试而不停提问”的评测脚本，而是一个有自己生活话题、社交
关系和临时需求的普通群友。群聊中的可见行为由两层组成：

- `Group User Agent`：一个主要群友，负责在 50 个 turn 中自然地在群友互聊、和
  Pumpkin 聊天、派发任务和跟进任务之间切换。
- `Background Members`：至少两个独立群友，各自维护自己的话题轨道；他们可以继续
  互相回复、发起新话题、在 Pumpkin 处理任务期间插话，不能被强行收敛到主用户目标。

主用户 Agent 和背景成员都必须遵守以下行为原则：

1. 先像群友一样聊天，只有需要助手时才自然地 @ Pumpkin 或使用助手名称，不把每一轮
   都变成测试提问。
2. 群里出现多个独立话题时，可以回复其他成员、引用消息、切换话题或保持安静；不能
   代替其他成员发言，也不能把全群持续拉回 Pumpkin。
3. Pumpkin 正在处理任务时，群聊继续运行。用户 Agent 可以等待、继续和群友聊天、补充
   任务信息、质疑结果或发起第二个独立请求；运行器不能因为等待 Pumpkin 而阻塞它。
4. 任务必须经历自然变化：可能只有模糊目标，随后补充约束、提供媒体或文件、询问进度、
   接受结果、要求修改，也可能在信息不足时暂时放下。
5. 用户知道 Pumpkin 能执行代码、搜索网页、生成图片、合成语音和整理图文，但只把这些
   当作“可以帮忙做什么”的公开能力，不得看到或提及 Skill 名称、工具名、任务状态、
   内部 Agent 或评测标签。

运行器不把下面的比例变成逐轮脚本，而是作为模型行为的软分布目标：约三成普通群友互聊，
约两成直接或间接和 Pumpkin 互动，约两成任务派发与补充，约一成任务结果确认或修改，
其余为观察、切换话题和自然静默。单轮可以发送多条消息或完全不发送；最终以可见对话
是否形成真实异步群聊为验收依据，而不是以消息数量达标。

每轮只初始化一个高层目标和少量行为倾向，不给出逐条台词。目标可以是：

- 和群里的某位成员聊一件日常小事，偶尔把话题带给 Pumpkin；
- 直接向 Pumpkin 交代一个可以执行的任务；
- 先给 Pumpkin 一个不完整目标，再在后续随机补充关键约束；
- 在 Pumpkin 交付后追问依据、要求修订或确认文件内容；
- 先和其他成员闲聊，之后才明确唤醒 Pumpkin；
- 唤醒 Pumpkin 完成一小段交流，随后回到与助手无关的群聊；
- 两个目标之间随机选择一个继续，观察 Pumpkin 是否会混淆任务。

目标用结构化约束描述，不保存固定对话文案：

```text
ConversationGoal
  goal_id
  intent: chat | assign_task | supplement | challenge | accept | disengage
  target_participant_id
  task_shape
  required_progress
  optional_progress
  allowed_media_types
  silence_windows
  completion_observation
```

`required_progress` 只说明这一轮至少需要观察到什么，例如“交代目标后补充一个关键
文件，再确认助手是否完成”；不规定必须使用哪句话。目标内容是群聊用户 Agent 的
隐藏输入，不进入 Pumpkin 的上下文。目标必须同时包含至少一个 `silence_window` 或
`member_chat_window`，确保 Pumpkin 有机会在无关群聊中保持安静。

### 3.3 用户 Agent 模型的决策回合

每次决策前，运行器向用户 Agent 模型提供一份按逻辑时间排序的可见群聊快照。模型
根据初始目标、最近上下文和角色状态做一次决策：

1. 选择发送一条消息、一次消息突发，或保持安静。
2. 如果发送，生成一个或多个 NapCat 消息段。
3. 指定引用的已见消息、@ 的合成参与者和转发的已见节点。
4. 选择等待 Pumpkin、继续观察或回到普通群聊；不能结束本轮。

`wait_policy` 只描述群友当前的心理状态，不是运行器的等待指令：

- `observe`：看群里有没有新消息，可能下一轮继续闲聊，也可能保持安静；
- `wait_for_reply`：期待 Pumpkin 回复，但期间仍允许背景成员和用户 Agent 发送消息；
- `continue_chat`：不等待 Pumpkin，优先和其他群友聊天或切换自己的话题。

模型每次决策都要重新查看完整的可见快照，不能因为上一轮选择了
`wait_for_reply` 就自动重复追问。只有出现新的 Pumpkin 消息、任务相关用户输入或明显
话题变化时，才继续该轨道；否则优先保持安静或参与其他成员的对话。

一次 `send` 可以包含多条连续的消息，但每条消息必须分别经过 Fake NapCat，并在报告中
保留原始顺序。用户 Agent 模型不能通过一次调用直接写入 Pumpkin 的归档或任务状态。

### 3.4 用户 Agent 的多模态 Skill

用户 Agent 模型不能自己生成二进制、猜测媒体路径或伪造 NapCat 消息段。为让它能够
自然地产生多模态群聊输入，评测器给它提供一组受限 Skill：

| Skill | 用途 | 输出 |
| --- | --- | --- |
| `image_asset` | 根据模型描述请求真实图片 Provider 生成合成图片 | `MediaAsset` |
| `sticker_asset` | 从合成表情包目录选择 `face` / `mface` 及预览文件 | `MediaAsset` |
| `voice_asset` | 根据模型指定的短句请求真实语音 Provider 合成语音 | `MediaAsset` |
| `file_asset` | 根据结构化内容创建合成文件，或选择预置测试文件 | `MediaAsset` |
| `message_compose` | 组合文本、资产、@、引用和转发节点并校验 NapCat schema | `NapCatMessageDraft` |

Skill 只返回资产引用，不直接向 Pumpkin 或 QQ 发送消息：

```text
MediaAsset
  asset_id
  kind: image | sticker | voice | file
  local_path
  mime_type
  size
  sha256
  source: real_provider | catalog | generated_fixture
```

图片和语音 Skill 使用配置的真实 Provider；请求失败、超时或返回非法媒体时，必须返回
明确失败，不能用一段文本或伪造文件冒充成功。表情包通常不需要再次调用生成模型，
而是从合成目录选择合法的 QQ 表情 ID 和对应预览；文件由受控的结构化生成器创建，
不能让用户 Agent 任意执行宿主机脚本。

用户 Agent 模型完成 Skill 调用后，只提交 `send` 或 `stay_silent` 决策。评测器检查
所有 `asset_id`、引用关系和消息段，再通过 Fake NapCat 发送。这样可以同时测试模型
是否会选择合适的媒体、NapCat 消息协议是否正确，以及 Pumpkin 收到合法多模态输入后
是否正确处理。

## 4. 消息和媒体协议

### 4.1 统一消息模型

用户 Agent 模型输出结构化的 `NapCatMessageDraft`，由评测器校验后转换为真实 NapCat
入站事件：

```text
NapCatMessageDraft
  message_id
  sender_id
  group_id
  segments
  reply_to
  at_targets
  logical_time
```

`message_id`、时间戳和媒体文件路径由评测器最终生成或校验，模型不能伪造已经发送成功
的 message ID，也不能引用未见消息。

所有 `NapCatMessageDraft` 必须经过 Fake NapCat 的入站事件和 Pumpkin 的
`NapCatMessageAdapter`。评测器禁止直接构造 `InboundMessage`、`MessageRecord`、
`WindowItem` 或 `ModelMessage` 注入 Pumpkin。这样测试到的是“NapCat 消息如何被 Pumpkin
适配并进入上下文”，而不是绕过适配器测试一个人工拼装的模型请求。

### 4.2 适配器后的模型输入

Pumpkin 的模型输入分为“上下文标记”和“按需媒体内容”两层：

```text
NapCat event
  -> NapCatMessageAdapter
  -> InboundMessage / MessageSegment
  -> archive / chat window
  -> text and media annotations
  -> optional media-inspection Skill
  -> ModelMessage content
```

适配器和窗口先把消息转换为模型可读的上下文：

- 文本、@、引用、转发、JSON 和表情进入文本上下文及结构化标记；
- 图片、文件和语音先以媒体类型、引用关系和必要元数据进入上下文，不把任意 URL 或
  本地路径直接塞进 Prompt；
- 模型确实需要理解媒体时，使用消息 ID 和 `segment_index` 调用
  `inspect_message_media`；
- 图片检查结果作为视觉输入传给模型；
- 文本文件返回受限文本预览，二进制文件返回可读元数据；
- 语音先由 MiniMax ASR 转成文字，再把转写结果交给模型。

因此，测试报告只记录 Fake NapCat 的原始入站消息和 Pumpkin 的可见出站消息，不能声称
已经观察到 Pumpkin 的内部 `ModelMessage`。媒体是否被理解，应从后续可见回复、任务
行为和交付结果判断。

### 4.3 必须覆盖的消息段

一轮或一组轮次的消息生成必须覆盖以下类型。覆盖要求针对消息协议，不要求每轮都把
所有类型塞进同一条消息：

| 类型 | 约束 |
| --- | --- |
| `text` | 普通文字，支持多段文字和空白边界校验 |
| `image` | `image_asset` 生成的合成图片，校验格式、尺寸和发送结果 |
| `file` | `file_asset` 创建的合成文件，保留文件名、大小和哈希摘要 |
| `face` / `mface` | `sticker_asset` 选择的 QQ 表情或扩展表情及合成 ID |
| `at` | 只能 @ 合成成员或配置的助手 |
| `reply` | 只能引用已经进入群聊的消息 |
| `forward` | 只能转发已经见过的消息节点 |
| `record` | `voice_asset` 生成并校验的语音文件，转换为 NapCat `record` 消息 |
| `json` / 组合消息 | 用于覆盖复杂消息和多段消息的协议边界 |

图片、文件和语音的二进制内容不由模型直接输出。模型通过 Skill 声明用途、媒体类型
和可选的合成内容描述；Skill 在隔离运行目录生成或选择资产，再由评测器把实际文件
交给 NapCat 适配器。

### 4.4 复杂引用和转发

评测器必须支持这些组合：

- 文本 + @ + 引用；
- 引用一条包含图片或文件的复杂消息；
- 合并转发中包含文本、图片、表情和引用；
- 转发节点中继续包含多段消息；
- Pumpkin 回复文件后，群聊用户 Agent 引用该文件消息并提出修改；
- Pumpkin 回复图片后，群聊用户 Agent 通过引用或转发要求解释图片。

为防止递归和无界膨胀，运行器限制合并转发的展开深度、节点数量、单个媒体大小和
整轮消息数量。超出限制时必须返回结构化校验错误，并把错误写入内部记录，不能静默
丢弃或把无效消息投递给 Pumpkin。

## 5. Pumpkin 的核心评测能力

### 5.1 聊天人格和简洁度

用户 Agent 模型通过自然的短对话观察 Pumpkin 的可见表现，但不负责评分。评测器之后
依据这些真实对话判断 Pumpkin 是否：

- 保持已配置的人格，不突然变成客服、说明书或评测器；
- 优先回应当前最重要的内容，而不是抓住无关关键词；
- 在一句话足够时使用短句，不输出小作文；
- 多条消息只在即时聊天确实需要时使用，不机械拆句；
- 能承接情绪、玩笑、迟疑和话题切换；
- 不把不知道的事情说成事实，不伪造经历、工具结果或未来承诺；
- 对敏感或高风险内容保持事实边界，不用撒娇或玩笑回避。

这里的目标是“像真人聊天”，不是让 Agent 追求固定字数。报告中保留完整消息，后续
评审依据上下文判断简洁和自然。

### 5.2 相关性和不瞎聊

用户 Agent 模型会制造与 Pumpkin 无关的消息、相似关键词和短暂的闲聊窗口，观察
Pumpkin 是否：

- 未被唤醒时保持安静；
- 被直接 @、使用前缀或明确请求时及时承接；
- 不因为群里出现相似词就抢答；
- 不重复回应已经处理过的消息；
- 在用户不等待时不强行插话；
- 能在任务仍有义务时保持必要通知；
- 区分群成员之间的对话和对助手的直接请求。

“不发送”是合法且重要的行为，不能被报告或评分系统默认记为漏回复。

### 5.3 任务执行和交付

用户 Agent 模型通过模糊交代、延迟补充、多人插话和结果质疑观察 Pumpkin 是否：

- 能识别确实需要执行的任务；
- 缺少必要信息时追问，而不是自行猜测；
- 接收后续补充，不覆盖已经确认的约束；
- 让两个交错任务在用户可见的对话和交付结果中保持上下文、作用域和 revision 隔离；
- 在执行期间按需要发送进度或等待提示；
- 工具失败、超时或局部成功时诚实地说明状态；
- 只有在任务和用户可见交付物真正完成后才声称完成；
- 能交付图片、文件、语音等用户可见产物；
- 用户引用结果要求修订时，能基于当前 revision 继续处理。

任务完成只能依据黑盒外部证据判断：

1. Pumpkin 对用户明确说明了当前结果或失败状态；
2. 必需的图片、文件或语音已经通过 Fake NapCat 实际发送；
3. 交付物满足场景中可从外部检查的格式、内容和关联要求；
4. 后续群聊用户 Agent 可以基于已交付内容继续确认、质疑或验收。

不能因为某个内部工具调用、内部状态或模型文本声称完成，就把任务判定为完成。

### 5.4 从参与到沉默

一轮目标可以要求用户 Agent 模型在 Pumpkin 被唤醒并完成一小段对话后，停止提及
助手，继续与其他成员聊天。此时 Pumpkin 的正确行为是逐步收敛到沉默：

```text
engaged -> cooling -> quiet
```

- `engaged`：正在回应明确问题、承接连续追问或执行未完成任务。
- `cooling`：没有新的明确唤醒，只处理当前话题的必要补充或未完成问题。
- `quiet`：没有新的直接请求、@、前缀、关键任务输入或待发送义务时完全不发消息。

进入 `quiet` 不发送“我先不打扰了”“你们聊”等告别式声明。群聊用户 Agent 仍可以
在 quiet 期间发送普通消息，验证 Pumpkin 不会因为历史上曾被唤醒而持续插话。之后新的
明确唤醒必须能够重新激活 Pumpkin。

## 6. 评测器和真实链路

### 6.1 组件

```text
Round Runner
  -> Goal Initializer
    -> Group User Agent Model
  -> Background Event Scheduler
  -> Fake NapCat
  -> Pumpkin Black Box
  -> Conversation Recorder
  -> Markdown Renderer
```

职责边界：

- `Goal Initializer`：按 seed 创建本轮模糊目标、参与者和停止条件。
- `Environment Initializer`：使用一次配置模型请求为每个背景成员生成独立行为准则和至少
  一条消息，再由运行器校验并固定本轮环境。
- `Group User Agent Model`：只观察可见群聊，决定 send 或 stay silent。
- `Background Event Scheduler`：投递由 seed 控制、可追踪的背景消息和故障事件。
- `Fake NapCat`：模拟入站和出站协议，记录实际消息段与发送结果。
- `Pumpkin Black Box`：以正常启动方式运行 Pumpkin，只暴露 Fake NapCat 的入站和出站
  边界；评测器不直接装配或调用其内部组件。
- `Conversation Recorder`：按逻辑时间记录每个可见事件、关联 ID 和发送状态。
- `Markdown Renderer`：把当前轮的可见对话渲染为单份 Markdown。

评测器不能在 `Pumpkin Black Box` 之外替换消息、在模型之后补写回复，或通过内部状态
直接告诉用户 Agent 模型 Pumpkin 是否“答对”。

### 6.2 逻辑时钟和异步事件

测试使用可控逻辑时钟，和墙上时间分离。必须支持：

- 同一逻辑时间投递多名成员的消息；
- Pumpkin 处理期间继续投递群聊用户 Agent 或背景消息；
- 群聊用户 Agent 发送后不等待 Pumpkin，继续观察和聊天；
- 延迟、重复、乱序入站事件；
- Pumpkin 处理期间的后续消息；
- 工具超时、无结果、发送失败和恢复重试。

所有随机选择由 `seed` 控制。每次真实模型运行必须保存展开后的事件顺序、模型配置
摘要、用户 Agent 模型请求关联 ID 和脱敏后的用户 Agent 决策摘要，使问题可以定位到
具体 turn。不得保存或读取 Pumpkin 的模型原始响应。重放只是调试辅助，不是默认测试
入口；重放不能替代真实模型验收，也不能把已保存的模型回复伪装成新的实时请求。不需要
保存密钥或完整 Prompt。

正式 CLI 必须实时输出阶段进度和每条可见消息，并在每个阶段刷新 stdout：轮次开始、
User Agent 模型请求开始和结束、用户/背景成员消息、Pumpkin 出站消息、消息投递、
Pumpkin 黑盒处理、Task Manager/Chat Agent/Task Agent 的模型请求和工具阶段、轮次完成，
以及报告评审模型请求开始和结束。进度日志至少包含当前
`turn / turn_budget`、阶段、发送者、消息方向、累计可见消息数和错误类型；运行中的完整
可见消息流同步写入 `transcript.live.md`，正常结束后再写入正式 `transcript.md`。
这些调试输出不能输出完整 Prompt、模型原始响应或敏感参数。

### 6.3 一轮结束

正常测试结束条件只有一个：**预设的 `turn_budget` 已耗尽**。

一个用户 Agent 决策及其触发的 Pumpkin 回复突发计为一个 interaction turn；Pumpkin
一次发送多条消息仍只计一个回复突发。背景成员的环境消息单独记录，不增加
`turn_budget`，也不能让测试提前结束。

用户 Agent 模型不能提前终止；即使初始目标已经完成、Pumpkin 已经进入 `quiet` 或
对话暂时没有新内容，运行器仍要执行到
`turn_budget`，用于验证 Pumpkin 是否会在后续无关群聊中错误插话。

模型请求失败、Pumpkin 进程异常、NapCat 协议错误或不可恢复基础设施错误只能标记本轮
为 `aborted`，并尽可能收集已经产生的完整可见对话，随后仍生成报告。它们不是普通的
提前成功结束条件。

## 7. 群聊用户 Agent 的输出契约

### 7.1 用户 Agent 模型决策结构

```text
UserAgentDecision
  action: send | stay_silent
  messages: NapCatMessageDraft[]
  wait_policy: observe | wait_for_reply | continue_chat
  next_focus: same_goal | new_topic | disengage
```

约束：

- `stay_silent` 时 `messages` 必须为空；
- `send` 至少包含一条消息，消息段必须通过 NapCat schema 校验；
- 引用和转发只能指向 Agent 已经看到的可见消息；
- @ 目标只能是当前群成员或配置的助手；
- 不能声明发送结果，发送结果只能由 Fake NapCat 返回；
- 不允许携带隐藏 expectation、评分、内部状态或 Pumpkin 工具参数。

### 7.2 Skill、生成和校验失败

用户 Agent 模型或媒体 Skill 生成无效结果时，评测器按以下顺序处理：

1. 记录结构化错误和当前 turn；
2. 不把无效消息或不完整媒体投递给 Pumpkin；
3. 不用文本、空文件或伪造媒体替代失败资产；用户 Agent 在本 turn 保持安静，但不能
   减少剩余轮次；
4. 继续执行到 `turn_budget`，并保留错误发生前后的可见过程。

Skill 失败必须在 `report.md` 中作为测试输入或基础设施问题单独记录，不能归咎为
Pumpkin 的问题。只有合法媒体已经通过 Fake NapCat 投递给 Pumpkin 后，Pumpkin 对该
媒体的处理错误才能作为 Pumpkin Finding。

## 8. 对话记录和 Markdown 报告

### 8.1 正式产物

每轮结束后生成两份 Markdown：

```text
plugins/pumpkin/tests/output/<round_id>/transcript.md
plugins/pumpkin/tests/output/<round_id>/report.md
```

`transcript.md` 只展示本轮完整对话过程；`report.md` 只总结 Pumpkin 在本轮中发现的
问题、证据和影响。`tests/output/` 必须保持 Git 忽略。JSONL、模型追踪和指标可以暂存
在运行内存或临时运行目录中，不作为本轮正式交付物。

### 8.2 Markdown 内容

`transcript.md` 只展示本轮对话过程和必要的轮次元信息：

```markdown
# Pumpkin 对话评测

- round: synthetic-round-...
- seed: ...
- turns: 50 / 50
- end: turn_budget

## 对话

### Turn 01

**10:00:00 群成员 A**

> ...

**10:00:02 Pumpkin**

> ...

**10:00:03 群成员 A**

> [引用 Turn 01 Pumpkin] ...

**10:00:04 Pumpkin**

> [图片: generated-image-01.png]
> [文件: task-result.csv]
```

渲染规则：

- 按逻辑时间和实际到达顺序展示所有可见群消息；
- 展示发送者、消息时间、turn、引用关系、@ 目标和消息段顺序；
- 文本原样保留，媒体显示合成文件名、类型和发送结果；
- 转发消息以树形或缩进方式展示节点，不能压平为一段不可读文本；
- Pumpkin 没有发送消息的 turn 显示为“Pumpkin 未发送消息”，不能补写解释；
- 群聊用户 Agent 的 `stay_silent` 原因、隐藏目标、期望标签、Prompt、Tool Call、
  token 和内部错误不进入这份报告；
- 发送失败可以用一行可见状态标记，但不能伪装成已发送。

`transcript.md` 的目的就是让人从头读完这一轮，判断聊天是否自然、是否抢话、任务是否
完成、媒体是否真实交付，以及助手是否在无关聊天中安静下来。

### 8.3 问题报告

`report.md` 在 `turn_budget` 耗尽、或本轮被标记为 `aborted` 后生成。它必须基于
`transcript.md` 和 Fake NapCat 的外部可见记录，指出 Pumpkin 在本轮存在的问题，不能
引用黑盒内部状态作为证据：

报告生成必须由一个独立的真实模型评审请求完成。该模型只能接收脱敏后的完整可见
transcript、轮次公开目标、公开评审标准和 Fake NapCat 发送状态；不能接收 Pumpkin 的
Prompt、Tool Call、数据库、内部日志或隐藏任务状态。评审模型必须返回结构化 Findings，
至少检查人设一致性、即时聊天简洁度、无关抢答、重复回复、任务可见状态和媒体交付。
评审模型请求失败、超时或结构化结果非法时，`report.md` 仍必须生成，并把失败写入
“测试输入与基础设施”，不能伪造“本轮未发现问题”。

```markdown
# Pumpkin 测试报告

- round: synthetic-round-...
- status: completed | aborted
- turns: 50 / 50

## 结论

本轮发现 2 个问题，严重问题 1 个。

## Findings

### F-001：无关抢答

- category: routing | persona | timing | task | delivery
- severity: blocker | major | minor | info
- evidence: Turn 06、Turn 07
- expected: 成员之间的普通聊天期间保持安静
- actual: Pumpkin 在没有被 @ 或直接请求时发送了消息
- impact: 打断群聊，未能收敛到 quiet
```

每个 Pumpkin Finding 至少包含：

- `finding_id` 和严重度；
- 发生问题的 turn、消息或媒体证据；
- 期望的外部行为；
- Pumpkin 实际可见行为；
- 对聊天自然度、任务结果或交付可靠性的影响；
- 后续建议，且建议只能依据外部证据提出。

没有发现问题时也必须生成 `report.md`，明确写出“本轮未发现可确认问题”，不能省略
报告。无法从黑盒外部证据确认的问题必须标记为待确认，不能写成确定缺陷。用户 Agent
Skill 失败、Provider 不可用和消息校验失败应放在单独的“测试输入与基础设施”部分，
不能伪装成 Pumpkin 缺陷。

### 8.4 记录完整性

尽管正式交付只有 Markdown，Recorder 仍必须为每条可见消息保留内存中的结构化记录：

```text
VisibleMessage
  event_id
  turn_index
  logical_time
  sender_id
  sender_role: user_agent | background_member | pumpkin
  segments
  reply_to
  forward_nodes
  received_at
  sent_at
  delivery_status
  napcat_message_id
```

这样可以保证 Markdown 不是从模型文本拼接出来的，而是从 Fake NapCat 实际收发事件
渲染出来的；也能在报告渲染失败时保留当前轮的调试现场。

## 9. 评测结论和指标

第一阶段先保证可以人工阅读和定位。评测器可以在内部计算以下指标，但不把分数写入
本轮对话 Markdown：

### 9.1 聊天能力

- 人格一致性；
- 上下文承接；
- 简洁度和无效拆句率；
- 相关回复率；
- 无关抢答率；
- 重复回复率；
- 多消息突发合理性；
- 事实边界和虚假承诺率。

### 9.2 任务能力

- 任务识别率；
- 必要澄清率；
- 多人补充合并率；
- 交错任务的对话和结果隔离正确率；
- 用户可见任务状态说明正确率；
- 用户可见交付物完整率；
- 图片、文件、语音实际交付率；
- 未完成却声称完成的比例；
- 失败恢复率。

### 9.3 自然收敛能力

- 满足退出条件后进入 `cooling`、`quiet` 的比例；
- `quiet` 阶段无授权发送率；
- 仍有任务义务时过早安静率；
- 新唤醒后的重新参与率；
- 无关消息期间的无效回复密度。

任何自动指标都必须能回指 `transcript.md` 中的 turn 和消息。没有可读的对话证据时，
指标不能单独判定成功。

## 10. 场景集合

场景不是固定台词，而是目标、背景事件、媒体覆盖和故障注入的组合：

| 场景 | 用户 Agent 模型行为 | 观察重点 |
| --- | --- | --- |
| 日常聊天 | 和成员聊吃饭、心情或临时小事，偶尔 @ Pumpkin | 人格、简洁、上下文 |
| 直接任务 | 交代一个可执行目标，等待结果 | 任务识别和交付 |
| 延迟补充 | 先给目标，后续补充文件、约束或验收条件 | 任务上下文合并 |
| 结果质疑 | 引用 Pumpkin 的结果，要求依据或修订 | 事实边界和 revision |
| 多模态交流 | 使用图片、文件、表情、语音、转发和复杂引用 | NapCat 消息协议 |
| 任务交错 | 两个成员分别补充两个任务 | 作用域和任务隔离 |
| 失败恢复 | 模型、工具、媒体或发送出现受控失败 | 状态诚实和恢复 |
| 自然退出 | 唤醒并交流几轮后转回成员间闲聊 | `engaged -> cooling -> quiet` |
| 重新唤醒 | quiet 后再次 @ 或明确请求 | 重新参与 |
| 抢答压力 | 相似关键词、多人插话和不等待的消息 | 相关性和回复节奏 |

每组场景可以重复多个 seed。seed 只改变发言时机、目标成员、媒体组合、补充顺序和
背景噪声，不改变必须覆盖的能力类别。

## 11. 安全、隐私和真实模型请求

- 模型请求必须访问配置的真实 Provider；测试不访问真实 QQ、生产数据库或生产文件。
- Pumpkin 作为黑盒启动和运行，评测器只使用 Fake NapCat 的外部入站和出站接口。
- 所有 QQ 身份、群号、文件名和媒体内容使用合成值。
- 模型配置只保存 Provider、模型名、版本摘要和脱敏参数；不保存密钥和完整 Prompt。
- 持久化运行日志只记录事件、状态、关联 ID 和错误码；交互式 CLI 为了实时观察测试
  效果，可以打印合成可见消息的脱敏预览，但不得打印完整 Prompt、模型原始响应或敏感
  参数。
- 媒体文件写入隔离的 `tests/output/` 运行目录，并在测试结束后按保留策略清理。
- 背景事件、seed、模型配置摘要和脱敏响应摘要可以序列化，用于定位失败；不得把它们
  当作新的实时模型结果。
- 任何结构化消息错误、发送失败、任务失败和报告渲染失败都必须明确暴露，禁止静默
  转换为成功。

## 12. 分阶段实现

### 阶段一：最小闭环

1. 定义 `Round`、`ConversationGoal`、`UserAgentDecision` 和 `VisibleMessage`。
2. 实现 Fake NapCat、逻辑时钟、轮次预算和消息 Recorder。
3. 接入一个扮演群聊用户 Agent 的模型，支持观察全量可见消息、send 和 stay silent。
4. 先覆盖文本、@、引用和 Pumpkin 的文本回复。
5. 轮次耗尽后输出完整对话过程的 `transcript.md` 和问题报告 `report.md`。

### 阶段二：完整消息协议

1. 为用户 Agent 接入 `image_asset`、`sticker_asset`、`voice_asset`、`file_asset` 和
   `message_compose` Skill。
2. 接入图片、文件、表情、语音和合并转发。
3. 支持复杂引用、转发节点和多条连续发送。
4. 为媒体增加隔离文件、格式、尺寸、哈希和实际发送状态校验。
5. 增加消息生成失败、发送失败、重复投递和乱序事件。

### 阶段三：能力场景

1. 加入聊天人格、简洁度、相关性和自然退出场景。
2. 加入单步、多步、补充、澄清、交错和失败恢复任务。
3. 验证图片、文件、语音等用户可见交付物真实发送。
4. 通过 turn 和消息关联定位 Pumpkin 的输入、可见回复、媒体交付和发送记录；不要求
   访问 Chat Run、Task Run 或其他内部记录。

### 阶段四：评测和优化

1. 从可读 Markdown 对话中建立人工盲评样本。
2. 增加硬规则 Oracle 和机器指标，但保持报告与内部评测数据分离。
3. 对问题按路由、时序、人格、任务、工具、持久化和安全责任层归类。
4. 只生成候选 Prompt、规则或代码变更，不自动修改生产配置。

## 13. 最低验收标准

完成阶段一必须满足：

1. 扮演群聊用户 Agent 的模型可以看到其他成员和 Pumpkin 的全部可见消息。
2. 该模型可以在每个决策点发送或保持安静，且两种行为都能被记录。
3. 一个 seed 可以稳定决定本地目标、背景事件、媒体选择和轮次边界；真实模型响应必须
   通过模型请求关联 ID 和脱敏摘要记录，不能假设重复请求会生成完全相同的对话。
4. Pumpkin 以正常方式启动，通过 Fake NapCat 的黑盒入站和出站链路运行；评测器不能
   直接调用其内部组件。
5. 一轮达到对话次数后停止，不因背景聊天无限延长。
6. `tests/output/<round_id>/transcript.md` 完整展示这轮对话，不混入隐藏目标和内部
   Prompt。
7. `tests/output/<round_id>/report.md` 明确列出本轮发现的问题、严重度、turn 证据、
   期望行为、实际行为和影响；没有问题时明确写出未发现可确认问题。
8. 报告能判断 Pumpkin 是否自然聊天、是否无关抢答、是否保持简洁以及是否收敛到安静
   状态。

完成阶段二必须额外满足：

1. 扮演群聊用户 Agent 的模型可以调用媒体 Skill，获取合法 `asset_id` 并组合成文本、
   图片、表情包、文件、引用、转发、@、语音和复杂消息。
2. 图片和语音 Skill 的真实 Provider 请求、表情包目录选择、文件生成和
   `message_compose` 校验均有明确结果。
3. 引用和转发关系经过 NapCat schema 校验，媒体文件经过实际发送结果校验。
4. Skill 失败、发送失败、重试和部分交付不会被报告伪装成 Pumpkin 成功。

完成阶段三必须额外满足：

1. 能发现聊天人格、相关性、任务执行和媒体交付中的代表性问题。
2. 能区分 Pumpkin 正确保持安静和应该回复却漏回复。
3. 能区分任务未完成、部分完成和已验证完成。
4. 能验证最终与助手无关的群聊让 Pumpkin 收敛到 `quiet`，且不发送告别式声明。

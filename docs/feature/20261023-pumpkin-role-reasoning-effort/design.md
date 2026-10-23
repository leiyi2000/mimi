---
status: implemented
owner: Mimi maintainers
updated: 2026-10-23
commits: []
---

# Pumpkin 按 Agent 角色配置思考深度

## 1. 背景

Pumpkin 已分别装配 Conversation Agent 和 Task Executor：

```text
user.message / task.event
  -> Conversation Agent
  -> model_chat

TaskHandoff
  -> Task Executor
  -> model_task
```

当前两套 `ModelConfig` 共用 `PUMPKIN_REASONING_EFFORT`，因此聊天与任务请求总是使用同一
思考深度。普通聊天需要低延迟，任务执行则需要更充分的工具规划和结果验收，统一档位无法
同时满足两种运行角色。

MiniMax Anthropic 兼容接口对 `MiniMax-M3.1-Flash` 支持
`output_config.effort`，合法值从低到高为 `low`、`medium`、`high`、`xhigh`、`max`。
省略该字段时默认为 `max`，`none` 不受支持并会返回 `400`。档位越高，thinking token
和响应时间越多。`max_tokens` 同时包含 thinking 和最终文本；设置过小会导致
`stop_reason=max_tokens` 且没有最终文本。官方要求多轮工具调用原样回传包含
thinking、text 和 tool_use 的完整 assistant 内容。依据：

- <https://platform.minimax.cn/docs/api-reference/api-overview>
- <https://platform.minimax.cn/docs/api-reference/text-anthropic-api>
- <https://platform.minimax.cn/docs/guides/text-m3-function-call>

当前 `AnthropicCompatibleProvider` 对聊天和任务均硬编码 `max_tokens=4096`，并在响应
转换时丢弃 thinking 内容块。提高任务思考档位前，必须同时拆分生成预算并保留
provider-native assistant 内容，否则 `max` 更容易消耗完生成预算，且工具调用后的
Interleaved Thinking 无法连续。

## 2. 设计结论

思考深度按 Agent 角色选择，不根据用户文本、关键词、任务类型或当前 Step 动态猜测：

| 运行角色 | 思考深度 | 生成预算 | 适用请求 |
| --- | --- | --- | --- |
| Conversation Agent | `low` | `4096` | 普通聊天、任务意图识别、任务事件通知与交付表达 |
| Task Executor | `max` | `8192` | 搜索、代码、媒体处理、Artifact 生成与任务验收 |

任务完成后的 `task.event` 由 Conversation Agent 消费，因此使用聊天档位。只有实际执行
任务的 Task Executor 使用任务档位。这样不会因为一次任务通知而增加交付消息延迟。

配置仍在进程启动时读取，变更后需要重启服务；“动态”指每次模型请求根据所属 Agent
自动选择已经装配的配置，不提供运行中热更新，也不让模型自行选择档位。

## 3. 配置与类型约束

新增 `ReasoningEffort` 字符串枚举，作为模型配置、环境变量解析和 Provider 请求参数的
唯一取值来源：

```text
low < medium < high < xhigh < max
```

`ModelConfig.reasoning_effort` 使用该枚举，而不是无约束字符串。环境变量存在但取值非法
时，应用必须在启动阶段明确失败，错误信息列出合法档位；不得静默回退或把错误推迟到模型
请求阶段。

删除全局 `PUMPKIN_REASONING_EFFORT`，避免全局值与角色值形成优先级歧义。
`.env.example` 和 `docker-compose.yml` 改为显式配置聊天与任务档位及生成预算：

```dotenv
PUMPKIN_CHAT_REASONING_EFFORT=low
PUMPKIN_TASK_REASONING_EFFORT=max
PUMPKIN_CHAT_MAX_TOKENS=4096
PUMPKIN_TASK_MAX_TOKENS=8192
```

该参数只发送给当前支持 `output_config.effort` 的 MiniMax Anthropic 兼容 Provider。
其他 Provider 不接收 MiniMax 专属字段。

`ModelConfig.max_tokens` 使用正整数，并由各 Provider 映射到对应协议的生成长度字段。
MiniMax 当前官方文档没有公布 `MiniMax-M3.1-Flash` 的服务端数值上限，因此不在本地
虚构最大值；`8192` 是 Pumpkin 的任务预算，不是对服务端上限的声明。部署前通过显式
真实请求验证目标端点接受该值，接口拒绝时阻止部署，不静默降低预算。

## 4. 请求路径

```text
PumpkinConfig.from_env()
  -> model_chat.reasoning_effort = low
  -> model_chat.max_tokens = 4096
  -> model_task.reasoning_effort = max
  -> model_task.max_tokens = 8192

PumpkinRuntime
  -> provider_chat -> Conversation Agent -> effort=low, max_tokens=4096
  -> provider_task -> Task Executor     -> effort=max, max_tokens=8192
```

主 Provider 和同角色的 fallback Provider 必须复用相同档位，避免端点切换后行为漂移。

Anthropic Provider 将完整的 assistant `response.content` 转换为可持久化的结构化内容，
写入 Session Event，并在下一次工具调用请求中原样重建。thinking 内容不写日志、不进入
用户回复，也不作为业务字段读取；它只用于同一模型会话的协议连续性。

## 5. 失败行为

- 非法档位：启动失败并列出五个合法值；
- 未配置：聊天使用 `low/4096`，任务使用 `max/8192`；
- `max_tokens` 非正整数：启动失败；
- MiniMax 返回不支持参数：按现有 `ModelProviderError` 路径显式失败，不做无声降级；
- 非 MiniMax Provider：不发送 `output_config.effort`；
- 任务重试或 fallback：保持任务档位和生成预算，不因重试改变推理策略；
- `stop_reason=max_tokens`：记录结构化失败原因，不能把空文本当作任务完成。

## 6. 验证

自动化测试覆盖：

1. 默认配置分别解析为聊天 `low/4096`、任务 `max/8192`；
2. 五个官方档位均可解析；
3. 非法档位在配置阶段失败；
4. 非法生成预算在配置阶段失败；
5. Conversation Agent 请求携带 `effort=low` 和 `max_tokens=4096`；
6. Task Executor 请求携带 `effort=max` 和 `max_tokens=8192`；
7. thinking、text、tool_use 内容块经过 Session Event 后可无损重建；
8. fallback Provider 保持所属角色的档位和生成预算；
9. 非 MiniMax Provider 不携带 MiniMax 专属参数；
10. 显式网络集成测试确认目标端点接受任务 `max_tokens=8192`；
11. Pumpkin 全量测试、Ruff、compileall 和 `git diff --check` 通过。

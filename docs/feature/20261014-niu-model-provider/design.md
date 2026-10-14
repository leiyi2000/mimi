---
status: implemented
owner: Mimi maintainers
updated: 2026-10-14
commits: []
---

# Niu Model Provider

## 目标

Niu 的 Agent Loop 不再直接依赖 Anthropic SDK 类型。文本模型可以在 MiniMax、GPT 和
Claude 之间切换，QQ 输入、Skill、Tool、任务完成门和发送层不感知厂商协议。

## 边界

`core/model.py` 定义内部模型契约：

- `ModelRequest`：系统提示词、消息、Tool Schema、模型、输出限制和超时。
- `TextBlock`、`ToolUseBlock`：统一响应内容块。
- `ModelResponse`、`ModelUsage`：停止原因和 Token 用量。
- `ModelProvider`：仅暴露 `complete` 和 `close`。
- Provider 级认证、限流、网络和一般错误。

`core/agent.py` 只组装 `ModelRequest`、消费统一响应并把 Provider 错误映射为群聊可见的
稳定提示。`providers/sticker_classifier.py` 复用同一 Provider，不再自行构造厂商 SDK
请求。MiniMax 图片和音乐仍是独立媒体 Provider，不属于文本模型抽象。

## Provider

`providers/model.py` 提供以下实现：

| Provider | 外部协议 | 适用模型 |
| --- | --- | --- |
| `minimax` | Anthropic Messages 兼容协议 | MiniMax M3 系列 |
| `anthropic` | Anthropic Messages 原生协议 | Claude |
| `openai` | OpenAI Chat Completions 协议 | GPT 及兼容端点 |

Anthropic 协议与内部消息结构接近，只规范化响应和异常。OpenAI 适配器额外转换系统消息、
Base64 图片、Function Tool Schema、assistant tool calls 和 tool result 消息，并把
`finish_reason=length` 统一为 `max_tokens`。

Provider 只负责协议差异，不包含 Skill 路由、提示词策略或模型名称判断之外的产品策略。
新增兼容协议时实现 `ModelProvider` 并在 `create_model_provider` 注册即可。

## 配置

`NIU_MODEL_PROVIDER` 选择 `minimax`、`openai` 或 `anthropic`；`NIU_MODEL`、
`NIU_MODEL_BASE_URL` 和 `NIU_MODEL_API_KEY` 描述文本模型。默认保持 MiniMax，并兼容旧的
`MINIMAX_MODEL`、`ANTHROPIC_BASE_URL` 和 `ANTHROPIC_API_KEY`。

媒体能力单独使用 `MINIMAX_API_BASE_URL` 和 `MINIMAX_API_KEY`。因此切换 GPT 或 Claude
不会隐式把 MiniMax 图片、音乐请求发到文本模型端点。

## 失败行为

认证失败、限流、连接超时和其他 API 状态错误在 Provider 边界规范化，Agent 分别返回配置
异常、繁忙或暂时无响应提示。无效 OpenAI JSON、无效 Tool 参数和未知 Provider 会显式
失败，不做静默降级，也不会自动切换到另一个厂商。

## 验证

单元测试覆盖默认与 OpenAI 配置、未知 Provider、OpenAI 图片和 Tool 转换、工具结果回填、
Token 用量、Provider 工厂，以及 Agent 和表情分类器对统一接口的使用。

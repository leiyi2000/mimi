# Niu

群聊 AI 对话插件，角色名为“温蒂”。

## 命令

| 命令 | 说明 |
| --- | --- |
| `@机器人 <内容>` | 在群聊中提及机器人并进行对话 |

插件仅处理“提及当前机器人 + 文本”的群消息。模型通过 Anthropic 兼容接口调用：

| 变量 | 说明 |
| --- | --- |
| `ANTHROPIC_BASE_URL` | Anthropic 兼容接口地址 |
| `ANTHROPIC_API_KEY` | 接口密钥 |

完整环境变量和部署规则见 [部署规范](../../docs/spec/deploy-spec.md)。

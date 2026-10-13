# Help

发送 `帮助` 获取宽版双列的聊天指令图片清单，发送 `帮助 <插件名>` 查看单个分类。

帮助内容来自各插件根目录的 `commands.toml`。新增插件时添加同结构清单即可自动展示：

```toml
[plugin]
key = "example"
name = "示例插件"
description = "插件简介"
order = 100

[[commands]]
name = "示例指令"
usage = "示例指令 <参数>"
description = "指令用途"
badge = "可选标记"
agent = true # 可选；仅对允许 Niu Agent 代为发送的安全指令启用
```

`name` 是实际触发词，必须在所有清单中保持唯一。`usage` 仅用于展示。
`agent` 默认视为 `false`；绑定、登录、登出和其他身份敏感或管理指令不得启用。

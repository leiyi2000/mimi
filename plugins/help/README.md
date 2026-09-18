# Help

发送 `帮助` 获取全部聊天指令的图片清单，发送 `帮助 <插件名>` 查看单个分类。

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
```

`name` 是实际触发词，必须在所有清单中保持唯一。`usage` 仅用于展示。

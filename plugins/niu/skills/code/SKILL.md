---
name: code
description: 在无文件、无网络、无环境变量的 Monty 沙箱中执行受限 Python；复杂计算、JSON 转换、过滤聚合或编排多个只读工具时使用。
allowed-tools: code
---

# Code Mode

使用 `code` 处理纯计算和结构化数据转换。代码只能读取显式 `inputs`，不能访问文件系统、
网络、环境变量、第三方包或子进程。

需要只读宿主能力时，在 `allowed_tools` 中声明工具名，并从代码调用
`await call_tool(name, arguments)`。不得尝试调用记忆写入、媒体生成或其他有副作用的工具。
执行失败后根据错误类型调整代码，不得要求降级到 Shell。

---
name: workspace
description: 在当前 Agent 运行期间持续读写隔离工作区、执行多步 Shell，并显式导出最终文件；适合需要多次检查和修改文件的任务。
allowed-tools: list_workspace read_workspace_file write_workspace_file run_shell export_workspace
triggers:
  - 工作区
  - 多步脚本
  - 分步修改文件
examples:
  - 创建一个文件，运行脚本修改后再把结果发给我
  - 先读取生成的数据，再调整格式并导出
  - 用 Shell 分几步处理这些文本
anti-triggers:
  - 读取 Mimi 仓库文件
  - 读取宿主机文件
  - 只需一次计算且无需保留中间文件
priority: 90
---

# Workspace

工作区只在当前 Agent Run 内存在，与 Mimi、宿主机、其他 Run 完全隔离。先用
`write_workspace_file` 写入文本，或用 `run_shell` 创建和处理文件；后续可通过
`list_workspace`、`read_workspace_file` 检查结果。同一 Run 的工具共享文件，但最终
答复结束后工作区会被删除。

需要把文件发送给用户时，必须显式调用 `export_workspace`。每次 Shell 后检查
`exit_code`、`stderr`、`timed_out` 和 `truncated`，失败时不得声称任务成功。路径只能是
工作区内的相对路径；不要尝试路径穿越、符号链接、Mimi 数据、宿主机目录、私网服务、
凭据或 Docker Socket。

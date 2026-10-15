---
name: code
description: 在隔离 Linux 工作区执行 Bash、Python、pip、curl 和 Git；需要计算、文件处理、联网取数或生成文件时使用。
allowed-tools: code
triggers:
  - 运行代码
  - 执行 Python
  - 跑脚本
examples:
  - 运行这段 Python
  - 把 JSON 转成 CSV
  - 下载数据并生成文件
anti-triggers:
  - 只需搜索网页
  - 读取 Mimi 或宿主机文件
  - 需要多步读取、修改和验证同一批文件
priority: 80
---

# Code Mode

使用 `code` 提交完整 Bash 脚本。工作区是一次性的，可使用 Python、pip、curl、Git、
Shell 和临时文件；公网请求经过受控代理，不能访问 Mimi、宿主机、容器内网、云元数据或
其他私网地址。

通过 `files` 提供少量文本输入。需要把结果发给用户时，必须写入 `output/`；该目录之外
的文件会在任务结束后删除。检查 `exit_code`、`stderr`、`timed_out` 和 `truncated`，
不得把失败执行描述为成功。需要分多步检查和修改文件时改用 `workspace` Skill。不要在
脚本中索取、输出或猜测密钥。

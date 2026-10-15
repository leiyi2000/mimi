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
  - 从公开资源索引筛选并下载目标文件
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

当上一步工具只给出候选数据、资源索引、页面链接或需要批量筛选时，主动用代码继续推进，
不要求用户再提醒执行代码。只把最终要交付的产物写入 `output/`；索引、清单、脚本、
缓存和重复副本保存在目录之外，否则它们也会被自动发送给用户。下载或生成的二进制保持
原始字节，由发送层按内容类型选择图片、语音或文件。

# Niu Sandbox

## 1. 目标

Niu 的 Code Skill 需要真实 Shell、Python、pip、Git、临时文件和公网访问。原有 Monty
只适合纯 Python 子集，无法覆盖依赖安装、网页下载和文件交付。本特性用独立
`sandbox` 替代 Monty，同时保持 Mimi 密钥、数据卷、容器内网和宿主机不可达。

Sandbox 是容器级隔离，不是虚拟机或可信多租户执行平台。它面向群聊中的短时任务，依赖
Compose 的只读根文件系统、Linux UID/rlimit、出口防火墙和资源限额形成纵深防御。

## 2. 运行边界

```text
Niu code Tool
  -> POST sandbox:8080/v1/execute
  -> /workspaces/<execution-id> tmpfs
  -> prlimit + setpriv + 独立 UID
  -> bash / python / pip / curl / git
  -> Squid -> 公网 80/443
  -> output/ 常规文件 -> Niu Artifact
  -> 删除工作区
```

- `mimi` 与 Sandbox 共享 `sandbox-control` 内部网络。
- Sandbox 不读取 `.env`，只接收 `SANDBOX_TOKEN`。
- Sandbox 不挂载插件数据、宿主目录或 Docker Socket。
- Sandbox 根文件系统只读，`/tmp` 和 `/workspaces` 使用有大小限制的 tmpfs。
- 每次任务使用 `20000-29999` 范围内的数值 UID；任务结束后删除整个工作区。
- 服务进程保留 `CHOWN`、`DAC_OVERRIDE`、`KILL`、`SETUID`、`SETGID`、`SETPCAP`
  和 `NET_ADMIN`，任务通过 `setpriv` 清空 capability
  bounding set 并启用 `no-new-privs`。

## 3. 执行 API

`POST /v1/execute` 使用 `Authorization: Bearer <token>`。请求为：

```json
{
  "script": "python - <<'PY'\nprint('ok')\nPY",
  "files": {"input.json": "{}"},
  "timeout": 30
}
```

`files` 只接收相对路径和 UTF-8 文本，拒绝绝对路径、`..` 和预写入 `output/`。响应为：

```json
{
  "exit_code": 0,
  "stdout": "ok\n",
  "stderr": "",
  "timed_out": false,
  "truncated": false,
  "files": [
    {
      "path": "result.json",
      "mime_type": "application/json",
      "data": "<base64>",
      "size": 2
    }
  ]
}
```

Sandbox 只递归收集 `output/` 下的常规文件，忽略并标记 symlink。文件数、单文件/总产物、
请求、输入和 stdout/stderr 均有限额。Niu 将图片和音频转成对应 Artifact，其他文件作为
普通附件发送。

非零退出码、超时、截断和 Sandbox 错误均保留为结构化结果。工具不得把失败描述成成功；
认证失败、繁忙、网络故障和无效响应具有独立错误码。

## 4. 网络策略

任务环境只包含工作目录、PATH 和代理变量，不继承 Sandbox 或 Mimi 环境。Sandbox
entrypoint 使用 iptables owner 匹配任务 UID：

1. 允许任务 UID 连接 `sandbox-egress-proxy:3128`。
2. 拒绝任务 UID 的其他所有出站连接。
3. Sandbox 服务进程仍可响应 Mimi 的控制请求。

Squid 是唯一连接 `sandbox-egress` 公网网桥的组件。它只允许 HTTP 80 和 HTTPS CONNECT
443，并拒绝回环、RFC1918、链路本地、CGNAT、文档保留网段、多播、云元数据和 IPv6
本地地址。目标 ACL 在代理解析域名后生效，用于阻止 DNS 重绑定到私网。

## 5. 资源与生命周期

Compose 限制 Sandbox 为 2 CPU、768 MiB 和 160 PID。应用默认限制：

| 项目 | 默认值 |
| --- | --- |
| 并发任务 | 2 |
| 单任务超时 | 30 秒，硬上限 60 秒 |
| 地址空间 | 512 MiB |
| 子进程数 | 64 |
| 打开文件数 | 256 |
| 请求体 | 1 MiB |
| 文本输入 | 最多 20 个、合计 512 KiB |
| stdout/stderr | 256 KiB |
| 产物 | 最多 10 个、合计 10 MiB |

任务通过 `start_new_session` 建立独立进程组，Sandbox 同时注册为 child subreaper。超时
后 Sandbox 向整个进程组发送 SIGKILL；无论主脚本如何退出，最后都会按任务 UID 扫描并
终止通过 `setsid` 脱离进程组的残留进程，再回收子进程、读取受限输出、收集产物并删除
工作区。Sandbox 达到并发上限时立即返回 `SANDBOX_BUSY`，不建立无界队列。

## 6. 代码结构

- `sandbox/app.py`：认证、请求校验、执行、限额和产物收集。
- `sandbox/entrypoint.sh`：解析代理地址并安装任务 UID 出口规则。
- `sandbox/squid.conf`：目标网段和端口 ACL。
- `plugins/niu/providers/sandbox.py`：Sandbox HTTP Provider 与 `code` Tool。
- `plugins/niu/skills/code/SKILL.md`：模型可见的工作区和交付约束。

原 `plugins/niu/core/monty.py`、Monty 测试、配置和依赖全部删除。20261013 的 Niu Agent
档案保留当时设计事实，当前实现以本档案和 `ARCHITECTURE.md` 为准。

## 7. 验证

- Sandbox 单元测试覆盖路径穿越、输入限额、symlink 和产物限额。
- Niu 测试覆盖成功结果、附件、非零退出、繁忙和无效响应。
- 容器冒烟覆盖 Bash、Python、pip/curl/Git 公网访问、产物、超时和输出截断。
- 网络负向测试覆盖 Sandbox/Mimi 服务名、回环和云元数据地址不可达。
- 发布前运行 Niu 全量测试、Compose 配置校验、Sandbox/Mimi 构建和
  `git diff --check`。

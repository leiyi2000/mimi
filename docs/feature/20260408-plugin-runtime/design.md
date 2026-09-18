---
status: shipped
owner: Mimi maintainers
updated: 2026-09-18
commits:
  - 15c71fc
  - 33edfe9
  - 29f7d07
  - 9b90be6
  - e52720a
---

# 插件运行时

> 建立基于 NapCat 的多进程插件框架，并让 Music、Niu 成为首批独立业务插件。

## 1. 背景与目标

初始提交 `15c71fc` 同时加入 Mimi 入口、NapCat 客户端、PluginManager、Compose 和 demo
插件。目标不是在根进程中堆积命令，而是让每个业务能力拥有自己的依赖和入口。

## 2. Git 演进

| 日期 | 提交 | 变化 |
| --- | --- | --- |
| 2026-04-08 | `15c71fc` | 建立根进程、插件发现、NapCat 与 Compose |
| 2026-04-10 | `33edfe9` | demo 演进为 Music |
| 2026-04-14 | `29f7d07` | 增加 Dockerfile 和 Music 运行依赖 |
| 2026-04-14 | `9b90be6` | Music 增加异常处理与自动重连 |
| 2026-04-15 | `e52720a` | 移除已跟踪 `.env`，加入 Niu |

## 3. 代码实现流程

### 3.1 根进程启动

`mimi/main.py:main()` 是 composition root：

```text
python -m mimi.main
  -> PluginManager()
  -> 扫描 plugins/ 直接子目录
  -> 校验全部 commands.toml
  -> PluginManager.start()
  -> 每个插件执行 uv run main.py
  -> 根 Client 连接 NapCat 并维持主进程
```

`mimi/plugin.py:PluginManager.__init__()` 收集插件目录，再由
`_validate_command_manifests()` 使用 `tomllib` 读取清单。命令名经过 `strip()` 和
`casefold()` 后必须全局唯一；冲突会在任何子进程启动前抛错。

### 3.2 子进程与日志

`PluginManager.start()` 以插件目录为 `cwd`，继承根进程环境并执行
`uv run main.py`。每个插件因此使用自己的 `pyproject.toml`、`uv.lock`、相对数据目录和
入口模块。管理器分别创建线程消费 stdout 与 stderr，并给日志增加插件名前缀，避免管道
写满阻塞子进程。

`stop()` 先发送 `terminate()`，十秒未退出再 `kill()`；`restart()` 复用停止和启动。
当前 `mimi/main.py` 尚未在退出路径主动调用 `stop()`，也不会自动拉起单个已退出插件，
容器级异常由 Compose 的重启策略接管。

### 3.3 消息进入业务插件

根进程不转发 OneBot 事件。APEX、LOL、Music、Help、Niu 和 Yasuo 各自创建
`NapCatClient`，直接消费同一个 WebSocket 服务：

```text
NapCat
  -> 插件 NapCatClient
  -> 插件 dispatcher 或模式匹配
  -> feature handler
  -> 外部服务 / SQLite / 海报
  -> event.send_msg(...)
```

插件连接失败后在自身循环中等待再重连，故障和依赖保持在插件进程内。配置由 Compose
注入，或在手动运行入口时通过 `--env-file .env.dev` 加载。

## 4. 最终边界

- PluginManager 只管理进程，不分发业务消息。
- 插件互不导入。
- 业务依赖不能加入根项目来绕过插件独立环境。
- 临时目录不能放在 `plugins/` 直接子级，否则会被当作插件启动。

## 5. 失败行为

- 清单缺字段或 TOML 无法解析时，根进程启动失败并暴露配置错误。
- 命令重名时，任何插件都不会启动，避免同一消息由多个业务声明所有权。
- 子进程输出持续转发；单个插件崩溃不会直接终止其他已启动插件。
- NapCat 断线由各插件延迟重连，禁止无等待循环。

## 6. 影响与验证

后续所有插件都沿用该进程模型。修改发现或启动逻辑时，应验证命令清单、多个插件启动、
日志转发、NapCat 断线重连和 Compose 配置。

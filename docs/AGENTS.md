# docs 文档维护规范

本文件是 `docs/` 的路由与元规范，回答“文档放哪、怎么找、怎么维护”。修改本目录前先读
仓库根 [`AGENTS.md`](../AGENTS.md)；系统架构事实只写在
[`ARCHITECTURE.md`](../ARCHITECTURE.md)。

## 0. 分类原则

文档按信息类型和生命周期组织，不按读者或代码模块重复拆分：

| 目录 | 信息类型 | 生命周期 |
| --- | --- | --- |
| `spec/` | 强制规范：设计、开发、部署必须怎样 | 慢变、长期有效 |
| `develop-guide/` | 操作经验：环境、排障、优化怎么做 | 持续更新 |
| `feature/` | 特性档案：为什么做、形成了什么设计 | 交付后冻结 |

README 是使用入口，AGENTS 是 Agent 路由入口；两者不复制详细事实，只链接到唯一来源。

## 1. 文档路由

| 我要找 | 去 |
| --- | --- |
| 系统组件、交互、数据所有权 | `../ARCHITECTURE.md` |
| 项目边界与实现硬约束 | `../AGENTS.md` |
| 设计、编码、部署规范 | `spec/` |
| 本地开发、排障、性能优化 | `develop-guide/` |
| 某功能的 Git 演进与设计背景 | `feature/<date-slug>/design.md` |
| 用户命令和插件用法 | `../README.md`、`../plugins/<name>/README.md` |

## 2. 单一事实来源

| 信息 | 唯一归属 |
| --- | --- |
| 当前系统架构 | `ARCHITECTURE.md` |
| 项目级硬约束 | 根 `AGENTS.md` |
| 工程规范 | `docs/spec/` |
| 可重复操作与排障方法 | `docs/develop-guide/` |
| 特性设计与历史决策 | `docs/feature/` |
| 聊天命令元数据 | `plugins/*/commands.toml` |
| 插件用户说明 | `plugins/<name>/README.md` |
| 运行单元与卷 | `docker-compose.yml` |
| 依赖与 Python 版本 | 各 `pyproject.toml`、`uv.lock` |

机械事实优先链接源码或配置，不在多篇散文中复制。

## 3. 特性档案

目录使用 `YYYYMMDD-<kebab-slug>`，日期取首次实现提交日。每个特性默认只保留
`design.md`；只有真实存在独立 PRD、计划或验证材料时才增加文件。

特性文档开头使用：

```yaml
---
status: shipped
owner: Mimi maintainers
updated: YYYY-MM-DD
commits:
  - <hash>
---
```

`commits` 必须能由 `git cat-file -e <hash>^{commit}` 验证。文档描述最终落地设计与关键
演进，不逐提交复述文件列表，不把猜测写成事实。

每篇特性档案必须从当前代码实现出发，至少覆盖：

1. 进程或命令入口，以及依赖如何装配。
2. 消息、HTTP 请求或任务如何进入分发层。
3. 服务、领域模型、持久化和外部接口的调用顺序。
4. 文本、JSON 或图片如何生成并回复。
5. 失败、重连、缓存和局部降级行为。

代码流程使用仓库相对路径和真实模块、类、函数名描述；Git 历史只解释设计为何形成，
不能替代当前实现说明。

## 4. 增删改

- 新规范：放入 `spec/`，更新本文件路由。
- 新操作经验：优先补 `develop-guide/development.md`，只有篇幅失控才拆新文档。
- 新特性：新增一个日期目录，并在 `feature/README.md` 登记。
- 架构变化：只更新根 `ARCHITECTURE.md`，其他文档回指。
- 功能变化：先改 `commands.toml` 和插件 README，再检查根 README。
- 过程材料、草稿、临时截图和一次性排查日志不进入正式文档。

## 5. 文案与命名

- 路径使用小写 kebab-case；入口统一 `README.md`。
- 文档描述当前状态或明确标识的历史设计，不写“刚刚”“之后再做”等过程话术。
- 命令、路径、环境变量和代码标识使用反引号。
- 文档只引用仓库相对路径，不写工作区、用户主目录、临时目录或其他项目的绝对路径。
- 不记录真实 QQ 号、邮箱、用户名、Token、Cookie、回调 URL、设备标识和数据库内容。
- 外部研究材料只描述结论；不得链接个人机器上的反编译目录、临时文件或私有项目。
- 同一事实只写一次，其余位置使用链接。

# Mimi Agent 开发指南

本文件面向编码 Agent 与协作者，定义 Mimi 的仓库边界、硬约束和交付流程。系统当前
长什么样以 [`ARCHITECTURE.md`](ARCHITECTURE.md) 为准；文档怎么组织以
[`docs/AGENTS.md`](docs/AGENTS.md) 为准。

## 阅读顺序

1. 修改架构、插件边界、运行单元或状态所有权前，先读 `ARCHITECTURE.md`。
2. 修改某个插件前，读该插件的 `README.md`、`pyproject.toml`、入口和测试。
3. 查设计/开发/部署规范时，按 `docs/AGENTS.md` 路由到 `docs/spec/`。
4. 搭环境、排障或做性能优化时，读 `docs/develop-guide/`。
5. 理解某个功能为何形成当前设计时，读 `docs/feature/<date-slug>/design.md`。

## 文档路由

| 我正在做的事 | 下一跳 |
| --- | --- |
| 理解系统全景、改架构或状态所有权 | `ARCHITECTURE.md` |
| 修改项目级代码约束 | 本文件 |
| 查设计、开发或部署规范 | `docs/AGENTS.md` → `docs/spec/` |
| 搭环境、修 Bug、优化构建或运行时 | `docs/develop-guide/development.md` |
| 理解历史功能决策 | `docs/feature/README.md` |
| 修改某个插件的用户命令 | `plugins/<name>/README.md` + `commands.toml` |

## 1. 目录与边界

| 目录 | 职责 |
| --- | --- |
| `mimi/` | 进程编排、插件发现和基础 NapCat 客户端，不放业务功能 |
| `plugins/<name>/` | 单个业务插件的入口、服务、模型、渲染、测试和运行数据 |
| `qimei/` | LOL 登录所需的无状态 QIMEI36 Java 服务 |
| `docs/spec/` | 设计、开发与部署的长期规范 |
| `docs/develop-guide/` | 环境、排障和优化等反复查阅的操作经验 |
| `docs/feature/` | 按首次提交日期归档的特性设计与历史 |
| `data/`、`plugins/*/data/` | 运行数据，不提交、不复制进构建上下文 |

跨目录边界：

- 插件不能直接导入另一个插件的代码。
- 根 `mimi/` 不承载 APEX、LOL、Music 等业务逻辑。
- QIMEI 服务只执行原生注册；设备 profile 和 QIMEI36 缓存归 LOL 插件。
- 数据获取、领域模型与 HTML 渲染保持单向依赖。
- 跨插件共享只有在形成稳定公共契约后才允许引入。

## 2. 编码约束

- Python 使用 3.12+ 原生能力，不增加兼容性导入。
- HTTP 使用 `httpx`，不为单个接口引入 SDK。
- 领域数据优先使用普通 `dataclass`；持久化实体使用 Tortoise `Model`。
- 配置集中在 `Config` dataclass 或入口装配，不在业务函数中散落默认值。
- 命令处理器只做参数、权限、用例编排和用户反馈。
- 海报由 Jinja2 生成 HTML，再由 `pytakumi` 渲染；远程图片先经过资产层。
- 新抽象必须减少真实重复或隔离明确边界；不使用装饰器堆叠、元编程或隐式上下文。
- 注释保持少量英文，只解释代码无法直接表达的约束。

## 3. 硬约束

### 3.1 NapCat 与命令

- 所有插件通过 `NAPCAT_HOST`、`NAPCAT_PORT`、`NAPCAT_TOKEN` 连接同一 NapCat。
- 宿主机单独运行插件时使用插件目录下的 `.env.dev`，并把 Compose 服务名改为本机端点。
- 插件必须延迟重连，禁止无间隔循环。
- 新增或修改聊天命令时，必须同步检查处理器、`commands.toml`、插件 README 和测试；
  面向所有用户的能力或输出发生变化时，同时检查根 README。
- `commands[].name` 在全仓库大小写不敏感且唯一。

### 3.2 LOL

- 掌盟请求固定使用 `lolapp/12.8.1 (Android)` User-Agent。
- 先调用 `/go/customize_search/search_type_keyword` 获取 `scene`、`uuid`、`area`，
  再调用战绩接口。
- 模式按 `game_queue_id` 判断：`450` 为极地大乱斗，`3270` 为海克斯大乱斗。
- 登录使用唯一共享掌盟账号；玩家绑定只保存查询偏好。
- 掌盟未提供的数据不能伪造，尤其不能把估算值标成隐藏分/MMR。
- 修改最近战绩数量时，同步常量、清单、插件 README、海报和测试。

### 3.3 数据与安全

- APEX、LOL、Yasuo 默认使用各自 `data/` 下的 SQLite。
- 数据卷路径是运行契约，改名或迁移前必须提供兼容方案。
- `.env`、票据、Cookie、Token、OAuth 回调、设备标识和数据库不得进入 Git、日志、
  文档或测试快照。
- 文档只使用仓库相对路径，不得出现用户主目录、临时目录或其他项目的绝对路径。
- `.dockerignore` 必须排除运行数据、虚拟环境、缓存和构建产物。

## 4. 修改流程

### 新功能

1. 定义输入、输出、权限和失败行为。
2. 在插件边界内完成模型、服务、用例和渲染。
3. 添加命令清单、插件 README 和测试。
4. 若形成新设计决策，在 `docs/feature/` 创建档案。

### Bug 修复

1. 建立稳定复现或固定 fixture。
2. 用日志、响应结构或测试定位责任层。
3. 先补回归测试，再修复最接近根因的代码。
4. 执行目标测试和至少一个相邻模块测试。

### 优化

1. 记录耗时、请求数、图片大小、构建上下文或内存基线。
2. 优先消除重复请求、串行 I/O 和无效构建上下文。
3. 并发请求必须允许可定义的局部失败。
4. 缓存必须有失效依据；设备身份缓存使用 profile digest。

## 5. 验证

```bash
cd plugins/lol && uv run pytest
cd plugins/apex && uv run pytest
cd plugins/help && uv run pytest
docker compose config --quiet
git diff --check
```

网络测试必须显式开启。默认单元测试不依赖第三方服务可用性。修改 Dockerfile 或 Compose
时补目标镜像构建；修改海报时补固定 fixture 的 PNG 验证，并将最终图片写入对应插件的
`tests/output/` 供人工检查样式。该目录必须保持在 Git 忽略范围内。

除纯文档修改外，必须运行所有受影响插件的测试，不得只依赖静态检查。交付前逐项核对：

1. 命令处理器中的触发词、参数、权限和失败提示是否与 `commands.toml` 一致。
2. `commands.toml` 的 `name`、`usage`、`description` 和管理标记是否与实际行为一致。
3. 插件 README 的命令表与当前 `commands.toml` 是否一致。
4. 用户可见能力或输出变化是否需要更新根 README。
5. 最终说明是否列出实际执行的测试命令、结果和未执行项。

## 6. 完成标准

- 当前代码、`ARCHITECTURE.md`、规范和插件 README 一致。
- 受影响插件的测试与相关 Compose 配置校验通过。
- 命令实现、`commands.toml`、插件 README 和根 README 已完成同步检查。
- 没有新增密钥、运行数据、缓存或无关生成物。
- 没有覆盖用户已有修改。
- 最终说明列出改动、验证和未执行项。

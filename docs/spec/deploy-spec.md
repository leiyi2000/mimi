---
status: active
owner: Mimi maintainers
updated: 2026-10-14
---

# 部署规范

本文件定义 Mimi 的构建、配置、数据和发布规则。当前运行单元以
[`docker-compose.yml`](../../docker-compose.yml) 为唯一事实来源。

## 1. 配置

从仓库样例创建运行配置：

```bash
cp .env.example .env
chmod 600 .env
```

`.env.example` 只保存变量名、安全默认值和占位符；`.env` 保存当前环境的真实配置且不得
提交 Git。容器间地址使用 Compose 服务名，例如 `napcat`、`qimei`、`ncm-api`。

### 1.1 基础变量

| 变量 | 必需 | 示例 / 默认值 | 用途 |
| --- | --- | --- | --- |
| `NAPCAT_HOST` | 是 | `napcat` | Compose 内的 NapCat 服务名 |
| `NAPCAT_PORT` | 是 | `3001` | OneBot WebSocket 端口 |
| `NAPCAT_TOKEN` | 是 | `replace-me` | 必须与 NapCat Access Token 一致 |
| `NAPCAT_CLIENT_TYPE` | 否 | `ws` | 根客户端连接类型 |
| `ADMINS` | 管理命令需要 | `123456789,987654321` | 允许登录、登出的 QQ 号，逗号分隔 |

### 1.2 插件变量

| 变量 | 必需 | 示例 / 默认值 | 用途 |
| --- | --- | --- | --- |
| `APEX_AUTH` | APEX 轮换需要 | `replace-with-apex-api-key` | Apex Legends Status API Key |
| `NETEASE_API` | 点歌需要 | `http://ncm-api:3000` | 网易云 API 服务地址 |
| `MUSIC_SIGN_API` | 音乐卡片需要 | `https://apii.xianyuw.cn/api/v1/qq-musicArk` | QQ 音乐卡片签名接口 |
| `MUSIC_SIGN_KEY` | 音乐卡片需要 | `replace-with-xianyuw-token` | 签名接口 Token |
| `NETEASE_COOKIE` | 否 | 不配置 | 仅用于部署时预置网易云登录态 |
| `NIU_MODEL_PROVIDER` | 否 | `minimax` | 文本 Provider：`minimax`、`openai` 或 `anthropic` |
| `NIU_MODEL` | 否 | 随 Provider 变化 | 文本模型名称 |
| `NIU_MODEL_BASE_URL` | 否 | 随 Provider 变化 | 文本模型 API 地址 |
| `NIU_MODEL_API_KEY` | 南瓜需要 | `replace-with-model-token` | 文本模型 Token |
| `ANYSEARCH_API_KEY` | 南瓜需要 | 不配置 | AnySearch API Key |
| `MINIMAX_API_BASE_URL` | 南瓜媒体能力需要 | `https://api.minimaxi.com/v1` | 图片和音乐原生端点 |
| `MINIMAX_API_KEY` | 南瓜媒体能力需要 | `replace-with-minimax-token` | MiniMax 图片和音乐 Token |
| `NIU_TRIGGER_PREFIX` | 否 | `南瓜` | 群消息文本触发前缀 |
| `NIU_MAX_INPUT_IMAGES` | 否 | `4` | 单条南瓜消息的图片上限 |
| `NIU_INPUT_IMAGE_MAX_BYTES` | 否 | `5242880` | 单张输入图片字节上限 |
| `NIU_STICKERS_ENABLED` | 否 | `true` | 启用群表情包收集、整理和回复 |
| `NIU_STICKER_MAX_ITEMS` | 否 | `200` | 每群表情包条目上限 |
| `NIU_STICKER_DAILY_UPDATES` | 否 | `30` | 每群每日表情包变更次数上限 |
| `NIU_STICKER_DAILY_CLASSIFICATIONS` | 否 | `5` | 每群每日后台 AI 分类次数上限 |
| `NIU_GROUP_HISTORY_ENABLED` | 否 | `true` | 是否启用当前群历史查询 |
| `YASUO_API_URL` | 群历史需要 | `http://127.0.0.1:6273` | Yasuo 内部 API |
| `SANDBOX_TOKEN` | 南瓜需要 | 随机高熵字符串 | Mimi 与 Sandbox 间的 Bearer Token |
| `NIU_SANDBOX_TIMEOUT` | 否 | `30` | 单次代码执行秒数，上限 60 |
| `NIU_IMAGE_ENABLED` | 否 | `true` | 图片 Skill；当前账号已完成付费权限验收 |
| `NIU_MUSIC_ENABLED` | 否 | `false` | 完成音乐 API 权限验收后开启 |
| `MLOL_HOST` | 否 | `mlol.qt.qq.com` | 掌盟接口主机 |
| `QIMEI_URL` | LOL 登录需要 | `http://qimei:8080` | QIMEI36 服务地址 |

资产缓存时间等常用可选项已列在 [`.env.example`](../../.env.example)，通常保持默认值
即可。SQLite 路径与 LOL 设备档案（`QIMEI_DEVICE_PROFILE`、`QIMEI_CACHE_PATH`）由插件
使用内置默认值，位于各自 `data/` 目录，通常不需要写入环境配置。该 `data/` 目录必须
持久且可写：设备档案漂移会使 QIMEI36 缓存失效并重新注册，等价于更换设备，可能触发掌盟
风控。设备档案的生成方式见 [LOL 插件说明](../../plugins/lol/README.md#设备档案)。

MiniMax 默认模式兼容旧的 `MINIMAX_MODEL`、`ANTHROPIC_BASE_URL` 和
`ANTHROPIC_API_KEY`。新部署统一使用 `NIU_MODEL_*`；使用 GPT 时将 Provider 设为
`openai`，使用 Claude 时设为 `anthropic`。图片和音乐始终走 MiniMax 原生端点，因此在
文本模型切换到 GPT 或 Claude 后，启用媒体 Skill 仍需单独配置 `MINIMAX_API_KEY`。

### 1.3 外部凭据

#### APEX

在 [Apex API Portal](https://apexlegendsapi.com/auth) 注册项目并获取 API Key，填入
`APEX_AUTH`。该非官方 API 当前可免费申请，初始限速为每秒 5 次请求；Key 仅放在
`.env`，不要提交或写入日志。

#### 音乐卡片签名

在 [咸鱼 API 开放平台](https://apii.xianyuw.cn/api/qq-musicArk) 注册后，从个人中心获取
Token，填入 `MUSIC_SIGN_KEY`。项目使用的“QQ 音乐卡片签名”接口当前标记为免费且需要
Token，默认请求地址已写入 `MUSIC_SIGN_API`。

#### 网易云 Cookie

通常不需要配置 `NETEASE_COOKIE` 或 `NETEASE_COOKIE_FILE`。管理员发送 `网易云登录`
扫码，或发送 `网易云登录 <MUSIC_U 值>`，登录态就会写入默认的
`data/netease_cookie.txt` 并在重启后继续使用。

只有需要在部署时预置登录态时，才执行以下步骤：

1. 在浏览器登录 [网易云音乐 Web 端](https://music.163.com/)。
2. 打开开发者工具，在 Application / Storage 的 Cookies 中选择 `music.163.com`。
3. 找到 `MUSIC_U`，只复制该项的值。
4. 写入 `.env`：`NETEASE_COOKIE=MUSIC_U=<value>`。

规范形式以 `MUSIC_U=` 开头；当前插件也兼容只填写裸值，并会自动补齐前缀。该 Cookie
等同登录凭据，不能写入 `.env.example`、文档、日志、Issue 或测试快照。运行时通过扫码
或 `网易云登录 <MUSIC_U 值>` 保存的 Cookie 优先于环境变量。

### 1.4 安全规则

- Secret 只在运行时注入，不写入文档、日志或测试。
- 新环境变量必须在读取点提供明确默认值或启动失败信息。
- 管理员列表使用 `ADMINS`，权限仍由每个管理命令在处理器内校验。
- `sandbox` 不读取 `.env`，只注入 `SANDBOX_TOKEN`；禁止挂载 Mimi 数据卷、
  Docker Socket 或宿主目录。
- Sandbox 与 Mimi 只共享内部控制网络。任务 UID 的出口只能到 Squid，代理拒绝回环、
  RFC1918、链路本地、CGNAT、云元数据和非 80/443 端口。

## 2. 构建

- `.dockerignore` 排除 `data/`、插件数据、`.git/`、虚拟环境和工具缓存。
- QIMEI 保持独立多阶段 Maven/JRE 镜像。
- Mimi 镜像必须同步每个插件的独立依赖。
- Sandbox 使用只读根文件系统和 tmpfs 工作区；Squid 是唯一公网出口。
- 只有排查缓存问题时使用 `--no-cache`。
- 构建前后关注上下文大小和镜像体积，禁止把数据库、图片缓存或 APK 放入 Mimi 镜像。

## 3. 数据

- NapCat、APEX、LOL、Niu、Yasuo 和 QIMEI 使用显式数据卷。
- 数据卷路径是运行契约，变更前提供迁移和回滚方案。
- 备份包含登录凭据与聊天数据，必须加密并限制访问。
- QIMEI APK 缓存可以重建；LOL 会话和用户绑定不能当作缓存删除。

## 4. 发布

```bash
docker compose build mimi qimei sandbox
docker compose up -d
docker compose ps
docker compose logs --tail=200 mimi qimei
```

只修改插件 Python 业务时可单独重建 `mimi`；修改 Code Tool、Sandbox 或代理策略时同时
重建 `mimi sandbox` 并重启 `sandbox-egress-proxy`；修改 QIMEI 协议、原生库或
Java 代码时同时重建 `qimei`。重建后至少验证帮助命令、一个文本命令和一个图片命令。

## 5. 回滚

1. 保留上一个可用 Git revision 或镜像。
2. 判断是否涉及数据库结构、设备 profile 或数据卷。
3. 停止写入，必要时恢复数据备份。
4. 恢复代码或镜像并重建受影响服务。
5. 执行同一组冒烟验证。

不得用 `git reset --hard` 清理包含未知修改的部署目录。

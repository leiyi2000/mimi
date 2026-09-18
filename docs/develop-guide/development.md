# Development

## 本地环境

需要 Python 3.12+、uv、Docker 与 Docker Compose。修改 `qimei/` 还需要 JDK 17 和
Maven 3.9。

根项目和每个插件分别管理依赖：

```bash
uv sync

for plugin in plugins/*; do
  if [ -f "$plugin/pyproject.toml" ]; then
    (cd "$plugin" && uv sync)
  fi
done
```

### 生成 `.env`

从可提交的样例创建本地配置，不要从聊天记录或其他环境复制整份 Secret：

```bash
cp .env.example .env
chmod 600 .env
```

至少配置 NapCat 连接。需要 APEX 地图轮换或音乐卡片时，再分别填写 `APEX_AUTH` 和
`MUSIC_SIGN_KEY`；申请入口、网易云 `MUSIC_U` 获取步骤和完整变量说明见
[部署规范](../spec/deploy-spec.md#13-外部凭据)。

### 生成 `.env.dev`

仓库根 `.env` 服务于 Compose，里面的 `napcat`、`qimei`、`ncm-api` 是容器 DNS 名。
插件直接运行在宿主机时，先在目标插件目录生成仅供本地使用的 `.env.dev`：

```bash
cd plugins/lol
umask 077
cp ../../.env .env.dev
chmod 600 .env.dev
```

复制后按运行位置修改服务端点：

| 插件 | 本地覆盖 |
| --- | --- |
| Help、APEX、LOL、Music | `NAPCAT_HOST=127.0.0.1`、`NAPCAT_PORT=3001` |
| Music | `NETEASE_API=http://127.0.0.1:3010` |
| LOL | 本地启动 QIMEI 后使用 `QIMEI_URL=http://127.0.0.1:8080` |

`.env.dev` 可以保留根 `.env` 中已有的 Token、管理员和业务密钥，但不得提交、粘贴到文档
或打印到日志。`NETEASE_COOKIE` 留空时可运行后由管理员发送 `网易云登录` 扫码建立
登录态；如需预置，必须使用 `MUSIC_U=<value>`，不得复制到测试或提交记录。

`.env.dev` 已由 `.env.*` 忽略，只有 `.env.example` 允许提交。APEX、LOL、Music 和
Help 均通过 `uv run main.py --env-file .env.dev` 显式加载；根 `PluginManager` 启动插件
时则直接继承当前进程环境。

启动外部依赖：

```bash
docker compose up -d napcat ncm-api
(cd qimei && mvn -q package)
(cd qimei && QIMEI_DATA_DIR=../data/qimei java -jar target/qimei-service.jar)
```

运行全部插件或单个插件：

```bash
uv run python -m mimi.main

cd plugins/lol
uv run main.py --env-file .env.dev
```

## 新增插件

推荐最小结构：

```text
plugins/example/
├── main.py
├── settings.py
├── commands.toml
├── pyproject.toml
├── uv.lock
├── README.md
├── features/
└── tests/
```

接入步骤：

1. 建立独立依赖环境。
2. 在 `main.py` 装配配置、数据库、客户端和延迟重连。
3. 将协议、模型、功能和渲染放入独立模块。
4. 添加 `commands.toml`，确认命令全局唯一。
5. 添加插件 README、测试和必要的数据卷。
6. 确认运行数据被 Git 与 Docker 构建排除。

## 测试

```bash
cd plugins/help && uv run pytest
cd plugins/apex && uv run pytest
cd plugins/lol && uv run pytest
```

海报专项：

```bash
cd plugins/lol
uv run pytest tests/test_battle_poster_image.py tests/test_detail_poster_image.py

cd plugins/apex
uv run pytest tests/test_stats.py tests/test_rotation.py
```

网络测试默认跳过。新增测试使用固定 fixture，不能让第三方接口波动决定结果。

### 图片回复调试

验证图片回复的测试不能只断言 `base64://` 或 PNG 文件头，还必须把最终 PNG 写入当前
插件的 `tests/output/`，供人工检查字体、布局、裁切、长文本和资源缺失时的样式：

```python
from pathlib import Path

output = Path(__file__).parent / "output" / "battle.png"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(png)
```

文件名应稳定并表达场景，例如 `battle.png`、`detail.png`、`empty-state.png`。测试仍需
断言 PNG 签名和合理大小，不能用人工检查代替自动断言。`plugins/*/tests/output/` 是本地
调试产物，不进入 Git；需要评审时只在评审渠道临时附图。

## 故障修复

按链路定位，不从用户提示直接猜根因：

```text
容器 -> 插件进程 -> NapCat -> 命令分发 -> 外部 API
     -> 领域模型 -> 图片资产 -> HTML/PNG -> 消息发送
```

基础证据：

```bash
docker compose ps
docker compose logs --tail=200 mimi
docker compose logs --tail=200 napcat
docker compose logs --tail=200 qimei
docker compose config --quiet
```

### 机器人无响应

- Compose 内 `NAPCAT_HOST` 应为 `napcat`，不是 `127.0.0.1`。
- 确认目标插件打印启动日志，且没有持续重连。
- 多数命令只匹配单个纯文本消息段。
- `commands.toml` 语法或重复命令会阻止主进程启动。

### LOL 登录或查询失败

- 确认发送者属于 `ADMINS`，QIMEI `/healthz` 正常，LOL 数据目录可写。
- OAuth 回调必须完整包含 `access_token` 和 `openid`，但不得记录到日志。
- 搜索必须先获取 `scene`、`uuid` 和 `area_id`。
- 掌盟请求必须使用 `lolapp/12.8.1 (Android)` User-Agent。
- 模式错误先检查 `game_queue_id`，不用地图 ID 推断。

### 海报失败

- 先用固定 fixture 判断是数据、资产还是渲染问题。
- 检查模板上下文、远程图片、字体、容器内存和临时目录。
- 单张资产失败应局部降级。
- PNG 渲染留在 `asyncio.to_thread`，避免阻塞事件循环。

### 其他插件

| 现象 | 检查 |
| --- | --- |
| APEX 轮换无响应 | `APEX_AUTH`、ALS API、插件日志 |
| 点歌失败 | 分别检查 `ncm-api` 与音乐卡片签名服务 |
| VIP 歌曲失败 | `网易云信息`、Cookie 是否过期 |
| 温蒂不回复 | 消息是否为“@机器人 + 文本”、模型地址与密钥 |
| Yasuo 查不到数据 | 是否为群消息、数据库卷、`POST /` 与分页参数 |

## 性能优化

优化前记录场景、数据规模、耗时、请求数、输出大小和资源占用。

### 构建

- 用 `.dockerignore` 排除数 GB 的运行数据、缓存和虚拟环境。
- 比较首次构建、仅源码变化和依赖变化三种耗时。
- 不把 SQLite、下载图片或 QIMEI APK 烘焙进 Mimi 镜像。

### 网络与渲染

- URL 去重后下载，稳定资产使用带 TTL 的缓存。
- 互不依赖的请求受控并发，并保留局部成功结果。
- 高频 HTTP 可复用 `httpx.AsyncClient`，但必须管理关闭。
- 显式设置超时；只重试幂等操作并使用有限退避。
- 分别测量 HTML 构建、资产下载、缓存命中与 PNG 渲染。

### 数据与进程

- 列表接口分页并限制 `page_size`。
- 删除消息时考虑清理孤立图片。
- 多条 NapCat 连接是插件隔离的成本，只有测量证明成为瓶颈时才集中分发。
- 优先补子进程退出监控、信号处理和统一重连退避。

优化验收记录：

```text
场景：
数据规模：
修改前：
修改后：
正确性验证：
资源变化：
回滚条件：
```

## 提交前

```bash
git status --short
git diff --check
docker compose config --quiet
```

再执行受影响插件的测试，确认没有 Secret、运行数据、缓存或构建产物进入变更。

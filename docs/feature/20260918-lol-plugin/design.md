---
status: shipped
owner: Mimi maintainers
updated: 2026-09-18
commits:
  - c69bac9
---

# LOL 插件与命令目录

> 在独立插件边界内接入掌盟登录、玩家查询、战绩海报和全局帮助目录。

## 1. Git 演进

| 日期 | 提交 | 变化 |
| --- | --- | --- |
| 2026-09-18 | `c69bac9` | 加入 LOL、QIMEI 和 Help 插件，并为已有插件补齐命令清单 |

该提交同时建立了功能实现和命令元数据契约，因此作为一个特性归档，不再拆分多篇提交说明。

## 2. 代码实现流程

### 2.1 LOL 启动与依赖装配

`plugins/lol/main.py` 加载可选 `.env.dev` 后，通过 `Config.from_env()` 读取 NapCat、
掌盟、QIMEI、数据库和管理员配置。`init_db()` 创建绑定与共享会话表，随后只在入口装配
一组服务：

```text
MlolClient
  -> MlolAuth
  -> RoleService
  -> PlayerSearch
  -> BattleService
QimeiClient
  -> login.setup(...)
MlolAuth + PlayerSearch + BattleService
  -> query.setup(...)
```

导入 `features` 时，`@command` 将登录、绑定、战绩和对局 handler 注册到 dispatcher。
NapCat 消息必须恰好包含一个 `Text` 段才会分发；连接失败后等待五秒重连。

### 2.2 管理员登录

`LOL登录` 首先由 `_require_admin()` 校验发送者。没有回调参数时：

1. `QimeiClient.get()` 解析或生成 `data/device_profile.json`。
2. profile digest 命中 `data/qimei36.json` 时直接复用；否则将原生配置 POST 到 QIMEI。
3. `qimei/QimeiServer` 以单线程执行器串行调用 Unidbg 和 `libqimei.so`，校验并返回
   36 位 QIMEI。
4. `build_authorize_url()` 使用同一设备信息生成 QQ OAuth 地址，并要求管理员私聊完成。

管理员带完整回调再次发送 `LOL登录` 后，`parse_callback()` 提取 OAuth 凭据，
`MlolAuth.login_by_qq()` 调用 `/go/auth/login_by_qq`，把票据写入固定主键为 `1` 的
`MlolSession`。随后 `RoleService.lol_roles()` 查询端游角色，并把首个角色的
`uuid`、`scene`、大区写回共享会话。

业务请求前，`MlolAuth.ensure_fresh()` 根据上游返回的刷新时间按需更新 client ticket 和
web ticket；刷新失败会把会话标记为过期。`LOL登出` 直接删除共享会话。

### 2.3 绑定与玩家解析

`LOL绑定` 使用发送者 QQ 号作为 `LolBinding` 主键保存昵称，`LOL解绑` 删除它。
`LOL战绩` 优先使用命令参数，否则读取绑定。查询必须先经过
`PlayerSearch.find()`：

```text
昵称
  -> GET /go/customize_search/search_type_keyword
  -> 解析 lolIntent
  -> Player(uuid, scene, area_id, nickname, avatar, rank)
```

搜索结果优先选择昵称完全匹配项；只有一个候选时允许回退。缺少 `scene` 或 `uuid` 的
候选不会进入战绩链路。

### 2.4 战绩与详情

`BattleService.list()` 使用搜索得到的目标身份和共享账号身份调用
`/go/battle_info/get_battle_list`，转换为最多八局 `Battle`。隐藏标记会直接回复用户，
不会继续请求详情。

列表成功后，`handle_battle()` 并发请求能力信息和每局详情。能力信息失败时使用列表数据
构造空缺可接受的 `PlayerOverview`；单局详情失败只跳过该局扩展数据。成功的
`(Player, BattlePage)` 保存在按发送者隔离的进程内 `_recent`，供 `LOL对局 <序号>`
再次获取指定详情。

`mlol/models.py` 的 dataclass 负责把弱类型响应转换为稳定领域对象，并计算展示所需的
KDA、伤害、参团率、队伍和模式。模式只按 `game_queue_id` 判断：`450` 是极地大乱斗，
`3270` 是海克斯大乱斗。

### 2.5 海报回复

`battle_poster()` 和 `detail_poster()` 只把领域对象转换为 Jinja2 上下文及资产 URL。
`fetch_assets()` 去重后读取缓存并并发下载缺失图片，单张失败可降级。`_send_poster()`
把同步 `pytakumi` 渲染放到线程，再将 PNG 编码为 `base64://` 的 NapCat `Image`。
渲染异常统一回复文本，不让异常退出消息循环。

### 2.6 Help 命令目录

`plugins/help/main.py` 为独立 NapCat 客户端。收到 `帮助 [插件名]` 后，
`CommandCatalog` 每次重新读取 `plugins/*/commands.toml`，转换为不可变 `Plugin` 和
`Command` dataclass，并校验插件 key、必填字段和命令名唯一性。筛选后的目录由
`render_help()` 生成 PNG 并作为 Base64 图片回复。

根 `PluginManager` 在进程启动前再次校验命令名全局唯一。Help 负责展示时的数据完整性，
根进程负责启动前拒绝歧义，两层校验使用同一批清单但职责不同。

## 3. 设计取舍

掌盟依赖原生 QIMEI 注册，但设备身份和登录会话属于 LOL 业务。QIMEI 服务因此只接受
原生配置并返回 QIMEI36，不持有玩家绑定、票据或设备缓存。命令说明使用 TOML 清单作为
机器可读契约，让帮助页不再解析各插件实现。

## 4. 状态与边界

- 掌盟请求固定使用 `lolapp/12.8.1 (Android)` User-Agent。
- 模式按 `game_queue_id` 判断：`450` 为极地大乱斗，`3270` 为海克斯大乱斗。
- 掌盟未返回的数据不估算或伪装成隐藏分/MMR。
- 新增或修改命令时同步处理器、`commands.toml`、插件 README 和测试。
- OAuth 回调、Cookie、Token、设备标识和数据库不进入日志、文档或 Git。

## 5. 失败行为

- QIMEI 服务不可用、返回值格式错误或原生注册失败：登录停在设备初始化阶段。
- OAuth 回调缺字段或掌盟响应缺票据：不创建共享会话。
- 搜索没有唯一玩家：不猜测目标，不进入战绩接口。
- 共享票据失效：提示管理员重新登录，不以游客结果伪装成功。
- 能力信息或部分详情失败：保留可用的战绩列表并局部降级。
- 图片下载或渲染失败：缺失资产继续渲染；整体渲染失败则回复文本。
- 命令清单无效：Help 返回加载失败；全局命令冲突则根进程拒绝启动。

## 6. 验证

修改时覆盖命令去重、帮助目录聚合、登录权限、搜索到战绩链路、绑定、固定响应解析、
海报 PNG 和 QIMEI 健康检查。战绩、详情和帮助图片测试必须将最终 PNG 写入各自
`tests/output/`。

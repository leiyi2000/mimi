# lol

LOL 端游战绩查询插件（掌上英雄联盟接口）。

插件使用一个共享掌盟账号登录，所有用户查询均复用该登录态。目标玩家通过联盟
昵称 ID 搜索（例如 `折断的骨头#29510`），不需要目标玩家登录。

## 命令

| 命令 | 说明 |
|---|---|
| `LOL登录` | 管理员获取 QQ 授权链接 |
| `LOL登录 <回调URL>` | 管理员登录或替换唯一的共享掌盟账号 |
| `LOL登出` | 管理员清除共享登录态 |
| `LOL绑定 <昵称#编号>` | 保存发送者常用的目标昵称，查询时可省略参数 |
| `LOL解绑` | 清除已绑定的昵称 |
| `LOL战绩 <昵称#编号>` | 搜索玩家并展示最近 8 局；省略昵称时使用绑定值 |
| `LOL对局 <1-8>` | 查询发送者最近一次战绩结果中的指定对局 |

`LOL绑定` 只保存 QQ 用户自己的查询偏好，不创建登录态。所有请求都使用共享账号的
Cookie 和 WT。目标玩家隐藏或未授权游戏数据时，插件直接返回掌盟原始限制提示。
战绩头部的段位、赛季胜率来自能力信息接口；近 8 局 KDA 和平均评分由最近 8 局真实
战绩计算。掌盟不提供隐藏分/MMR 字段，因此不会伪造该数据。

管理员 QQ 通过 `ADMINS` 配置，多个 QQ 号使用逗号分隔。未配置时登录和登出
命令默认不可用。

## 登录机制

QQ OpenSDK 授权 → `access_token`+`openid` → `login_by_qq`（`mcode`=QIMEI36）→
共享掌盟票据（ct/ctt/wt），固定持久化为唯一 `MlolSession`。票据在业务请求前按需续期。

查询链路：

```text
昵称 ID
  -> GET /go/customize_search/search_type_keyword
  -> uuid + scene + regionId
  -> POST /go/battle_info/get_battle_list
  -> POST /go/battle_info/get_battle_detail_h5
```

QIMEI36 由独立的 [qimei](../../qimei/README.md) 服务提供（`QIMEI_URL`，
容器内 `http://qimei:8080`），插件仅通过 HTTP 取值。

## 静态资料（英雄 / 强化 / 召唤师技能）

海报把英雄 ID、大乱斗强化和召唤师技能翻译成中文名与图标。除召唤师技能外全部**实时请求 +
缓存**，仓库不再存数据文件：

- **英雄**：启动时从公开 CDN `heroList/hero_list.js` 拉取全量（id / 别名 / 中文名）。
- **强化**：启动时从 CommunityDragon `cherry-augments.json`（zh_cn）拉取 id 与中文名，并解析
  图标——优先命中掌盟官方图床 `act/img/rune/{resource}_large.png`，掌盟没有的通用强化回退到
  CommunityDragon 的图标。
- **召唤师技能**：无公开清单且几乎不变，内联为 `mlol/game_data.py` 的常量，图标用官方 gtimg。

结果写入运行缓存 `data/game_data.json`，按 `LOL_GAME_DATA_TTL_DAYS`（默认 30 天）过期。未过期
跳过网络；过期后使用源站的 `ETag` / `Last-Modified` 发起条件请求，资源未变化时只续期缓存，不重复
下载和解析。拉取失败或断网时回退缓存；无缓存时英雄/强化名退化为掌盟响应里的原始值，跳过缺失
图标，永不阻塞启动。未收录的强化按 `掌盟未提供的数据不伪造` 原则跳过展示，并把原始标识追加到
`data/unknown_augments.jsonl` 供排查，不猜测名称。`data/` 为持久化卷，首次刷新后即缓存。

海报图片缓存遵循浏览器式 HTTP 缓存语义：优先使用 `Cache-Control` / `Expires` 判断新鲜度，过期后
携带 `ETag` / `Last-Modified` 验证，源站返回 `304` 时继续复用本地图片。源站没有缓存策略时使用
`LOL_ASSET_CACHE_TTL_DAYS`（默认 7 天）作为兜底；网络异常时允许使用已校验的过期图片。缓存正文与
元数据均先写入同目录临时文件，再原子替换正式文件，进程在写入途中退出不会破坏已有缓存。

## 设备档案

QIMEI36 绑定在一份设备指纹上，由 `QIMEI_DEVICE_PROFILE` 指向的 JSON 保存，默认
`data/device_profile.json`。它既用于向 qimei 服务注册 QIMEI36，也填充 QQ 授权链接展示的
设备信息。QIMEI36 缓存以该档案的摘要为键：档案不变则复用缓存、跳过注册；档案变动则重新注册。

生成方式，任选其一：

- **自动伪造（默认）**：不配置任何东西。首次 `LOL登录` 时若档案不存在，插件从内置真实机型
  模板随机选一台、只随机化每份安装独有的标识，写盘后跨重启稳定。适合大多数场景。
- **抓取真机（可选）**：连上开启 USB 调试的安卓机，运行脚本把真机指纹写入档案。

  ```bash
  uv run python scripts/capture_device_profile.py --output data/device_profile.json
  # 连接多台设备时追加 --serial <序列号>
  ```

- **手写 JSON（可选）**：把符合字段的 JSON 放到该路径，缺失字段用默认值补齐。

自动伪造只在首次随机一次，之后不变；**风险来自档案丢失或漂移**：档案一变，QIMEI36 缓存
失效并重新注册，等价于「换了一台新设备」，可能触发掌盟风控。因此务必让档案所在的 `data/`
目录持久化（容器挂载卷、路径可写），不要指向会被清理的临时或外部路径。

## 本地运行

```bash
# 先起 qimei 服务（另开终端，或 docker compose up -d qimei）
uv run main.py --env-file .env.dev
```

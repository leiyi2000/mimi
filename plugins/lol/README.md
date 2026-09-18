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

## 本地运行

```bash
# 先起 qimei 服务（另开终端，或 docker compose up -d qimei）
uv run main.py --env-file .env.dev
```

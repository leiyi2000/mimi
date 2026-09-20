---
status: shipped
owner: Mimi maintainers
updated: 2026-09-20
---

# 手游战绩接口调查

## 1. 证据等级

| 等级 | 含义 |
| --- | --- |
| 已确认 | 掌盟 Android 12.8.1 客户端存在明确路径、请求构造或响应模型 |
| 部分确认 | 接口或字段已确认，但缺少当前账号的真实成功响应 |
| 待确认 | 只能从服务端路由字段继续追踪，当前不得作为实现契约 |

所有 `/go/*` 请求沿用现有 `MlolClient` 的 `lolapp/12.8.1 (Android)` User-Agent 和
Cookie 登录态。文档不记录真实 Cookie、角色标识、`scene` 或 `battleId`。

## 2. 手游角色

### `POST /go/account/get_roles_by_game_v2`

状态：部分确认。

请求：

```json
{
  "game_id": "lgame"
}
```

客户端模型表明，成功响应的 `data` 是单个游戏对象：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `gameId` | string | 游戏标识，应为 `lgame` |
| `gameName` | string | 游戏名称 |
| `gameIcon` | string | 游戏图标 |
| `list` | array | 登录账号列表 |

`list[]` 包含 `accountId`、`accountType`、`uuid`、`isRegister`、`iconUrl`、
`authStatus` 和 `roles[]`。角色对象包含 `roleId`、`roleName`、`areaId`、
`areaName`、`level`、`roleIdentity`、`gameId`、`uuid` 等字段。

当前共享账号的只读请求到达业务接口，但没有返回该账号自己的手游角色。公开玩家查询已
确认可以从搜索结果取得目标角色身份：

### `GET /go/customize_search/search_type_keyword`

```text
keyWord=<role-name>
searchType=1
page=0
pageSize=10
gameId=lgame
```

手游结果使用 `lgameIntent`，其路由为 `qtpage://lgame/battle`，查询参数包含 `scene`
和 `uuid`。带 `#编号` 的完整游戏 ID 未命中，而去掉 `#编号` 后的角色名可以命中；
实现时应先按角色名搜索，再做精确昵称和区服筛选，不能把端游的 `lolIntent` 解析逻辑
直接复用。

## 3. 战绩列表

### `POST /go/lgame_battle_info/battle_list`

状态：已通过真实公开玩家响应确认。

首屏请求：

```json
{
  "scene": "<mobile-role-scene>",
  "params": "",
  "wins": "1"
}
```

翻页请求在相同参数上增加服务端上页返回的游标：

```json
{
  "scene": "<mobile-role-scene>",
  "params": "",
  "wins": "1",
  "next": "<cursor>"
}
```

参数语义：

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `scene` | 是 | 目标手游角色身份 |
| `params` | 是 | 模式筛选；全部战绩为空字符串，其他值由模式配置接口下发 |
| `wins` | 是 | 客户端固定传字符串 `"1"` |
| `next` | 否 | 非首屏请求携带的游标 |

客户端按下列顶层结构解析响应：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `result` | int | `0` 成功；`100` 为访客权限受限但仍可解析 |
| `msg` | string | 错误或访客权限提示 |
| `next` | string | 下一页游标；空字符串或 `-1` 表示结束 |
| `private_status` | bool | 战绩隐私状态 |
| `data` | array | 战绩条目 |
| `wins` | array | 各模式胜场，元素为 `type`、`num` |
| `share_message` | object | 分享信息 |
| `ai_tip` | string | AI 提示文案 |

`data[]` 已确认字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `avatar` | string | 本局英雄头像 |
| `gameResultTitle` | string | 结果标题 |
| `game_type` | string | 模式名称 |
| `kill` | int | 击杀 |
| `death` | int | 死亡 |
| `assist` | int | 助攻 |
| `time` | int | Unix 时间戳（秒），客户端乘以 1000 后格式化 |
| `win_flag` | int | 胜负标记 |
| `intent` | string | 单局详情路由 |
| `main_honor_url` | array | 主要荣誉图标 |
| `other_honor_url` | array | 其他荣誉图标 |
| `light_honor_url` | array | 高亮荣誉图标 |
| `ai_tag` | object/null | AI 标签 |

实现注意：该接口把 `next`、`private_status` 和 `wins` 与 `data` 放在同一层。
现有 `MlolClient._data()` 会只返回 `parsed["data"]`，直接复用将丢失分页和隐私元数据。
实现时应增加显式的 envelope 读取能力，不能在手游服务中绕过客户端重复写 HTTP。

## 4. 战绩概览

### `POST /go/lgame_battle_info/overview`

状态：已通过真实公开玩家响应确认。

```json
{
  "scene": "<mobile-role-scene>"
}
```

该接口用于手游战绩页头部概览。已确认响应包含 `head` 和 `body`：`head` 提供区服、
角色名、等级和段位，`body.data[]` 提供总局数、胜率、五杀、英雄数、常用英雄和皮肤数
等展示项。字段仍按服务端标题驱动，不能依赖固定数组下标。列表功能不能依赖概览成功；
概览失败时应允许仅渲染列表已有数据。

`POST /go/lgame_battle_info/home_card` 使用相同的 `scene` 请求体，返回首页卡片数据。
它不是战绩列表的必要依赖，第一阶段不接入。

## 5. 单局详情

### `POST /go/lgame_battle_info/detail_v2`

状态：已通过真实符文大乱斗响应确认。

已观察到的符文大乱斗 `intent` 指向：

```text
https://lolm.qq.com/lolmzmgamedetail/page/hex-aram/index.html
```

其查询参数包含 `guid`、`izoneareaid`、`scene`、`userId`、`selectedTabKey` 和
`navigationBarHidden`。H5 脚本使用前三个参数请求详情：

```json
{
  "guid": "<battle-guid>",
  "izoneareaid": "<zone-area-id>",
  "scene": "<mobile-role-scene>"
}
```

响应为通用 `{result, data}` envelope，`result == 0` 时 `data` 包含：

| 字段 | 说明 |
| --- | --- |
| `head` | 胜负、模式、KDA、开局时间、时长、评分类型与评分值 |
| `battleTab.myCamp` | 目标玩家所属阵营 |
| `battleTab.teamData[]` | 双方胜负、总经济、团队 KDA 和成员列表 |
| `teamPlayer[]` | 玩家英雄、等级、KDA、装备、技能、符文、评分、经济和战斗统计 |
| `teamPlayer[].playData[]` | 玩家、队伍均值和全场最佳等维度的详细统计 |
| `teamPlayer[].runeEffect[]` | 符文名称、说明和本局效果 |
| `teamPlayer[].hexbuffs[]` | 符文大乱斗强化名称、品质和描述 |
| `highlightTab` | 本局时刻列表 |

该接口依赖掌盟 Cookie，不能套用端游 `/go/battle_info/get_battle_detail_h5` 的
`uuid/area_id/game_id/start_time` 请求体。已观察到的 H5 `intent` 不包含
`battleId`，而是使用 `guid` 表示对局。

## 6. 剩余验证

当前已取得搜索、概览、列表和符文大乱斗详情的成功响应。实现前还需补充：

1. 将成功响应脱敏为列表和详情 fixture。
2. 补一局普通匹配或排位，确认其 H5 路径和详情结构是否与符文大乱斗一致。
3. 验证 `next` 翻页、`result == 100`、隐藏战绩和详情不存在的响应。
4. 确认搜索结果存在同名玩家时，`#编号` 应如何参与消歧。

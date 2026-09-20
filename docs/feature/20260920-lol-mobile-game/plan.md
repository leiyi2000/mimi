---
status: shipped
owner: Mimi maintainers
updated: 2026-09-20
---

# 手游战绩适配计划

## 1. 交付范围

目标是在 `plugins/lol/` 内增加英雄联盟手游查询，不改变现有端游命令行为、共享登录态和
数据卷。

首个可交付版本包含：

- 独立的 `MLOL绑定`、`MLOL战绩` 和 `MLOL对局` 命令。
- 手游玩家或角色解析。
- 最近战绩列表、隐私状态和游标解析。
- 独立的手游战绩领域模型与海报。
- 单局详情模型与海报。
- 命令清单、插件 README、根 README 和测试同步。

不在首版范围内：资产、宠物、荣誉、在线状态、赛季历史和模式筛选 UI。

## 2. 实施门禁

### 门禁 A：角色与列表协议（已通过）

公开玩家只读请求已确认：

- 搜索结果的 `lgameIntent` 提供 `scene` 和 `uuid`。
- `battle_list` 返回顶层分页、隐私、胜场和战绩列表。
- 符文大乱斗 `intent` 指向 `lolm.qq.com` 详情 H5。

开始实现前仍需把响应脱敏为固定 fixture。

### 门禁 B：详情核心协议（已通过）

详情 H5 脚本和真实响应已确认：

- 数据接口为 `POST /go/lgame_battle_info/detail_v2`。
- 请求字段为 `guid`、`izoneareaid` 和 `scene`。
- 请求沿用掌盟 Cookie；该链路不使用 `battleId`。
- 成功响应包含对局头部、双方队伍、详细统计、符文、强化和本局时刻。

当前实现按 `detail_v2` 的公共字段解析，并对缺失字段降级。普通模式、隐私、过期和无
数据响应仍需在取得脱敏样本后继续扩大回归覆盖，不阻塞已确认链路交付。

## 3. 代码设计

### 3.1 HTTP 客户端

在 `plugins/lol/mlol/client.py` 增加明确命名的原始 envelope 请求方法，复用现有
URL、User-Agent、Cookie、超时和 HTTP 错误处理。现有 `get()`、`post()` 和
`post_form()` 行为保持不变，避免影响端游。

新方法负责：

- 保留顶层 `result`、`msg`、`data`、`next` 等字段。
- 允许调用方声明 `result == 100` 可解析。
- 继续把其他业务错误转换为 `MlolError`。
- 不记录请求体中的 `scene`、`battleId` 或 Cookie。

### 3.2 角色解析

公开玩家的 `scene` 来自搜索结果 `lgameIntent`。实现使用独立
`MobilePlayerSearch`，固定 `gameId=lgame`，不修改端游 `PlayerSearch` 的契约。

### 3.3 领域服务

新增 `MobileBattleService`，放在 LOL 插件的 `mlol` 边界内：

```text
list(scene, cookies, cursor=None) -> MobileBattlePage
overview(scene, cookies) -> MobilePlayerOverview
detail(battle, cookies) -> MobileBattleDetail
```

手游模型使用独立 dataclass：

```text
MobilePlayer
MobileBattle
MobileBattlePage
MobilePlayerOverview
MobileBattleDetail
```

`MobileBattle` 保存服务端 `intent`，但渲染层只能使用解析后的领域字段。

### 3.4 用例与状态

手游命令处理器放在 `plugins/lol/features/`。最近一次查询状态不能与端游当前的 `_recent`
字典混用；状态值需要带明确游戏类型，防止“手游对局 1”读取到端游列表。

手游命令统一使用 `MLOL` 前缀：`MLOL绑定`、`MLOL战绩` 和 `MLOL对局`。手游绑定与
最近查询状态均独立存储，不能复用端游的 `LolBinding` 或 `_recent` 值。玩家输入格式在
门禁 A 后根据真实搜索响应确定，但不得改变现有 `LOL绑定`、`LOL战绩` 和 `LOL对局`
的语义。

### 3.5 渲染

手游模板和端游模板分开维护。列表海报第一版只展示接口直接提供的数据：

- 英雄头像、胜负、模式、K/D/A 和对局时间。
- 主要及其他荣誉图标。
- 隐私提示和可用的战绩概览。

缺失的补刀、装备、经济、评分或队伍信息不推导。远程图片继续通过
`features/rendering/assets.py` 缓存后交给 Jinja2 和 `pytakumi`。

## 4. 实施顺序

1. 已将列表和详情响应脱敏为固定 fixture。
2. 已为 `MlolClient` 增加 envelope 读取能力及错误测试。
3. 已增加手游玩家、列表和详情 dataclass。
4. 已实现按 `gameId=lgame` 搜索的玩家解析和 `MobileBattleService`。
5. 已增加 `MLOL` 命令、独立绑定、最近查询状态和海报。
6. 已覆盖成功列表、游标、隐私字段、访客 envelope 和详情解析；其他失败响应随样本补充。
7. 已同步 `commands.toml`、插件 README、根 README、架构和特性档案状态。

## 5. 测试

单元测试使用脱敏 fixture，默认不访问掌盟：

- envelope 成功、`result == 100`、业务错误、缺字段和非 JSON。
- 角色响应的多账号、多角色、无角色和不可见角色。
- 列表首屏、翻页、`next == -1`、隐藏战绩和空列表。
- 战绩字段缺失、未知 `win_flag`、空荣誉和非法 `intent`。
- 端游与手游最近查询状态互不串用。
- 概览失败时列表海报正常降级。
- 手游列表与详情 PNG 固定 fixture 渲染。

网络验证必须显式开启，只输出响应字段名、数量和脱敏路由结构。

## 6. 交付检查

```bash
cd plugins/lol && uv run pytest
docker compose config --quiet
git diff --check
```

同时核对：

1. 处理器、`commands.toml`、插件 README 和测试中的命令完全一致。
2. 根 README 展示新增手游能力，且不混称端游数据。
3. `ARCHITECTURE.md` 仍准确描述 LOL 插件边界；若新增持久化状态则同步更新。
4. fixture 和日志不含 Cookie、Token、OAuth 回调、设备标识、真实账号或角色身份。
5. 最终手游详情协议已写回 [接口调查](api.md)，并将档案状态改为 `shipped`。

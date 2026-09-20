---
status: shipped
owner: Mimi maintainers
updated: 2026-09-20
---

# 英雄联盟手游战绩适配

> 在不影响现有端游查询链路的前提下，为 LOL 插件增加手游角色、战绩列表和单局详情能力。

## 1. 当前结论

掌盟 Android 12.8.1 客户端中，英雄联盟手游使用游戏标识 `lgame`。战绩列表是原生
JSON 接口，路径、请求参数、分页和主要响应字段均已确认。玩家搜索结果中的
`lgameIntent` 提供目标角色 `scene`，列表项中的 `intent` 指向单局详情 H5。

客户端没有声明对应的手游详情原生协议，而是把服务端返回的 `intent` 原样交给路由器。
真实 H5 脚本已确认详情数据来自 `POST /go/lgame_battle_info/detail_v2`，请求使用
`guid`、`izoneareaid` 和 `scene`，不使用端游详情参数或 `battleId`。

现有共享掌盟会话已对指定公开玩家完成只读验证，取得了搜索、概览、列表和详情成功响应。
验证过程未写入会话；文档只保留协议结构，不记录账号、角色、对局或票据标识。

## 2. 文档

- [接口调查](api.md)：记录已经确认的请求、响应和证据边界。
- [适配计划](plan.md)：定义代码边界、实施顺序、测试和交付门禁。

## 3. 设计约束

1. 复用 `MlolAuth` 和 `MlolCookies`，不建立第二套掌盟登录态。
2. 手游与端游使用不同的接口和响应模型，不在现有 `Battle`、`BattleDetail` 上堆条件分支。
3. 服务层只输出普通 `dataclass`；HTML 渲染不接触原始响应或 `intent`。
4. `intent` 只在服务层解析，渲染层不得依赖 H5 URL 或查询参数。
5. 不把估算数据标记为官方战绩，不以端游字段推导手游缺失字段。
6. 实现前将已验证响应脱敏为固定 fixture，测试不得访问真实玩家或掌盟服务。

## 4. 调用链

```text
手游玩家输入
  -> lgame 玩家/角色解析
  -> MobileBattleService.list(scene, cursor)
  -> MobileBattlePage
  -> 手游战绩海报

MobileBattle.intent
  -> 已验证的详情路由解析
  -> MobileBattleService.detail(...)
  -> MobileBattleDetail
  -> 手游详情海报
```

手游命令统一使用 `MLOL` 前缀，包括 `MLOL绑定`、`MLOL解绑`、`MLOL战绩` 和
`MLOL对局`。手游绑定与最近查询状态独立于端游；搜索时去掉输入中的 `#编号`，再按
手游角色名匹配。处理器、`commands.toml`、插件 README、根 README 和测试保持同步。

# Mimi

<p align="center">
  <img src="docs/images/mimi-avatar.png" width="160" alt="Mimi">
</p>

基于 NapCat / OneBot 的插件化 QQ 机器人。Mimi 把游戏战绩、音乐、群聊 AI、帮助目录和
消息归档拆成独立插件进程，让一个 QQ 账号同时承载多种群聊能力。

## 图片帮助

发送 `帮助` 即可查看所有功能，发送 `帮助 <插件名>` 可以只查看一个分类。帮助插件会
自动读取各插件的 `commands.toml` 生成中文指令目录。

<p align="center">
  <img src="docs/images/help-lol.png" width="720" alt="Mimi 英雄联盟指令帮助">
</p>

## 英雄联盟

通过 Riot ID 查询掌盟公开数据。普通用户不需要登录掌盟，管理员维护一个共享登录态；
用户可以绑定常用玩家，也可以直接查询其他玩家。

<table>
  <tr>
    <td width="50%">
      <img src="docs/images/lol-battle.png" alt="英雄联盟最近战绩海报">
    </td>
    <td width="50%">
      <img src="docs/images/lol-detail.png" alt="英雄联盟单局详情海报">
    </td>
  </tr>
  <tr>
    <td align="center"><b>最近战绩</b><br>段位、胜率、KDA、评分、装备、伤害与双方阵容</td>
    <td align="center"><b>单局详情</b><br>十人数据、召唤师技能、装备与海克斯强化</td>
  </tr>
</table>

- `LOL绑定 <昵称#编号>`：保存常用 Riot ID
- `LOL战绩 [昵称#编号]`：查看最近 8 局
- `LOL对局 <1-8>`：展开最近一次查询中的单局详情

## APEX 英雄

绑定或直接输入 EA ID，查看玩家生涯、常用武器、常用传奇和各模式统计；地图轮换会同时
展示当前地图、剩余时间和下一张地图。

<table>
  <tr>
    <td width="58%">
      <img src="docs/images/apex-stats.png" alt="APEX 玩家战绩海报">
    </td>
    <td width="42%">
      <img src="docs/images/apex-rotation.png" alt="APEX 地图轮换海报">
    </td>
  </tr>
  <tr>
    <td align="center"><b>玩家战绩</b><br>生涯、武器、传奇与模式统计</td>
    <td align="center"><b>地图轮换</b><br>排位与匹配地图时间线</td>
  </tr>
</table>

- `APEX绑定 <EA ID>`：保存常用玩家
- `APEX战绩 [EA ID]`：生成玩家数据海报
- `APEX轮换`：查看当前及下一张地图

## 网易云音乐

发送 `点歌 <歌曲名>[-歌手名]` 搜索歌曲，Mimi 会直接回复可播放的网易云音乐卡片。
管理员可以通过二维码建立共享登录态，群成员无需分别登录。

<p align="center">
  <img src="docs/images/music-card.png" width="100%" alt="Mimi 网易云点歌与音乐卡片">
</p>

## 更多能力

| 插件 | 使用方式 | 功能 |
| --- | --- | --- |
| 温蒂 | `@机器人 <内容>` | 在群聊中提及机器人进行 AI 对话 |
| Yasuo | 自动归档 | 保存群消息和图片，并提供分页查询 HTTP API |

## 文档入口

部署、开发和排障指引统一维护在 `docs/`，根 README 只介绍产品能力。

| 主题 | 文档 |
| --- | --- |
| 部署、配置、数据卷与回滚 | [部署规范](docs/spec/deploy-spec.md) |
| 本地环境、运行、测试与排障 | [开发指南](docs/develop-guide/README.md) |
| 设计与开发约束 | [工程规范](docs/spec/design-spec.md) |
| 系统组件与数据流 | [系统架构](ARCHITECTURE.md) |
| 功能设计与演进 | [特性档案](docs/feature/README.md) |
| 插件完整命令 | [`plugins/<name>/README.md`](plugins/lol/README.md) |

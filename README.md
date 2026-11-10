# Mimi

以 **南瓜（Pumpkin）AI 助手**为核心的 QQ 机器人：在群聊或私聊中自然对话、查资料、
浏览网页、处理图片和执行任务，也支持英雄联盟与 APEX 战绩查询、网易云点歌和消息归档。
基于 NapCat / OneBot，各项能力由独立插件提供。

## 南瓜 · AI 助手

在群里 `@南瓜` 或以“南瓜”开头提出需求，也可以直接私聊。南瓜能结合对话上下文回答，
引用它的回复即可继续追问；复杂任务会先确认，再交付结果。

- **聊天与看图**：日常问答、理解图片，按语境使用表情回应。
- **查资料与浏览网页**：搜索并整理信息、读取网页、浏览动态页面，按需交付网页截图。
- **执行任务**：运行代码、处理数据，将结果整理成图文报告或文件。
- **图片与语音**：查找现成图片；配置相应服务后，也支持图片生成和语音合成。

例如：“南瓜，帮我整理这篇文章的要点”“南瓜，把这张图里的数据整理成表格”。
发送 `南瓜退下` 或 `闭嘴` 可让当前会话休眠，再次明确呼唤即可唤醒。
任务只完成一部分或遇到限制时，会说明已完成的内容和剩余事项。

详细能力与配置见 [Pumpkin 使用说明](plugins/pumpkin/README.md)。

## 图片帮助

发送 `帮助` 即可查看所有功能，发送 `帮助 <插件名>` 可以只查看一个分类。帮助插件会
自动读取各插件的 `commands.toml` 生成中文指令目录。

<p align="center">
  <img src="docs/images/help.png" width="720" alt="Mimi 指令帮助">
</p>

## 英雄联盟

通过 Riot ID 或手游角色名查询掌盟公开数据。普通用户不需要登录掌盟，管理员维护一个
共享登录态；端游与手游使用独立绑定和最近查询状态。

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="docs/images/lol-battle.png" alt="英雄联盟最近战绩海报">
    </td>
    <td width="50%" valign="top">
      <img src="docs/images/lol-detail.png" alt="英雄联盟单局详情海报">
    </td>
  </tr>
  <tr>
    <td align="center"><b>端游最近战绩</b><br>段位、胜率、KDA、评分、装备、伤害与双方阵容</td>
    <td align="center"><b>端游单局详情</b><br>十人数据、召唤师技能、装备与海克斯强化</td>
  </tr>
</table>

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="docs/images/mlol-battle.png" alt="英雄联盟手游最近战绩海报">
    </td>
    <td width="50%" valign="top">
      <img src="docs/images/mlol-detail.png" alt="英雄联盟手游单局详情海报">
    </td>
  </tr>
  <tr>
    <td align="center"><b>手游最近战绩</b><br>段位、荣誉、装备、经济、伤害、参团率与双方阵容</td>
    <td align="center"><b>手游单局详情</b><br>十人经济、输出、承伤、参团、装备、符文与强化</td>
  </tr>
</table>

- `LOL绑定 <昵称#编号>`：保存常用 Riot ID
- `LOL战绩 [昵称#编号]`：查看最近 8 局
- `LOL对局 <1-8>`：展开最近一次查询中的单局详情
- `MLOL绑定 <手游昵称>`：保存常用手游角色
- `MLOL解绑`：清除已绑定的手游角色
- `MLOL战绩 [手游昵称]`：查看手游最近 8 局
- `MLOL对局 <1-8>`：展开最近一次手游查询中的单局详情

## APEX 英雄

绑定或直接输入 EA ID，查看玩家生涯、常用武器、常用传奇和各模式统计；地图轮换会同时
展示当前地图、剩余时间和下一张地图。

<table>
  <tr>
    <td width="58%" valign="top">
      <img src="docs/images/apex-stats.png" alt="APEX 玩家战绩海报">
    </td>
    <td width="42%" valign="top">
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
也可以对南瓜说“点歌 …”，获取同样的音乐卡片。

<p align="center">
  <img src="docs/images/music-card.png" width="100%" alt="Mimi 网易云点歌与音乐卡片">
</p>

## 更多能力

| 插件 | 使用方式 | 功能 |
| --- | --- | --- |
| Yasuo | `群撤回` / `原始消息` | 生成同群撤回海报或单独发送归档原消息 |

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

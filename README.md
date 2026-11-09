基于 NapCat / OneBot 的插件化 QQ 机器人。Mimi 把游戏战绩、音乐、帮助目录和
消息归档拆成独立插件进程，让一个 QQ 账号同时承载多种群聊能力。

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
明确对 Pumpkin 说“点歌 …”时，它会回复同一条真实点歌指令，由 Music 插件继续完成
搜索和卡片发送，不会改成语音朗读或图片任务。

<p align="center">
  <img src="docs/images/music-card.png" width="100%" alt="Mimi 网易云点歌与音乐卡片">
</p>

## 更多能力

| 插件 | 使用方式 | 功能 |
| --- | --- | --- |
| Pumpkin | `@南瓜` / `南瓜退下` | 克制参与、任务执行、动态网页浏览、表情回应与持久休眠控制 |
| Yasuo | `群撤回` / `原始消息` | 生成同群撤回海报或单独发送归档原消息 |

Pumpkin 查找现成图片时会读取相关页面、下载候选并查看预览；找不到合适资源时说明缺口，
不会自行改为图片生成。
引用南瓜的回答追问时会保留原问答作为参考；已验收的任务图片、文件和语音直接交付，
不等待聊天排队。单个网页来源失败可继续换来源，未核验的结果不会自动宣称完成。
用户纠正过的任务在完成前会独立核验实际候选内容；条件 ID 或完成声明不能代替证据。
部分完成会说明已完成的部分和剩余事项，只核验声称已满足的条件。
浏览器截图发送前会逐张核验文字可读性和任务要求，过程截图仅供诊断，不混入最终交付。
浏览器任务使用独立、无网络的 Python 程序沙箱，通过受控 SDK 连续操作页面；
程序间保留有界执行记录，并按剩余时间收敛；完成参数可纠正一次，仍需通过最终页面门禁。
SDK 保留通用 DOM 结构和局部文字，支持定位重复入口；填写结果按最终控件状态核验，
空表单标题或过程态文字不能证明填写完成。
元素引用与页面观察版本分离，填写和新观察不会使有效引用失效。
网页任务可在一次委派内保留页面继续执行，结束前自动获取最终证据；
当前只支持可逆公开交互，不支持提交。公开交互每个任务版本只执行一次，
部分完成后换链接或目标不能从头重做；填写结果通过控件状态和最终观察核验。
浏览器能力不满足明确要求时会说明缺口；失败结果保留同次执行的观察和动作效果，
不会用新开的空白页面判断旧操作结果。动作后先观察效果再判断停滞，
原交互未核验时只能报告部分完成。

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

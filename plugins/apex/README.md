# APEX

APEX 英雄玩家战绩与地图轮换插件。

## 命令

| 命令 | 说明 |
|---|---|
| `APEX绑定 <EA ID>` | 保存发送者常用的 EA ID |
| `APEX战绩 [EA ID]` | 查询玩家战绩，省略参数时使用绑定值 |
| `APEX轮换` | 查看当前及下一张轮换地图 |

## 配置

`APEX轮换` 需要 `APEX_AUTH`。在
[Apex API Portal](https://apexlegendsapi.com/auth) 注册项目并获取免费 API Key，再写入
本地 `.env`：

```dotenv
APEX_AUTH=replace-with-apex-api-key
```

该非官方 API 的初始限速为每秒 5 次请求。API Key 不得提交到 Git、日志或测试。
完整环境变量和部署规则见 [部署规范](../../docs/spec/deploy-spec.md)。

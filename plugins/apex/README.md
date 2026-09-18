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

海报图片遵循浏览器式 HTTP 缓存语义：优先使用 `Cache-Control` / `Expires` 判断新鲜度，
过期后携带 `ETag` / `Last-Modified` 验证，源站返回 `304` 时继续复用本地图片。源站没有
缓存策略时使用 `APEX_ASSET_CACHE_TTL_DAYS`（默认 7 天）作为兜底；网络异常时允许使用
已缓存的过期图片。缓存正文与元数据均通过同目录临时文件原子替换，进程中断不会破坏旧缓存。

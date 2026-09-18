---
status: shipped
owner: Mimi maintainers
updated: 2026-09-18
commits:
  - 9b57824
  - fa072ef
---

# 群消息归档

> 通过 Yasuo 插件归档群消息与图片，并提供分页查询 API。

## 1. 背景与目标

`9b57824` 首次加入 Yasuo 和插件数据卷，使群消息数据库与下载图片可跨容器重建保留。
初始实现集中在单个入口文件，随后在同日拆分职责。

## 2. Git 演进

| 提交 | 变化 |
| --- | --- |
| `9b57824` | 引入 Yasuo、FastAPI、SQLite 与 Docker 数据持久化 |
| `fa072ef` | 拆出 `api.py`、`models.py`、`tasks.py` 与应用生命周期 |

## 3. 代码实现流程

### 3.1 应用生命周期

`plugins/yasuo/main.py:create_app()` 创建 FastAPI 应用并注册 `lifespan()`。启动阶段按
`DATABASE_URL` 初始化 Tortoise，自动建立 `group_messages` 表，再创建
`NapCatSync.run()` 后台任务；关闭阶段取消任务并关闭数据库连接。

```text
uvicorn
  -> FastAPI lifespan
  -> SQLite / Tortoise
  -> NapCatSync 后台任务
  -> FastAPI POST /
```

### 3.2 消息写入

`plugins/yasuo/tasks.py:NapCatSync.start()` 直接消费 NapCat 事件，只接受
`GroupMessageEvent`。`save_message()` 的顺序是：

1. `extract_image_urls()` 遍历消息段，提取 `_type == "image"` 且包含 URL 的图片。
2. `download_images()` 按顺序下载图片，并写入
   `data/images/<group_id>/<message_id>/`。
3. `download_image()` 根据响应 `content-type` 选择扩展名，以 URL 哈希作为文件名。
4. `GroupMessage.create()` 保存消息 ID、群和用户信息、原始文本、图片路径与时间。

图片下载使用独立 `httpx.AsyncClient` 和 30 秒超时。单张图片下载异常返回 `None`，已获取
的文本及其他图片仍然入库。连接循环发生异常后等待五秒再连接 NapCat。

### 3.3 查询与响应

`plugins/yasuo/api.py:reads()` 接收 JSON 过滤条件以及 query string 中的 `page`、
`page_size`。它先构造 `GroupMessage.filter(**query)` 并统计总数，再按 `time`
倒序分页读取。每条记录由 `MessageResponse.model_validate()` 转换；存在本地图片时，
`_read_image_b64()` 异步读取并编码，无法读取的图片被跳过。

最终响应为：

```text
MessageListResponse
  -> total
  -> data[]
      -> 消息与发送者字段
      -> images_base64[]（存在可读图片时）
```

## 4. 状态与边界

- 只归档群消息，不处理私聊。
- `plugins/yasuo/data/yasuo.db` 与 `plugins/yasuo/data/images/` 由 Yasuo 独占。
- `message_id` 唯一，重复事件需要防止重复写入。
- 当前 `page_size` 没有硬上限，大结果可能放大数据库和 Base64 开销。
- Compose 默认不映射 `6273` 到宿主机。
- 数据库和图片目录必须作为同一生命周期的数据备份。

## 5. 失败行为

- NapCat 连接或事件处理异常：记录堆栈，等待五秒后重连。
- 图片 HTTP、文件创建或写入失败：记录警告，继续保存消息。
- 查询时图片文件丢失：响应保留消息，只省略该图片。
- 数据库唯一键冲突：当前会退出本轮消费并进入重连循环，修改时应重点回归重复事件。

## 6. 验证

修改归档链路时至少验证纯文本、含图消息、图片下载失败、重复消息、分页过滤和容器重启
后的数据保留。

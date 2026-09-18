---
status: shipped
owner: Mimi maintainers
updated: 2026-09-18
commits:
  - 5f31918
  - 6a48909
  - 0b061b2
  - 2cb7cd3
  - a5a43c8
---

# APEX 数据与海报

> 从地图轮换命令演进为包含 EA ID 绑定、玩家战绩和共享图片渲染的完整插件。

## 1. Git 演进

| 日期 | 提交 | 变化 |
| --- | --- | --- |
| 2026-08-04 | `5f31918` | 首次加入 APEX 插件 |
| 2026-08-07 | `6a48909` | 轮换改为排位/匹配与时间范围文本 |
| 2026-08-13 | `0b061b2` | 引入 dispatcher、Jinja2 海报、字体、资产缓存和 pytest |
| 2026-08-14 | `2cb7cd3` | 增加玩家战绩、EA ID 绑定和共享渲染模块 |
| 2026-08-20 | `a5a43c8` | 清理战绩测试桩 |

## 2. 代码实现流程

### 2.1 启动与分发

`plugins/apex/main.py` 可先通过 `--env-file` 加载 `.env.dev`，然后执行
`init_db()` 创建 `data/apex.db` 和绑定表。导入 `features` 时，
`plugins/apex/dispatcher.py:command()` 将 `APEX绑定`、`APEX战绩`、`APEX轮换`
注册到进程内命令表。

NapCat 事件只在消息恰好包含一个 `Text` 段时进入 `dispatch()`。分发器按命令长度倒序
匹配、不区分大小写，并将剩余文本留给 `argument()`。连接异常时入口等待五秒重建
`NapCatClient`，数据库连接只在进程退出时关闭。

### 2.2 绑定与战绩

`plugins/apex/features/binding.py:handle_bind()` 使用发送者 QQ 号作为主键，
`EABinding.update_or_create()` 保存 EA ID。`handle_stats()` 优先读取命令参数，未提供时
调用 `get_bound_ea_id()`：

```text
APEX战绩 [EA ID]
  -> 参数或 EABinding
  -> fetch_event_name() 获取并缓存活动 ID
  -> fetch_stats() 请求 EA 玩家数据
  -> build_stats_html() 转换中文展示上下文
  -> fetch_assets() 下载头像、传奇和段位图片
  -> render_image() 生成 PNG
  -> Base64 Image 回复
```

`build_stats_html()` 将总览、传奇、武器和模式数据整理成模板字段，并在这一层完成段位、
武器、模式和指标名称翻译。模板不读取原始 HTTP 响应，也不发起网络请求。

### 2.3 地图轮换

`plugins/apex/features/rotation.py:handle_rotation()` 读取 `APEX_AUTH`，请求 ALS
地图轮换接口。`build_rotation_html()` 只保留排位和匹配模式，把当前与下一张地图转换为
中文名称、北京时间、剩余时间和进度，再走与战绩相同的资产及 PNG 管线回复。

### 2.4 资产与渲染

`features/rendering/assets.py` 以 URL 的 SHA-256 作为缓存键，遵循 `Cache-Control`、
`Expires`、`ETag` 和 `Last-Modified`。新鲜资源直接复用，过期资源条件请求，`304` 只更新
元数据；源站未声明缓存策略时默认保留七天，请求失败时回退已有资源。缓存正文与元数据通过
同目录临时文件原子替换，进程中断不会破坏旧缓存。缺失资源使用同一个 `httpx.AsyncClient`
并发请求；单张失败只记录警告。`render.py` 在模块加载时读取字体，`render_image()` 调用
`pytakumi.html_to_pic()`。handler 通过 `asyncio.to_thread()` 执行同步渲染，避免阻塞消息
事件循环。

## 3. 设计取舍

轮换曾短暂采用纯文本输出，随后在数据和版式稳定后升级为图片。渲染基础设施先服务轮换，
再被玩家战绩复用，避免两个功能各自维护下载、字体和 PNG 逻辑。

## 4. 状态与边界

- APEX API key 由 `APEX_AUTH` 提供。
- 绑定只保存查询偏好，不保存 EA 凭据。
- 远程图片必须经过资产层，模板不发网络请求。
- 单张资产失败应局部降级，不使插件退出。

## 5. 失败行为

- 未配置 `APEX_AUTH`：轮换命令只记录配置警告，不调用上游。
- 玩家参数和绑定都缺失：回复明确用法。
- EA 请求异常或无玩家摘要：分别回复查询失败或未找到玩家。
- 轮换响应没有可展示模式：回复未获取到轮换信息。
- 图片资产失败：继续使用可用资产渲染；HTML 或 PNG 失败由插件连接循环记录。

## 6. 验证

修改时覆盖 dispatcher、绑定 CRUD、轮换转换、战绩 fixture、资产失败和 PNG 输出。
图片测试与预览必须将最终文件写入 `plugins/apex/tests/output/`。

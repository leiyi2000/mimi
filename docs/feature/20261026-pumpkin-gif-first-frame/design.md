---
status: implemented
owner: Mimi maintainers
updated: 2026-10-26
commits: []
---

# Pumpkin GIF 首帧读取

## 1. 背景

Pumpkin 通过 `MessageMediaProvider` 按需下载 QQ 消息中的图片和扩展表情，再由
`inspect_message_media` 把完整图片编码为 Base64 交给多模态模型。当前 GIF 会保留全部
动画帧。动态表情即使画面尺寸较小，也可能因为帧数较多而占用较大的模型请求体，并触发
模型端的图片格式或请求大小限制。

媒体下载已有独立的字节上限。该上限用于限制外部响应和进程内存占用，不能通过图片解码
绕过。本特性只改变已成功下载 GIF 的模型输入表示，不放宽下载安全边界。

## 2. 目标

- GIF 图片在交给模型前只保留第一帧。
- 第一帧统一编码为 PNG，向模型报告 `image/png`。
- PNG、JPEG 和 WebP 保持当前行为，不做无关转码。
- 解码失败、像素数量异常或转换失败时返回明确错误，不把原始 GIF 静默回退给模型。
- 不改变 QQ 消息归档、媒体引用解析、任务权限和模型协议。

## 3. 处理边界

首帧归一化由 `providers/media.py` 的 `MessageMediaProvider` 负责。Provider 已拥有下载
字节、内容类型识别和媒体安全边界，因此转换不进入 Adapter、聊天工具或模型 Provider。

目标链路：

```text
NapCat image / mface segment
  -> MessageMediaProvider.fetch
  -> URL validation and bounded download
  -> content type validation
  -> GIF first-frame decode
  -> RGBA/RGB conversion
  -> PNG encoding
  -> FetchedMedia(image/png)
  -> inspect_message_media
  -> Base64 image input
  -> multimodal model
```

处理顺序：

1. 继续使用 `PUMPKIN_MEDIA_MAX_BYTES` 检查响应头和流式下载累计字节。
2. 仅当已识别内容类型为 `image/gif` 时进入图片解码。
3. 使用 Pillow 打开内存字节流，只读取索引为 `0` 的帧。
4. 在完整解码前校验宽高和总像素数量，拒绝解压炸弹或异常尺寸。
5. 保留透明通道；无透明通道时输出 RGB，最终编码为 PNG。
6. 返回转换后的字节和 `image/png`，`source` 仍保留原媒体来源。

Pillow 必须作为 Pumpkin 的直接依赖声明，不能依赖其他渲染库间接安装。

## 4. 失败行为

新增或复用以下明确错误：

| 错误码 | 条件 | 用户可见含义 |
| --- | --- | --- |
| `MEDIA_TOO_LARGE` | 原始响应超过下载上限 | 媒体超过允许的大小限制 |
| `MEDIA_CONTENT_UNSUPPORTED` | GIF 无法识别或首帧无法解码 | 媒体格式暂不支持查看 |
| `MEDIA_PIXEL_LIMIT_EXCEEDED` | 首帧宽高或总像素超过安全上限 | 图片尺寸超过允许的范围 |
| `MEDIA_CONVERSION_FAILED` | 首帧 PNG 编码失败 | 图片处理失败，请稍后重试 |

解码和编码在工作线程执行，避免 CPU 密集型图片处理阻塞异步事件循环。错误不得携带媒体
内容、URL 查询参数或底层图片库异常详情进入用户回复和日志。

## 5. 非目标

- 不提取代表帧、关键帧或动画中间帧。
- 不分析 GIF 动画语义。
- 不对静态 WebP、PNG 或 JPEG 做缩放和压缩。
- 不提高 `PUMPKIN_MEDIA_MAX_BYTES`，也不允许先下载超限 GIF 再压缩。
- 不改变网页图片下载、图片生成 Artifact 或 HTML/Markdown 渲染链路。

## 6. 验收标准

1. 普通 GIF 和扩展表情 GIF 返回第一帧 PNG，模型输入内容类型为 `image/png`。
2. 多帧 GIF 的输出只包含一帧，透明像素正确保留。
3. PNG、JPEG 和静态 WebP 字节及内容类型保持不变。
4. 超过下载上限的 GIF 仍返回 `MEDIA_TOO_LARGE`，不会进入解码。
5. 损坏 GIF、异常像素尺寸和编码失败分别返回稳定错误码。
6. 图片转换不阻塞事件循环，并且不记录原始媒体内容。
7. `plugins/pumpkin` 全量测试和 `git diff --check` 通过。

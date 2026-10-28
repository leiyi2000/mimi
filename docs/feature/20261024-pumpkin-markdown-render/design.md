---
status: implemented
owner: Mimi maintainers
updated: 2026-10-24
commits: []
---

# Pumpkin Markdown 图片渲染

## 1. 目标

为 Task Executor 提供 `markdown-render` Skill，把结构化 Markdown 渲染为经过验证的 PNG
Artifact。适合旅行攻略、调研摘要、清单、表格和代码说明等内容；需要自由布局时继续使用
`html-render`。

## 2. 边界

- 使用 `pytakumi.md_to_pic` 和 `markdown-it-py`，支持标题、引用、列表、表格、代码块、
  强调、删除线和普通链接。
- 加载 Pumpkin 的 CJK 与 Emoji 字体，默认使用深色主题。
- Markdown 原始 HTML 不执行；脚本、iframe 和事件属性不会进入渲染树。
- 普通 HTTP(S) 链接可以作为可见来源保留，但图片只接受当前任务和 revision 下的
  `artifact://<artifact_id>`。
- 公网图片先由 `fetch_url(output=artifact)` 下载并验证，再把返回的 Artifact ID
  写入 Markdown。
- 单次最多引用 8 张图片，Markdown 长度、输出宽度和设备像素比均有确定性上限。

## 3. 调用链

```text
Task Executor
  -> load_skill(markdown-render)
  -> render_markdown(markdown, name, theme, width, device_pixel_ratio)
       -> MarkdownRenderer validates image references
       -> ArtifactStore resolves current revision image bytes
       -> pytakumi.md_to_pic
       -> ArtifactService.write(image)
  -> complete_task(artifact_ids)
```

图片来自公开 URL 时：

```text
fetch_url(url, output=artifact)
  -> image Artifact
  -> ![说明](artifact://<artifact_id>)
  -> render_markdown
```

## 4. 失败行为

- 空 Markdown、内容过长、非法宽度或设备像素比：`MARKDOWN_RENDER_INVALID`。
- 外链、本地路径或非 Artifact 图片：`MARKDOWN_RENDER_INVALID`。
- Artifact 缺失、跨任务、跨 revision、非图片或路径越界：`MARKDOWN_ASSET_INVALID` 或
  `MARKDOWN_RENDER_FAILED`。
- 渲染和 Artifact 写入失败必须显式返回错误，不产生完成状态。

## 5. 验收标准

1. 中文标题、列表、引用、GFM 表格和代码块可渲染为 PNG。
2. 当前 revision 的图片 Artifact 可通过 Markdown 图片语法显示。
3. HTTP(S) 图片 URL 和跨作用域 Artifact 被拒绝。
4. 工具结果登记经过验证的图片 Artifact，并保留 Markdown 摘要与资源来源指纹。
5. `markdown-render` 只对 Task Executor 可见。
6. Pumpkin 全量测试与 `git diff --check` 通过。

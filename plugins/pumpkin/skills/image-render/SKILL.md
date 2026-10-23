---
name: image-render
description: 使用已配置的图片模型生成图片 Artifact。
allowed-tools:
  - generate_image
agent-roles:
  - task
load-policy: on-demand
triggers:
  - 生成图片
  - 画图
  - 图片渲染
  - 参考图生图
examples:
  - 生成一张插画
  - 按描述渲染图片
anti-triggers:
  - 发送已有图片
priority: 60
---

图片生成只能作为当前任务的已验证 Artifact 交付。先确认提示词、模型和画幅，再调用工具；
如果使用人物主体参考图，只能传入 HTTPS 图片地址，不能传入本地路径或任意文件路径。工具支持
`image-01` / `image-01-live`、比例、像素尺寸、数量、Prompt 优化和水印参数；`style` 只对
`image-01-live` 生效。每个返回的 Artifact ID 都可以用于任务交付。模型服务未配置、返回非图片
内容、下载失败或参考图参数非法时必须明确报告失败，不得使用外部 URL 冒充已生成图片。
同一次逻辑生成必须复用稳定的 `idempotency_key`；超时、恢复或重复工具轮次不得换键。
只有用户明确要求重新生成，或提示词、参考图与参数发生实质变化时才使用新键。

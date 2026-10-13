---
name: image
description: 根据文字提示生成一张图片；用户要求画图、生成图片或制作视觉内容时使用。
allowed-tools: generate_image
---

# Image

将用户要求整理成具体提示词并调用 `generate_image`。只有工具返回 `IMAGE_GENERATED` 后
才能确认成功。内容安全拒绝、账号无权限和下载失败必须如实说明。

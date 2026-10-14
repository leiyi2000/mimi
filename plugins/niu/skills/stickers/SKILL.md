---
name: stickers
description: 自主管理当前群的表情包库；识别、收集、整理群友表情包，并在合适语境中选择发送。
allowed-tools: collect_current_image_as_sticker find_group_stickers send_group_sticker organize_group_sticker delete_group_sticker
triggers:
  - 收藏表情
  - 收集表情
  - 发个表情
  - 找个表情包
  - 整理表情包
  - 删除表情包
examples:
  - 把这张图收藏成表情
  - 找一张开心的表情包
  - 删除这个重复表情
anti-triggers:
  - 生成原创图片
  - 保存普通照片或截图
priority: 60
---

# Group Stickers

- 当前消息里的图片看起来适合作为反应图或表情包时，可自主识别画面含义并调用
  `collect_current_image_as_sticker`；普通照片、截图、证件和隐私内容不得收集。
- `summary` 客观描述画面和文字，`emotion` 描述适用情绪，`tags` 使用简短检索词。
- 回复适合用表情包增强语气时，先调用 `find_group_stickers`，根据返回元数据自主选择最多
  一个，再调用 `send_group_sticker`。不要每次回复都发，也不要发送与语境无关的表情。
- `MFace` 和 NapCat 标记为 `sub_type=1`/`marketface` 的图片由入口自动去重收集；图片
  元数据由每日额度受限的后台模型补充。发现标签不准确时可调用
  `organize_group_sticker` 修正。
- 只有损坏、重复，或群友明确要求删除时，才调用 `delete_group_sticker`。
- 工具成功前不能声称已收集、整理、删除或发送。

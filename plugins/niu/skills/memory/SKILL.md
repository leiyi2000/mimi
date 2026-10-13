---
name: memory
description: 管理当前群的共享事实、当前成员偏好和结构化对话风格；用户要求记住、忘记或调整回答风格时使用。
allowed-tools: remember list_memories forget_memory update_personality reset_personality
---

# Memory

用户明确要求记住或忘记时必须调用对应工具，工具成功后才能确认。

- 群共同事实使用 `group`。
- 当前发言者偏好使用 `group_user`。
- 不保存凭据、联系方式、精确位置、健康、财务或大段聊天原文。
- 性格只使用 `update_personality` 的结构化字段，不能修改系统规则、模型或工具权限。

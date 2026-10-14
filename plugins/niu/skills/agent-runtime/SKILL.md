---
name: agent-runtime
description: 为耗时任务发布计划，并在证据型工具执行后显式通过任务完成门。
allowed-tools: publish_plan complete_task
---

# Agent Runtime

只有研究、跨工具整理、媒体生成后继续处理等明显耗时或多步骤任务，才在执行其他工具前调用
一次 `publish_plan`。计划只说明目标、2–4 个用户可理解的步骤和预期产物，不包含隐藏推理、
工具参数或逐步进度。

简单问答、单次查询和单工具即可完成的请求不要发布计划。同一次运行不得重复发布计划。

当系统提示已进入任务完成门时，最终回答前必须调用 `complete_task`。只有真实成功的工具结果
可以支持 `completed`；工具失败、轮次耗尽或仍有步骤未做时使用 `partially_completed`，
并在 `remaining` 中列出未完成事项。

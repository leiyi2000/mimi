---
status: implemented
owner: Mimi maintainers
updated: 2026-10-23
commits: []
---

# Pumpkin 任务交付与纠正门禁

## 1. 背景

Task Executor 生成 Artifact 后会先发出 `task.artifact_ready` 进度事件，完成验收后再通过
`complete_task` 把任务迁移到 `completed` 或 `partially_completed`。此前 Conversation
Agent 在候选事件轮次也拥有交付能力，提前调用 `send_task_delivery` 时，
`not_delivery_ready` 又会被计为用户纠正违规。若同一轮的完成调用遗漏自然语言验收条件，
两次无关错误会被累计为重复纠正违规并直接终止任务。

本特性将进度通知、终态交付、验收身份和纠正违规计数拆成独立边界。

## 2. 终态交付

`ConversationTurnPolicy` 只对 `task.completed` 且状态为 `completed` 或
`partially_completed` 的 TaskEvent 授予 `TASK_DELIVERY` capability。
`task.artifact_ready`、等待、进度和失败事件只能读取或纠正任务，模型看不到
`send_task_delivery`。

`DeliveryService.send` 仍执行第二层确定性校验：任务必须属于当前 scope、revision 一致，
并且数据库状态已经终结。非终态请求返回 `TASK_NOT_TERMINAL`，即使 Artifact 被意外标记为
`delivery_ready` 也不会进入 Outbox。

Artifact 门禁先检查交付就绪状态，再检查用户纠正。候选 Artifact 返回独立的
`ARTIFACT_NOT_DELIVERY_READY`，不会写入纠正违规计数。

## 3. 稳定验收条件

每条纠正验收条件使用以下稳定 ID：

```text
<correction_id>:criterion:<1-based-index>
```

`correction_payload` 同时注入：

- `acceptance_criteria`：原有文本列表，供旧调用兼容；
- `acceptance_criteria_refs`：包含 `criterion_id` 和 `text` 的结构化列表。

Task Agent 调用 `complete_task` 时优先提交 `criterion_id`。验收门禁仍接受完全相同的旧
文本，保证已有 Session Event 和旧模型调用可恢复。Runtime 的 Artifact fallback 也从
Task 上下文提取全部稳定 ID，不再自行编造验收文案。

## 4. 违规计数

`pumpkin_correction_violations` 按以下三元组持久化次数：

```text
(correction_id, revision, reason)
```

内容、来源、策略、拒绝状态和验收条件分别形成稳定原因。只有同一纠正、同一 revision、
同一原因重复两次才触发 `CORRECTION_REPEAT_LIMIT`。新 revision 或不同原因从一次重新
开始，不会被历史错误污染。

`pumpkin_task_corrections.violation_count` 保留为各作用域的最大次数，用于旧载荷和状态
展示，不参与重复违规判定。新表由 Tortoise 的安全 schema 初始化创建，不修改现有运行
数据列。

## 5. 调用链

```text
Task Executor
  -> ArtifactService.write
  -> task.artifact_ready
  -> ConversationTurnPolicy (no delivery capability)
  -> complete_task
       -> stable criterion gate
       -> scoped correction gate
       -> mark delivery_ready + terminal task state (one transaction)
  -> task.completed
  -> ConversationTurnPolicy (delivery capability)
  -> DeliveryService terminal check
  -> Artifact delivery-ready gate
  -> Outbox
```

## 6. 验收标准

1. 候选 Artifact 提前交付不会增加纠正违规次数。
2. 非终态任务无法交付，即使 Artifact 状态异常。
3. 终态事件才向 Conversation Agent 暴露交付工具。
4. `criterion_id` 和旧验收文本均可通过完成门禁。
5. 不同 revision 或不同原因不共享重复违规次数。
6. 同一作用域内第二次真实违规仍会停止任务。
7. Pumpkin 全量测试、Ruff、compileall 和 `git diff --check` 通过。

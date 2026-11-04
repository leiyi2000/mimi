---
status: implemented
owner: Mimi maintainers
updated: 2026-10-29
commits: []
---

# Pumpkin 对话与任务运行时边界

## 1. 背景

线上私聊和群聊记录暴露出四类边界问题：

1. 纯称呼消息没有回复 capability，但 `load_skill` 仍被 AgentLoop 强制暴露，模型可能进行
   无意义搜索并耗尽步骤；
2. 私聊可以使用搜索等执行能力，却不能创建 Task，也不能通过聊天回复工具交付图片；
3. 普通回复工具的 schema 没有限定消息段结构，模型可能把 CQ 媒体码放进正文，长正文又被
   渲染为文字图片；
4. Conversation Agent 默认 36 步远高于聊天和任务分流所需，单个异常 Turn 会阻塞同一
   scope 后续消息。

本次只修正 Runtime capability、Task scope、回复契约和执行预算，不通过中文关键词或
特定用户、群号建立旁路。

## 2. Turn capability

纯称呼消息是合法的直接聊天输入，应具备：

- `CHAT_REPLY`；
- `send_napcat_reply`、`send_napcat_replies` 和 `chat_turn_complete`。

它不具备任务、任务查询、任务纠正和表情能力，也不允许调用 `load_skill`。是否允许加载
Skill 是 Turn loadout 的显式字段；AgentLoop 不再无条件加入 `load_skill`。Task Event
同样只使用事件类型授予的固定能力，不加载额外 Skill。

普通用户消息仍可加载当前角色授权的 Skill。

## 3. 私聊 Task

Task 的权威隔离键是 `scope_id`，支持 `group:<id>` 和 `private:<id>`。私聊 Task 复用现有
TaskService、TaskRuntime、Artifact、TaskEvent、Delivery 和 Outbox 链路：

```text
private message
  -> Conversation Agent
  -> create_task(private scope)
  -> Task Executor
  -> task.completed
  -> Conversation Agent
  -> send_task_delivery(private scope)
```

不增加私聊专用执行器或媒体旁路。Task 创建仍要求源消息恰好是当前 Inbox 消息；查询、
更新、Artifact 和交付仍校验同一 scope、task 和 revision。群聊交付才 @ 请求人，私聊
直接发送。

## 4. 回复与媒体契约

`send_napcat_reply` 只接受三种普通聊天段：

| 类型 | 必需字段 |
| --- | --- |
| `text` | `text` |
| `at` | `qq` |
| `face` | `id` |

schema 明确限制段类型，Handler 再执行条件字段校验。正文和 `text` 段禁止包含 CQ 控制码。
图片、文件、语音和扩展表情不能通过模型拼接字符串或任意 segment 发送：

- 普通表情使用受控 Sticker 工具；
- 任务图片、文件和语音必须形成当前 revision 的已验证 Artifact；
- 最终媒体只通过 `send_task_delivery` 交付。

普通 URL 仍可作为文本发送；超过 80 字的真实文本仍按既有规则渲染为图片。

## 5. 错误止损与步骤预算

Conversation Agent 默认最多 10 步，Task Executor 继续保持 16 步和最后 2 步收尾预算。
Task Event 保持独立的 6 步上限。

不可重试工具错误保留一次模型纠错机会。同一错误码、工具和参数指纹跨两个 Step 重复时，
Runtime 立即终止 Turn；同一 Step 的并行重复只计一次。角色、capability、loadout 和预算
错误按错误码与工具聚合，不能通过改写无关参数绕过。这样既避免错误循环，也允许模型修正
消息 ID、合法 segment 或完成工具参数。

纯称呼和私聊 Task 的 capability 修复会在错误发生前移除主要无效调用来源，不依赖放大
步骤预算。

## 6. 上下文边界

普通消息默认不注入活跃任务索引和历史任务输出。只有结构化 `task_related` 消息和
Task Event 使用 related task context。纯称呼始终保持 `active_task_index=[]` 且不读取
任务工具。

本次不按“失败”“重试”等词判断任务关联，也不自动把普通追问改写为任务重跑。

## 7. 验收标准

1. 纯称呼可正常回复并结束 Turn，只看到聊天回复与完成工具，不看到 `load_skill` 或任务工具。
2. 私聊请求可创建 Task、异步执行并通过原私聊 scope 交付结果。
3. `send_napcat_reply` schema 限制段类型，Handler 拒绝 CQ 控制码和伪造媒体段。
4. 普通 URL 和合法文本段行为保持不变，长文本仍可渲染为 PNG。
5. Conversation 默认 10 步，Task 默认 16 步。
6. 同一不可重试错误跨两个 Step 重复后终止，不继续消耗剩余步骤。
7. Pumpkin 全量测试、Ruff、compileall 和根目录 `git diff --check` 通过。

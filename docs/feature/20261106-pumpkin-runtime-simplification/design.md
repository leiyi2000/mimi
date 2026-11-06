---
status: implemented
owner: Mimi maintainers
updated: 2026-11-06
commits: []
---

# Pumpkin 对话和任务可靠性优化

## 目标与边界

对话模型负责内容与行动选择，Runtime 负责发送确认、幂等、话题状态和交付。
保留持久化 Inbox/Outbox、任务 revision、取消恢复与执行隔离，不引入新的规划 Agent，
不按用户关键词修补行为，也不改变模型推理参数。

## 对话入口与结束

NapCat 经 MessageIngress 归档、判定处理资格并写入 Conversation Inbox。
增加 `reply_and_complete`，一次提交文本、回复模式与 continuation；ChatToolService
复用原有发送校验、Outbox 和回复台账，成功后由 Runtime 结束话题和 Turn。
旧发送和完成工具暂时兼容。Conversation Agent 返回普通正文时，AgentLoop 将正文
转换为同一受控回复工具；空正文且没有已发送回复属于协议失败，不能静默标记完成。
TaskEvent 和 Task Executor 不使用正文自动发送路径。

## 对话关联

明确唤醒仍创建新的处理话题，避免授予历史消息权限。回复助手消息时，通过同 scope
的已确认 Outbox 及 Session Event 找到被回复 Turn 的 topic_id，仅将有限历史作为
参考上下文投影。关联不恢复旧工具调用、任务权限或 continuation；无引用的新唤醒
仍隔离旧话题。

## 任务错误与候选产物

ToolResult 用强类型错误范围区分资源失败和操作失败。网页抓取、下载的资源失败只
淘汰该 URL；Turn 内复用失败结果，不能重复耗费请求，也不因重复同源失败终止整项任务。
权限、协议和状态错误继续有界止损。失败或预算耗尽时保留已生成候选文件，但有未核验
纠正条件时，不自动复制条件 ID 宣称验收通过。候选保留与交付许可分别管理。

## 终态交付

已完成任务的 Artifact、作用域、revision 和接收人由 Runtime 确定。终态通知直接
复用 DeliveryService，经过 Artifact gate 和 Outbox；无需等待同群 Conversation
Agent 再选文件。无产物的文本结果和等待、失败、暂停继续由 Conversation Agent 表达。
直接交付使用事件级幂等键，恢复查询跳过已有发送确认的 Outbox；发送失败或结果未知时不
伪装成功，复用同一 Outbox 终态而不重新发送。尚未发布失败通知的事件仍进入恢复查询，
以固定 Inbox ID 发布一次必要失败通知。

## 验证

回归覆盖普通正文、单次回复结束、空输出、已发送后正文、引用历史隔离、不同群引用、
资源失败后换来源、权限错误止损、未核验纠正条件、终态交付与恢复去重。
执行 Pumpkin 全量测试及相邻 Music/Help 测试；默认不调用真实模型、QQ 或公网。
模型行为质量还需独立 live evaluation，协议回归不能替代模型能力评估。

`tests/test_runtime_simplification.py` 覆盖单轮正文回复、合法工具事件配对、空输出和
CQ 控制码拒绝、引用隔离、错误范围、未核验纠正条件、终态交付及恢复去重。直接交付
集成测试使用本地生成的 PNG、真实 Artifact gate 和 Outbox，以发送确认和通知次数验收，
不以模型声明作为成功证据。此变更不新增聊天命令或调整模型参数，命令清单保持一致。

离线验证：Pumpkin 执行 `uv run pytest -q`，331 项通过；相邻 Music 和 Help 执行同一
命令，分别 4 项与 10 项通过。修改文件的 `uv run ruff check`、`git diff --check` 与
`docker compose config --quiet` 通过。未重建镜像、未部署服务、未执行真实模型或 QQ
评测；这些回归证明运行时契约，不代表模型语义判断已达到目标。

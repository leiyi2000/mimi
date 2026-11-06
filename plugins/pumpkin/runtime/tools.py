from __future__ import annotations

from typing import Any, Protocol

from core.domain import AgentRole, ToolContext, ToolResult, TurnCapability
from core.tools import TraceCallback, Tool, ToolRegistry
from runtime.agent_commands import MUSIC_QUERY_MAX_CHARS
from runtime.payloads import TASK_ACKNOWLEDGEMENT_MAX_CHARS


class ChatToolHost(Protocol):
    async def reply_and_complete(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def search_messages(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def inspect_message_media(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def send_chat_reply(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def send_chat_replies(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def chat_turn_complete(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...


class StickerToolHost(Protocol):
    async def search_stickers(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def send_sticker(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def remove_quoted_sticker(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...


class AgentCommandToolHost(Protocol):
    async def send_music_command(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...


class TaskToolHost(Protocol):
    async def create_task(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def get_task_status(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def update_task(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def submit_task_correction(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def send_task_delivery(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def update_task_description(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def emit_task_event(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def complete_task(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def task_turn_complete(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...


def build_registry(
    chat: ChatToolHost,
    task: TaskToolHost | None = None,
    *,
    sticker: StickerToolHost | None = None,
    agent_command: AgentCommandToolHost | None = None,
    trace: TraceCallback | None = None,
    registry: ToolRegistry | None = None,
) -> ToolRegistry:
    if task is None:
        task = chat  # type: ignore[assignment]
    if sticker is None:
        sticker = chat  # type: ignore[assignment]
    registry = registry or ToolRegistry(trace=trace)

    def register(
        name: str,
        description: str,
        schema: dict[str, Any],
        handler,
        *,
        skill_name: str | None = None,
        read_only: bool = True,
        timeout: float = 60,
        idempotency_required: bool = False,
        allowed_roles: frozenset[AgentRole] = frozenset(AgentRole),
        required_capabilities: frozenset[TurnCapability] = frozenset(),
    ) -> None:
        registry.register(
            Tool(
                name=name,
                description=description,
                input_schema=schema,
                handler=handler,
                skill_name=skill_name,
                read_only=read_only,
                timeout=timeout,
                idempotency_required=idempotency_required,
                allowed_roles=allowed_roles,
                required_capabilities=required_capabilities,
            )
        )

    register(
        "load_skill",
        "按名称加载当前角色已授权的 Skill。",
        {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
        _unsupported,
        allowed_roles=frozenset(AgentRole),
    )
    register(
        "reply_and_complete",
        "发送文本并结束聊天，Runtime 自动确认消息 ID。优先用于普通回答；"
        "只有明确追问才保留 continuation，其余使用 closed。",
        {
            "type": "object",
            "required": ["text", "response_mode", "continuation"],
            "properties": {
                "text": {"type": "string"},
                "response_mode": {
                    "type": "string",
                    "enum": ["casual_chat", "professional_answer"],
                },
                "continuation": {
                    "type": "string",
                    "enum": ["closed", "requester_input", "group_open"],
                },
            },
        },
        chat.reply_and_complete,
        read_only=False,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.CHAT_REPLY}),
    )
    register(
        "chat_turn_complete",
        "结束当前聊天轮次。普通回答、闲聊、沉默、拒绝和失败使用 closed；只有回复中"
        "明确等待当前请求人补充时使用 requester_input，明确邀请群成员参与时使用 "
        "group_open。sent_message_ids 必须按发送顺序完整"
        "复制当前轮所有发送工具返回的 message_ids，不得包含历史消息 ID。工具返回"
        "SEND_NOT_CONFIRMED 时，根据结果中的 confirmed_message_ids 修正后重试。"
        "发送简短拒绝时 outcome 仍使用 replied；主动保持安静且未发送消息时使用 ignored。",
        {
            "type": "object",
            "required": ["outcome", "sent_message_ids", "continuation"],
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["replied", "ignored", "failed"],
                },
                "sent_message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "continuation": {
                    "type": "string",
                    "enum": ["closed", "requester_input", "group_open"],
                },
                "reason": {"type": "string"},
            },
        },
        chat.chat_turn_complete,
        read_only=False,
        allowed_roles=frozenset({AgentRole.CHAT}),
    )
    register(
        "task_turn_complete",
        "结束当前任务轮次。缺少完成目标所必需的输入时使用 waiting_for_input；确认无法"
        "继续时使用 failed。已满足验收条件时改用 complete_task。",
        {
            "type": "object",
            "required": ["outcome", "task_id", "revision", "reason"],
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["waiting_for_input", "failed"],
                },
                "task_id": {"type": "string"},
                "revision": {"type": "integer"},
                "reason": {"type": "string"},
            },
        },
        task.task_turn_complete,
        read_only=False,
        allowed_roles=frozenset({AgentRole.TASK}),
    )
    register(
        "update_task_description",
        "在任务目标、参与人或已确认执行结果发生实质变化时更新任务说明投影；不要把计划"
        "或未经工具验证的结果写入说明。",
        {
            "type": "object",
            "required": [
                "involved_people",
                "goal",
                "execution_result",
                "idempotency_key",
            ],
            "properties": {
                "involved_people": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["participant_ref"],
                        "properties": {
                            "participant_ref": {"type": "string"},
                            "role": {"type": "string"},
                            "involvement": {"type": "string"},
                        },
                    },
                },
                "goal": {"type": "string"},
                "execution_result": {
                    "type": "object",
                    "required": ["status", "summary"],
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": [
                                "not_started",
                                "in_progress",
                                "waiting_for_input",
                                "completed",
                                "partially_completed",
                                "failed",
                            ],
                        },
                        "summary": {"type": "string"},
                        "artifact_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "delivery_message_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "idempotency_key": {"type": "string"},
            },
        },
        task.update_task_description,
        read_only=False,
        allowed_roles=frozenset({AgentRole.TASK}),
    )
    register(
        "emit_task_event",
        "记录当前任务中已发生的实质事件。不要用进度事件代替计划，也不要发送已由"
        "complete_task 或 task_turn_complete 表达的终态。",
        {
            "type": "object",
            "required": [
                "event_type",
                "task_id",
                "revision",
                "summary",
                "idempotency_key",
            ],
            "properties": {
                "event_type": {
                    "type": "string",
                    "enum": [
                        "started",
                        "progress",
                        "waiting_for_input",
                        "artifact_ready",
                        "failed",
                    ],
                },
                "task_id": {"type": "string"},
                "revision": {"type": "integer"},
                "summary": {"type": "string"},
                "waiting_reason": {"type": "string"},
                "artifacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["artifact_id"],
                        "properties": {"artifact_id": {"type": "string"}},
                    },
                },
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["evidence_id", "kind", "summary"],
                        "properties": {
                            "evidence_id": {"type": "string"},
                            "kind": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                    },
                },
                "idempotency_key": {"type": "string"},
            },
        },
        task.emit_task_event,
        read_only=False,
        allowed_roles=frozenset({AgentRole.TASK}),
    )
    register(
        "complete_task",
        "在验收条件已有可验证证据时完成或部分完成当前任务。artifact_ids 只引用当前任务"
        "工具返回的 Artifact ID；accepted_criteria 使用 task_corrections 中"
        " acceptance_criteria_refs 的 criterion_id，旧版验收条件原文仍兼容；"
        "remaining 说明部分完成后仍未完成的事项。",
        {
            "type": "object",
            "required": [
                "status",
                "summary",
                "remaining",
                "accepted_criteria",
                "idempotency_key",
            ],
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["completed", "partially_completed"],
                },
                "summary": {"type": "string"},
                "remaining": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "accepted_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "artifact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "idempotency_key": {"type": "string"},
            },
        },
        task.complete_task,
        read_only=False,
        allowed_roles=frozenset({AgentRole.TASK}),
    )
    register(
        "search_group_messages",
        "查询当前作用域内有限的已归档消息。",
        {
            "type": "object",
            "properties": {
                "cursor": {"type": "integer"},
                "keywords": {"type": "array"},
                "sender_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        chat.search_messages,
        skill_name="group-history",
    )
    register(
        "inspect_message_media",
        "按需查看当前作用域消息中的图片、文件或语音媒体。普通媒体使用 segment_index；"
        "引用消息内的媒体使用 segment_path，例如 [0, 1] 表示顶层第 0 个引用段、"
        "引用内容的第 1 段；合并转发使用 [0, 1, 0] 表示顶层第 0 段、"
        "第 1 个转发节点、第 0 个节点内段；链接失效或格式不支持时返回明确错误。",
        {
            "type": "object",
            "required": ["message_id"],
            "properties": {
                "message_id": {"type": "string"},
                "segment_index": {"type": "integer"},
                "segment_path": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1,
                },
            },
        },
        chat.inspect_message_media,
        skill_name="media-inspection",
        timeout=30,
        allowed_roles=frozenset({AgentRole.CHAT, AgentRole.TASK}),
    )
    sticker_categories = [
        "greeting",
        "agreement",
        "celebration",
        "amusement",
        "affection",
        "comfort",
        "surprise",
        "confusion",
        "embarrassment",
        "sadness",
        "anger",
        "disagreement",
        "teasing",
        "silence",
        "other",
    ]
    register(
        "search_stickers",
        "按语义分类查询可发送的活跃表情包，只返回受控 sticker_id 和语义元数据。",
        {
            "type": "object",
            "required": ["category"],
            "properties": {
                "category": {"type": "string", "enum": sticker_categories},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 8,
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 8},
            },
        },
        sticker.search_stickers,
        skill_name="sticker-reply",
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.STICKER_REPLY}),
    )
    register(
        "send_sticker",
        "发送 search_stickers 返回的一个活跃 sticker_id。每个聊天 Turn 最多发送一个，"
        "并计入当前回复条数预算。",
        {
            "type": "object",
            "required": ["sticker_id", "idempotency_key"],
            "properties": {
                "sticker_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
        sticker.send_sticker,
        skill_name="sticker-reply",
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.STICKER_REPLY}),
    )
    register(
        "remove_quoted_sticker",
        "移除当前消息所引用的单个表情包。群内任何用户都可请求；目标完全由 Runtime "
        "解析，不接受 sticker_id 或消息 ID。",
        {
            "type": "object",
            "required": ["idempotency_key"],
            "properties": {
                "idempotency_key": {"type": "string"},
            },
        },
        sticker.remove_quoted_sticker,
        skill_name="sticker-reply",
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.STICKER_REPLY}),
    )
    register(
        "send_napcat_reply",
        "发送当前作用域的普通聊天回复；群聊普通回复不引用消息，正文超过 80 个字符时 "
        "Runtime 会自动渲染为图片。总发送条数不得超过动态上下文中的 "
        "max_reply_messages。必须声明回复模式：日常交流使用 casual_chat，明确要求分析、"
        "解释、建议或专业资料时使用 professional_answer，任务事件状态使用 task_status。",
        {
            "type": "object",
            "required": ["response_mode", "idempotency_key"],
            "properties": {
                "response_mode": {
                    "type": "string",
                    "enum": [
                        "casual_chat",
                        "professional_answer",
                        "task_status",
                    ],
                },
                "text": {"type": "string"},
                "segments": {
                    "type": "array",
                    "maxItems": 12,
                    "items": {
                        "type": "object",
                        "required": ["type"],
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["text", "at", "face"],
                            },
                            "text": {"type": "string"},
                            "qq": {"type": "string"},
                            "id": {"type": "string"},
                        },
                    },
                },
                "mention_user_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
        chat.send_chat_reply,
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.CHAT_REPLY}),
    )
    register(
        "send_music_command",
        "向当前会话代发 Music 清单明确开放的点歌命令。只传歌曲查询和稳定幂等键；"
        "不能指定命令名、目标会话或消息段。成功只代表指令已发出，随后直接使用返回的"
        "message_ids 结束聊天轮次，不追加确认。",
        {
            "type": "object",
            "required": ["query", "idempotency_key"],
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MUSIC_QUERY_MAX_CHARS,
                },
                "idempotency_key": {"type": "string"},
            },
        },
        (
            agent_command.send_music_command
            if agent_command is not None
            else _unsupported
        ),
        skill_name="music-command",
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.CHAT_REPLY}),
    )
    register(
        "create_task",
        "为需要延后执行或生成可交付结果的请求创建一个单一任务；普通聊天和状态查询不使用。"
        "goal 忠实概括原始请求，不扩大执行方式；constraints 和 requested_deliverables "
        "只记录用户提出或已确认的要求，不自行增加格式、数量、配字或限制。"
        "allowed_skills 是可用能力，不代表用户授权改变目标；只授予完成原请求需要的能力。"
        "allowed_skills 只填写 Task Executor 可用的执行 Skill。acknowledgement 必须是"
        "一句简短自然的即时消息，只确认开始处理，不完整复述请求，不解释工具、执行步骤或"
        "投递位置，也不承诺完成时间。创建成功后 Runtime 负责发送确认并结束当前 "
        "Conversation Turn，不要再次发送确认。",
        {
            "type": "object",
            "required": [
                "source_message_ids",
                "goal",
                "acknowledgement",
                "allowed_skills",
                "idempotency_key",
            ],
            "properties": {
                "source_message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "goal": {"type": "string"},
                "acknowledgement": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": TASK_ACKNOWLEDGEMENT_MAX_CHARS,
                },
                "constraints": {"type": "array", "items": {"type": "string"}},
                "requested_deliverables": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "involved_people": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["participant_ref"],
                        "properties": {
                            "participant_ref": {"type": "string"},
                            "role": {"type": "string"},
                            "involvement": {"type": "string"},
                        },
                    },
                },
                "allowed_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "idempotency_key": {"type": "string"},
            },
        },
        task.create_task,
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.TASK_CREATE}),
    )
    register(
        "send_napcat_replies",
        "按顺序发送 1-3 条短聊天消息；每条消息都应只表达一个事实，且总条数不得超过"
        "动态上下文中的 max_reply_messages。必须按当前内容声明 response_mode。",
        {
            "type": "object",
            "required": ["messages", "response_mode", "idempotency_key"],
            "properties": {
                "response_mode": {
                    "type": "string",
                    "enum": [
                        "casual_chat",
                        "professional_answer",
                        "task_status",
                    ],
                },
                "messages": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {"type": "string", "maxLength": 80},
                },
                "mention_user_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
        chat.send_chat_replies,
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.CHAT_REPLY}),
    )
    register(
        "get_task_status",
        "查询已有任务时读取当前作用域的已确认状态、进度摘要和最近事件；不创建或修改任务。",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
            },
        },
        task.get_task_status,
        read_only=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.TASK_READ}),
    )
    register(
        "update_task",
        "修改当前作用域内任务状态或 revision。revise 时可用 allowed_skills 显式替换"
        " Task Executor Skill 授权；省略则保留当前授权。",
        {
            "type": "object",
            "required": ["task_id", "revision", "action", "idempotency_key"],
            "properties": {
                "task_id": {"type": "string"},
                "revision": {"type": "integer"},
                "action": {
                    "type": "string",
                    "enum": ["revise", "start", "pause", "resume", "delete"],
                },
                "goal": {"type": "string"},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "requested_deliverables": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "allowed_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "confirmation_message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "summary": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
        task.update_task,
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.TASK_UPDATE}),
    )
    register(
        "submit_task_correction",
        "用户否定既有结果或提出修正时，将说明、可验证验收条件和被否定 Artifact 关联到"
        "原任务，并创建继承纠正约束的新 revision；不要创建无关任务代替纠正。"
        "执行方式改变时用 allowed_skills 显式替换 Task Executor Skill 授权；省略则保留"
        "当前授权。",
        {
            "type": "object",
            "required": [
                "task_id",
                "revision",
                "instruction",
                "acceptance_criteria",
                "idempotency_key",
            ],
            "properties": {
                "task_id": {"type": "string"},
                "revision": {"type": "integer"},
                "instruction": {"type": "string"},
                "acceptance_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "rejected_artifact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "goal": {"type": "string"},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "requested_deliverables": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "allowed_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "idempotency_key": {"type": "string"},
            },
        },
        task.submit_task_correction,
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.TASK_CORRECT}),
    )
    register(
        "send_task_delivery",
        "仅在消费 task.event 的轮次由 Conversation Agent 向任务原请求人发送已验证交付物；"
        "请求消息和请求人由 Runtime 根据任务所有权确定，普通 user.message 轮次和 "
        "Task Executor 无权调用。完整任务只交付一个音频时，Runtime 会忽略重复完成文案，"
        "只发送一条语音消息；成功后直接结束 Turn，不要追加普通聊天回复。",
        {
            "type": "object",
            "required": ["task_id", "revision", "idempotency_key"],
            "properties": {
                "task_id": {"type": "string"},
                "revision": {"type": "integer"},
                "text": {"type": "string"},
                "artifacts": {"type": "array"},
                "idempotency_key": {"type": "string"},
            },
        },
        task.send_task_delivery,
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.TASK_DELIVERY}),
    )
    return registry


async def _unsupported(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return ToolResult(
        content="load_skill is handled by the session driver",
        is_error=True,
        error_code="INTERNAL_TOOL",
    )

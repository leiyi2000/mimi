from __future__ import annotations

import asyncio
import base64
import html
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from core.agent import AgentError, AgentLoop
from core.config import Config
from core.run import RunStatus
from core.topic import TopicCandidate, TopicGate
from core.tools import Artifact, ToolContext, ToolImage
from domain.memory import MemoryStore
from domain.message_content import UserMessage


log = logging.getLogger(__name__)

SendText = Callable[[str, bool], Awaitable[None]]
SendArtifact = Callable[[Artifact], Awaitable[None]]
SendGroupText = Callable[[str], Awaitable[None]]
RouteAction = Literal["ignore", "stop", "absorb", "respond"]


@dataclass(frozen=True, slots=True)
class TopicRoute:
    action: RouteAction
    explicit: bool = False
    topic_id: str | None = None


@dataclass(slots=True)
class ActiveTopic:
    topic_id: str
    opened_at: float
    idle_expires_at: float
    hard_expires_at: float
    summary: str
    previous_topic_id: str | None = None
    accepted_message_ids: deque[str] | None = None

    def __post_init__(self) -> None:
        if self.accepted_message_ids is None:
            self.accepted_message_ids = deque(maxlen=50)

    def active(self, now: float) -> bool:
        return now < self.idle_expires_at and now < self.hard_expires_at


@dataclass(frozen=True, slots=True)
class PreviousTopicCheckpoint:
    goal: str
    confirmed_facts: tuple[str, ...]
    latest_result: str
    pending: tuple[str, ...]
    status: str

    def prompt(self) -> str:
        facts = "；".join(self.confirmed_facts) or "无"
        pending = "；".join(self.pending) or "无"
        return (
            "这是紧邻的上一群聊话题检查点，仅在与当前请求相关时作为背景使用；"
            "当前请求优先，不得继承上一位成员的身份或权限。\n"
            f"目标：{self.goal or '未提取'}\n"
            f"已确认事实：{facts}\n"
            f"最新结果：{self.latest_result or '无'}\n"
            f"未完成项：{pending}\n"
            f"状态：{self.status}"
        )


@dataclass(frozen=True, slots=True)
class IncomingTurn:
    message_id: int
    group_id: int
    sequence: int
    topic_id: str
    previous_topic_id: str | None
    respond: bool
    user_id: int
    nickname_snapshot: str
    message: UserMessage
    received_at: float
    send_text: SendText
    send_artifact: SendArtifact
    send_group_text: SendGroupText | None = None
    is_admin: bool = False


@dataclass(frozen=True, slots=True)
class CompletedTurn:
    sequence: int
    topic_id: str
    user_id: int
    nickname_snapshot: str
    user_text: str
    assistant_text: str
    status: RunStatus | Literal["in_progress"] = "completed"


@dataclass(slots=True)
class _GroupState:
    queue: asyncio.Queue[IncomingTurn]
    history: deque[CompletedTurn]
    worker: asyncio.Task[None]
    next_sequence: int = 1
    last_activity: float = 0.0
    active_topic: ActiveTopic | None = None


class ConversationStore:
    _STOP_PHRASES = frozenset({"结束对话", "退出对话", "不用回复了"})

    def __init__(
        self,
        *,
        config: Config,
        agent: AgentLoop,
        memory: MemoryStore,
        topic_gate: TopicGate | None = None,
    ) -> None:
        self.config = config
        self.agent = agent
        self.memory = memory
        self.topic_gate = topic_gate
        self._groups: dict[int, _GroupState] = {}
        self._seen: dict[int, float] = {}

    def parse_trigger(self, text: str) -> str | None:
        stripped = text.lstrip()
        if not stripped.startswith(self.config.trigger_prefix):
            return None
        remainder = stripped[len(self.config.trigger_prefix) :]
        return remainder.lstrip(" \t,，:：")

    async def route(
        self,
        *,
        group_id: int,
        user_id: int,
        nickname: str,
        text: str,
        explicit: bool,
        has_reply: bool,
        only_mentions_others: bool,
        other_command: bool,
        has_images: bool,
        has_emojis: bool,
    ) -> TopicRoute:
        state = self._groups.get(group_id)
        now = time.monotonic()
        topic = self._active_topic(state, now)
        trigger_text = self.parse_trigger(text)
        normalized = (
            trigger_text if trigger_text is not None else text
        ).strip().rstrip("。.!！")
        if explicit:
            if normalized in self._STOP_PHRASES:
                return TopicRoute("stop", explicit=True)
            return TopicRoute("respond", explicit=True)
        if topic is None or other_command or only_mentions_others:
            return TopicRoute("ignore")
        if has_reply:
            return TopicRoute("respond", topic_id=topic.topic_id)
        if self.topic_gate is None:
            return TopicRoute("ignore")
        recent = self._recent_topic_turns(state.history, topic.topic_id)
        try:
            decision = await self.topic_gate.classify(
                TopicCandidate(
                    summary=topic.summary,
                    recent_turns=recent,
                    user_id=user_id,
                    nickname=nickname[:100],
                    text=text.strip()[:1000],
                    has_images=has_images,
                    has_emojis=has_emojis,
                    seconds_since_activity=max(
                        0.0,
                        now
                        - (
                            topic.idle_expires_at
                            - self.config.continuous_chat_idle_ttl
                        ),
                    ),
                )
            )
        except Exception:
            log.exception(
                "topic gate failed: group_id=%s user_id=%s",
                group_id,
                user_id,
            )
            return TopicRoute("ignore")
        return TopicRoute(decision.action, topic_id=topic.topic_id)

    def end_topic(self, group_id: int) -> bool:
        state = self._groups.get(group_id)
        if state is None or state.active_topic is None:
            return False
        state.active_topic = None
        return True

    async def accept(
        self,
        *,
        message_id: int,
        group_id: int,
        user_id: int,
        nickname: str,
        message: UserMessage,
        send_text: SendText,
        send_artifact: SendArtifact,
        route: TopicRoute | None = None,
        send_group_text: SendGroupText | None = None,
        is_admin: bool = False,
    ) -> bool:
        self._purge_seen()
        if message_id in self._seen:
            return False
        if len(message.history_text()) > self.config.max_input_chars:
            await send_text("消息太长了，请精简后再试。", True)
            return False
        if message.is_empty:
            await send_text(
                f"用法：{self.config.trigger_prefix} <内容>",
                True,
            )
            return False
        state = self._state(group_id)
        if state.queue.full():
            await send_text("当前群请求较多，请稍后再试。", True)
            return False

        now = time.monotonic()
        if route is None:
            topic_id = "legacy"
            previous_topic_id = None
            respond = True
        elif route.explicit:
            previous = self._active_topic(state, now)
            topic_id = uuid.uuid4().hex
            previous_topic_id = previous.topic_id if previous is not None else None
            state.active_topic = ActiveTopic(
                topic_id=topic_id,
                opened_at=now,
                idle_expires_at=now + self.config.continuous_chat_idle_ttl,
                hard_expires_at=now + self.config.continuous_chat_max_ttl,
                summary=message.history_text()[:1000],
                previous_topic_id=previous_topic_id,
            )
            respond = True
        else:
            topic = self._active_topic(state, now)
            if (
                topic is None
                or route.topic_id is None
                or route.topic_id != topic.topic_id
            ):
                return False
            topic_id = topic.topic_id
            previous_topic_id = topic.previous_topic_id
            respond = route.action == "respond"
            topic.idle_expires_at = min(
                now + self.config.continuous_chat_idle_ttl,
                topic.hard_expires_at,
            )
            topic.summary = self._merge_summary(
                topic.summary, message.history_text()
            )

        turn = IncomingTurn(
            message_id=message_id,
            group_id=group_id,
            sequence=state.next_sequence,
            topic_id=topic_id,
            previous_topic_id=previous_topic_id,
            respond=respond,
            user_id=user_id,
            nickname_snapshot=nickname[:100],
            message=message,
            received_at=now,
            send_text=send_text,
            send_artifact=send_artifact,
            send_group_text=send_group_text,
            is_admin=is_admin,
        )
        state.next_sequence += 1
        state.queue.put_nowait(turn)
        self._seen[message_id] = now
        if state.active_topic is not None:
            state.active_topic.accepted_message_ids.append(str(message_id))
        return True

    async def close(self) -> None:
        workers = [state.worker for state in self._groups.values()]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    def _state(self, group_id: int) -> _GroupState:
        existing = self._groups.get(group_id)
        now = time.monotonic()
        if (
            existing is not None
            and now - existing.last_activity > self.config.history_idle_ttl
            and existing.queue.empty()
        ):
            existing.history.clear()
        if existing is not None:
            existing.last_activity = now
            self._active_topic(existing, now)
            return existing
        queue: asyncio.Queue[IncomingTurn] = asyncio.Queue(
            self.config.group_queue_size
        )
        placeholder = asyncio.current_task()
        assert placeholder is not None
        state = _GroupState(
            queue=queue,
            history=deque(maxlen=self.config.history_turns or 1),
            worker=placeholder,
            last_activity=now,
        )
        state.worker = asyncio.create_task(
            self._worker(group_id, state), name=f"niu-group-{group_id}"
        )
        self._groups[group_id] = state
        return state

    async def _worker(self, group_id: int, state: _GroupState) -> None:
        while True:
            turn = await state.queue.get()
            try:
                await self._process(state, turn)
            except Exception:
                log.exception(
                    "conversation worker failed: group_id=%s message_id=%s",
                    group_id,
                    turn.message_id,
                )
                await self._safe_send(
                    turn.send_text,
                    "处理消息失败，请稍后再试。",
                    True,
                )
            finally:
                state.last_activity = time.monotonic()
                state.queue.task_done()

    async def _process(self, state: _GroupState, turn: IncomingTurn) -> None:
        current_text = turn.message.history_text()
        if not turn.respond:
            self._append_history(
                state,
                turn,
                current_text,
                "",
                status="in_progress",
            )
            self._refresh_topic(state, turn.topic_id, current_text, "")
            return

        context = ToolContext(
            group_id=turn.group_id,
            user_id=turn.user_id,
            message_id=turn.message_id,
            nickname=turn.nickname_snapshot,
            is_admin=turn.is_admin,
            images=tuple(
                ToolImage(image.data, image.mime_type, image.label)
                for image in turn.message.images
            ),
            quoted_images=tuple(
                ToolImage(image.data, image.mime_type, image.label)
                for image in (
                    turn.message.quoted.images
                    if turn.message.quoted is not None
                    else ()
                )
            ),
            send_group_text=turn.send_group_text,
        )
        messages = self._messages(state.history, turn)
        checkpoint = self._checkpoint(
            state.history, turn.previous_topic_id
        )
        try:
            memory_context = await self.memory.system_context(
                turn.group_id, turn.user_id, current_text
            )
            response = await self.agent.run(
                messages=messages,
                context=context,
                memory_context=memory_context,
                conversation_context=checkpoint.prompt() if checkpoint else "",
                user_text=current_text,
            )
        except AgentError as exc:
            await turn.send_text(exc.user_message, True)
            return
        except RuntimeError as exc:
            if str(exc) == "MEMORY_CORRUPTED":
                await turn.send_text(
                    "记忆数据损坏，已隔离，请联系管理员。",
                    True,
                )
                return
            raise

        for artifact in response.artifacts:
            try:
                await turn.send_artifact(artifact)
            except Exception:
                log.exception(
                    "artifact send failed: group_id=%s message_id=%s kind=%s",
                    turn.group_id,
                    turn.message_id,
                    artifact.kind,
                )
                await self._safe_send(
                    turn.send_text,
                    "部分内容已生成，但附件发送失败，请稍后重试。",
                    True,
                )
                return
        if response.text.strip():
            await turn.send_text(response.text, response.mention_sender)
        self._append_history(
            state,
            turn,
            current_text,
            response.text,
            status=response.status,
        )
        self._refresh_topic(
            state, turn.topic_id, current_text, response.text
        )

    def _append_history(
        self,
        state: _GroupState,
        turn: IncomingTurn,
        user_text: str,
        assistant_text: str,
        *,
        status: RunStatus | Literal["in_progress"],
    ) -> None:
        if not self.config.history_turns:
            return
        state.history.append(
            CompletedTurn(
                sequence=turn.sequence,
                topic_id=turn.topic_id,
                user_id=turn.user_id,
                nickname_snapshot=turn.nickname_snapshot,
                user_text=user_text,
                assistant_text=assistant_text,
                status=status,
            )
        )

    def _refresh_topic(
        self,
        state: _GroupState,
        topic_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        topic = state.active_topic
        now = time.monotonic()
        if topic is None or topic.topic_id != topic_id or not topic.active(now):
            return
        topic.idle_expires_at = min(
            now + self.config.continuous_chat_idle_ttl,
            topic.hard_expires_at,
        )
        topic.summary = self._merge_summary(
            topic.summary,
            "\n".join(part for part in (user_text, assistant_text) if part),
        )

    def _messages(
        self, history: deque[CompletedTurn], turn: IncomingTurn
    ) -> list[dict[str, object]]:
        selected: list[CompletedTurn] = []
        chars = 0
        for item in reversed(history):
            if item.topic_id != turn.topic_id:
                continue
            size = len(item.user_text) + len(item.assistant_text)
            if chars + size > self.config.history_max_chars:
                break
            selected.append(item)
            chars += size
        messages: list[dict[str, object]] = []
        for item in reversed(selected):
            messages.append(
                {
                    "role": "user",
                    "content": self._user_content(
                        item.user_id,
                        item.nickname_snapshot,
                        item.user_text,
                    ),
                }
            )
            if item.assistant_text:
                messages.append(
                    {"role": "assistant", "content": item.assistant_text}
                )
        messages.append(
            {
                "role": "user",
                "content": turn.message.api_content(
                    turn.user_id, turn.nickname_snapshot
                ),
            }
        )
        return messages

    @staticmethod
    def _checkpoint(
        history: deque[CompletedTurn],
        previous_topic_id: str | None,
    ) -> PreviousTopicCheckpoint | None:
        if previous_topic_id is None:
            return None
        turns = [
            item for item in history if item.topic_id == previous_topic_id
        ][-4:]
        if not turns:
            return None
        goal = turns[0].user_text[:500]
        facts = tuple(
            item.user_text[:300]
            for item in turns[1:]
            if item.user_text.strip()
        )
        latest_result = next(
            (
                item.assistant_text[:800]
                for item in reversed(turns)
                if item.assistant_text.strip()
            ),
            "",
        )
        status = turns[-1].status
        pending = (
            ("继续核对上一话题中尚未确认的信息",)
            if status != "completed"
            else ()
        )
        return PreviousTopicCheckpoint(
            goal=goal,
            confirmed_facts=facts,
            latest_result=latest_result,
            pending=pending,
            status=status,
        )

    @staticmethod
    def _recent_topic_turns(
        history: deque[CompletedTurn],
        topic_id: str,
    ) -> tuple[str, ...]:
        rows = [
            (
                f"{item.nickname_snapshot}: {item.user_text}"
                + (
                    f"\n南瓜: {item.assistant_text}"
                    if item.assistant_text
                    else ""
                )
            )[:800]
            for item in history
            if item.topic_id == topic_id
        ]
        return tuple(rows[-2:])

    @staticmethod
    def _active_topic(
        state: _GroupState | None,
        now: float,
    ) -> ActiveTopic | None:
        if state is None or state.active_topic is None:
            return None
        if not state.active_topic.active(now):
            state.active_topic = None
            return None
        return state.active_topic

    @staticmethod
    def _merge_summary(current: str, addition: str) -> str:
        parts = [part.strip() for part in (current, addition) if part.strip()]
        return "\n".join(parts)[-1000:]

    @staticmethod
    def _user_content(user_id: int, nickname: str, text: str) -> str:
        return (
            "<group-message>\n"
            f'<sender id="{user_id}" nickname="{html.escape(nickname, quote=True)}" />\n'
            f"<text>{html.escape(text)}</text>\n"
            "</group-message>"
        )

    def _purge_seen(self) -> None:
        cutoff = time.monotonic() - self.config.message_cache_ttl
        self._seen = {
            message_id: seen_at
            for message_id, seen_at in self._seen.items()
            if seen_at >= cutoff
        }

    @staticmethod
    async def _safe_send(
        send: SendText,
        text: str,
        mention_sender: bool,
    ) -> None:
        try:
            await send(text, mention_sender)
        except Exception:
            log.exception("failed to send error response")


def artifact_base64(artifact: Artifact) -> str:
    return "base64://" + base64.b64encode(artifact.data).decode("ascii")

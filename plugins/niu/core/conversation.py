from __future__ import annotations

import asyncio
import base64
import html
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core.agent import AgentError, AgentLoop
from core.config import Config
from domain.memory import MemoryStore
from domain.message_content import UserMessage
from core.tools import Artifact, ToolContext, ToolImage


log = logging.getLogger(__name__)

SendText = Callable[[str, bool], Awaitable[None]]
SendArtifact = Callable[[Artifact], Awaitable[None]]
SendGroupText = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class IncomingTurn:
    message_id: int
    group_id: int
    sequence: int
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
    user_id: int
    nickname_snapshot: str
    user_text: str
    assistant_text: str


@dataclass(slots=True)
class _GroupState:
    queue: asyncio.Queue[IncomingTurn]
    history: deque[CompletedTurn]
    worker: asyncio.Task[None]
    next_sequence: int = 1
    last_activity: float = 0.0


class ConversationStore:
    def __init__(
        self,
        *,
        config: Config,
        agent: AgentLoop,
        memory: MemoryStore,
    ) -> None:
        self.config = config
        self.agent = agent
        self.memory = memory
        self._groups: dict[int, _GroupState] = {}
        self._seen: dict[int, float] = {}

    def parse_trigger(self, text: str) -> str | None:
        stripped = text.lstrip()
        if not stripped.startswith(self.config.trigger_prefix):
            return None
        remainder = stripped[len(self.config.trigger_prefix) :]
        return remainder.lstrip(" \t,，:：")

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
        turn = IncomingTurn(
            message_id=message_id,
            group_id=group_id,
            sequence=state.next_sequence,
            user_id=user_id,
            nickname_snapshot=nickname[:100],
            message=message,
            received_at=time.monotonic(),
            send_text=send_text,
            send_artifact=send_artifact,
            send_group_text=send_group_text,
            is_admin=is_admin,
        )
        state.next_sequence += 1
        state.queue.put_nowait(turn)
        self._seen[message_id] = time.monotonic()
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

    async def _process(
        self, state: _GroupState, turn: IncomingTurn
    ) -> None:
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
        current_text = turn.message.history_text()
        try:
            memory_context = await self.memory.system_context(
                turn.group_id, turn.user_id, current_text
            )
            response = await self.agent.run(
                messages=messages,
                context=context,
                memory_context=memory_context,
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
        await turn.send_text(response.text, response.mention_sender)
        if self.config.history_turns:
            state.history.append(
                CompletedTurn(
                    sequence=turn.sequence,
                    user_id=turn.user_id,
                    nickname_snapshot=turn.nickname_snapshot,
                    user_text=current_text,
                    assistant_text=response.text,
                )
            )

    def _messages(
        self, history: deque[CompletedTurn], turn: IncomingTurn
    ) -> list[dict[str, object]]:
        selected: list[CompletedTurn] = []
        chars = 0
        for item in reversed(history):
            size = len(item.user_text) + len(item.assistant_text)
            if chars + size > self.config.history_max_chars:
                break
            selected.append(item)
            chars += size
        messages: list[dict[str, object]] = []
        for item in reversed(selected):
            messages.extend(
                (
                    {
                        "role": "user",
                        "content": self._user_content(
                            item.user_id,
                            item.nickname_snapshot,
                            item.user_text,
                        ),
                    },
                    {"role": "assistant", "content": item.assistant_text},
                )
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

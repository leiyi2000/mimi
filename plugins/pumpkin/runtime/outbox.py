from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from adapters.napcat import NapCatReply, NapCatSender
from core.domain import AgentEventType, OutboxItem, OutboxStatus
from persistence.repositories import OutboxStore
from providers.text_image import LONG_REPLY_TEXT_LIMIT, TextImageRenderer


class OutboxSender:
    def __init__(
        self,
        *,
        store: OutboxStore,
        sender: NapCatSender,
        text_renderer: TextImageRenderer,
        data_dir: str,
        assistant_name: str,
    ) -> None:
        self.store = store
        self.sender = sender
        self.text_renderer = text_renderer
        self.data_dir = Path(data_dir)
        self.assistant_name = assistant_name
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._worker: asyncio.Task[None] | None = None
        self._closing = False
        self._failure: BaseException | None = None

    async def start(self) -> None:
        self._closing = False
        recovered = await self.store.mark_sending_outbox_unknown()
        for item in recovered:
            await self._append_event(item, AgentEventType.OUTBOX_UNKNOWN)
        await self.wake()

    async def submit(
        self,
        *,
        idempotency_key: str,
        scope_id: str,
        source_event_id: str,
        payload: dict[str, Any],
    ) -> OutboxItem:
        item = await self.store.enqueue_outbox(
            idempotency_key=idempotency_key,
            scope_id=scope_id,
            source_event_id=source_event_id,
            payload=payload,
        )
        if item.status in {
            OutboxStatus.SENT,
            OutboxStatus.FAILED,
            OutboxStatus.UNKNOWN,
        }:
            return item
        await self._append_event(item, AgentEventType.OUTBOX_REQUESTED)
        await self.wake()
        while True:
            current = await self.store.get_outbox(item.outbox_id)
            if current is None:
                raise RuntimeError("outbox item disappeared")
            if current.status in {
                OutboxStatus.SENT,
                OutboxStatus.FAILED,
                OutboxStatus.UNKNOWN,
            }:
                return current
            await asyncio.sleep(0.01)

    async def wake(self) -> None:
        if self._closing:
            return
        self._wake.set()
        if self._worker is None or self._worker.done():
            self._failure = None
            self._idle.clear()
            self._worker = asyncio.create_task(
                self._run(),
                name="pumpkin-outbox",
            )

    async def when_idle(self) -> None:
        await self._idle.wait()
        if self._failure is not None:
            raise self._failure

    async def close(self) -> None:
        self._closing = True
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self._idle.set()

    async def _run(self) -> None:
        try:
            while not self._closing:
                self._wake.clear()
                while item := await self.store.claim_outbox():
                    await self._send(item)
                if not self._wake.is_set():
                    self._worker = None
                    self._idle.set()
                    return
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._failure = exc
            self._worker = None
            self._idle.set()
            raise

    async def _send(self, item: OutboxItem) -> None:
        payload = item.payload
        try:
            text, segments, rendered_as_image = self._prepare(item)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            finished = await self.store.finish_outbox(
                item.outbox_id,
                status=OutboxStatus.FAILED,
                error_code=f"OUTBOX_PREPARE_FAILED:{type(exc).__name__}",
            )
            await self._append_event(finished, AgentEventType.OUTBOX_FAILED)
            return
        try:
            message_ids = await self.sender.send(
                item.scope_id,
                NapCatReply(
                    text=text,
                    segments=segments,
                    reply_to_message_id=_optional_string(
                        payload.get("reply_to_message_id")
                    ),
                    mention_user_id=_optional_string(
                        payload.get("mention_user_id")
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            finished = await self.store.finish_outbox(
                item.outbox_id,
                status=OutboxStatus.UNKNOWN,
                error_code=f"SEND_OUTCOME_UNKNOWN:{type(exc).__name__}",
            )
            await self._append_event(finished, AgentEventType.OUTBOX_UNKNOWN)
            return
        original_text = str(payload.get("original_text", text or ""))
        safe_segments = tuple(
            {"type": str(segment.get("type", "unknown"))}
            for segment in segments
        )
        try:
            await self.store.save_outbound_message(
                scope_id=item.scope_id,
                message_ids=message_ids,
                text=original_text,
                segments=safe_segments,
                source=str(payload.get("source", "conversation")),
                idempotency_key=item.idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            finished = await self.store.finish_outbox(
                item.outbox_id,
                status=OutboxStatus.UNKNOWN,
                message_ids=message_ids,
                error_code=f"OUTBOUND_PERSIST_UNKNOWN:{type(exc).__name__}",
            )
            await self._append_event(finished, AgentEventType.OUTBOX_UNKNOWN)
            return
        finished = await self.store.finish_outbox(
            item.outbox_id,
            status=OutboxStatus.SENT,
            message_ids=message_ids,
        )
        await self._append_event(
            finished,
            AgentEventType.OUTBOX_SENT,
            extra={"rendered_as_image": rendered_as_image},
        )

    def _prepare(
        self,
        item: OutboxItem,
    ) -> tuple[str, tuple[dict[str, Any], ...], bool]:
        payload = item.payload
        text = str(payload.get("text", "")).strip()
        raw_segments = payload.get("segments", [])
        if not isinstance(raw_segments, list):
            raise TypeError("outbox segments must be a list")
        segments = tuple(dict(segment) for segment in raw_segments)
        segment_text = "".join(
            str(segment.get("text", ""))
            for segment in segments
            if segment.get("type") == "text"
        )
        render_text = f"{segment_text}{text}"
        if len(render_text) <= LONG_REPLY_TEXT_LIMIT:
            return text, segments, False
        path = self._write_rendered_reply(render_text, item.idempotency_key)
        non_text = tuple(
            segment for segment in segments if segment.get("type") != "text"
        )
        return "", (*non_text, {"type": "image", "file": path}), True

    def _write_rendered_reply(self, text: str, idempotency_key: str) -> str:
        content = self.text_renderer.render(
            text,
            label=self.assistant_name,
        )
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("text renderer returned an invalid PNG")
        root = (self.data_dir / "rendered-replies").resolve()
        root.mkdir(parents=True, exist_ok=True)
        filename = (
            f"{hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()[:24]}.png"
        )
        path = root / filename
        fd, temporary = tempfile.mkstemp(prefix=f".{filename}.", dir=root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return str(path)

    async def _append_event(
        self,
        item: OutboxItem,
        event_type: AgentEventType,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        agent_id = f"conversation:{item.scope_id}"
        await self.store.append_agent_event(
            agent_id=agent_id,
            session_id=f"{agent_id}:session",
            scope_id=item.scope_id,
            event_type=event_type,
            payload={
                "outbox_id": item.outbox_id,
                "idempotency_key": item.idempotency_key,
                "status": item.status.value,
                "message_ids": list(item.message_ids),
                "error_code": item.error_code,
                **(extra or {}),
            },
            turn_id=item.source_event_id,
            event_id=f"{item.outbox_id}:{event_type.value}",
        )


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None

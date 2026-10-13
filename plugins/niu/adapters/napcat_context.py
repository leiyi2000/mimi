from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from napcat import NapCatError, Reply, Text
from napcat.types.messages import MessageSegment, UnknownMessageSegment


class NapCatContextError(RuntimeError):
    pass


class NapCatContextClient(Protocol):
    async def get_group_member_info(self, **kwargs: Any) -> Any: ...

    async def get_msg(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class QuotedPayload:
    message_id: str
    sender_id: int
    sender_nickname: str
    segments: tuple[MessageSegment | UnknownMessageSegment, ...]


class NapCatContextAdapter:
    def __init__(self, client: NapCatContextClient) -> None:
        self.client = client

    async def member_name(
        self,
        *,
        group_id: int,
        user_id: int,
        fallback: str | None = None,
    ) -> str:
        try:
            data = await self.client.get_group_member_info(
                group_id=str(group_id),
                user_id=str(user_id),
                no_cache=False,
            )
        except (NapCatError, OSError, TimeoutError):
            return self._fallback_name(user_id, fallback)
        if not isinstance(data, dict):
            return self._fallback_name(user_id, fallback)
        return self._first_name(data.get("card"), data.get("nickname")) or (
            self._fallback_name(user_id, fallback)
        )

    async def quoted_message(
        self,
        *,
        group_id: int,
        reply: Reply,
    ) -> QuotedPayload:
        identifier: str | int | None = (
            reply.seq if reply.seq is not None else reply.id
        )
        if identifier is None:
            raise NapCatContextError("REPLY_ID_MISSING")
        try:
            data = await self.client.get_msg(message_id=identifier)
        except (NapCatError, OSError, TimeoutError) as exc:
            raise NapCatContextError("REPLY_LOOKUP_FAILED") from exc
        if not isinstance(data, dict):
            raise NapCatContextError("REPLY_RESPONSE_INVALID")
        sender = data.get("sender")
        if not isinstance(sender, dict):
            raise NapCatContextError("REPLY_SENDER_INVALID")
        try:
            sender_id = int(sender["user_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NapCatContextError("REPLY_SENDER_INVALID") from exc
        sender_name = self._first_name(
            sender.get("card"),
            sender.get("nickname"),
        )
        if sender_name is None:
            sender_name = await self.member_name(
                group_id=group_id,
                user_id=sender_id,
            )
        segments = self._segments(data.get("message"))
        return QuotedPayload(
            message_id=str(data.get("message_id", identifier)),
            sender_id=sender_id,
            sender_nickname=sender_name,
            segments=segments,
        )

    @classmethod
    def _segments(
        cls, value: Any
    ) -> tuple[MessageSegment | UnknownMessageSegment, ...]:
        if isinstance(value, str):
            return (Text(text=value),) if value else ()
        if isinstance(value, dict):
            return (cls._segment(value),)
        if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
            raise NapCatContextError("REPLY_CONTENT_INVALID")
        segments: list[MessageSegment | UnknownMessageSegment] = []
        for item in value:
            if isinstance(item, (MessageSegment, UnknownMessageSegment)):
                segments.append(item)
            elif isinstance(item, dict):
                segments.append(cls._segment(item))
            else:
                raise NapCatContextError("REPLY_CONTENT_INVALID")
        return tuple(segments)

    @staticmethod
    def _segment(
        value: dict[str, Any],
    ) -> MessageSegment | UnknownMessageSegment:
        try:
            return MessageSegment.from_dict(value)
        except (TypeError, ValueError) as exc:
            raise NapCatContextError("REPLY_CONTENT_INVALID") from exc

    @staticmethod
    def _first_name(*values: Any) -> str | None:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text[:100]
        return None

    @classmethod
    def _fallback_name(cls, user_id: int, fallback: str | None) -> str:
        return cls._first_name(fallback) or f"QQ用户{user_id}"

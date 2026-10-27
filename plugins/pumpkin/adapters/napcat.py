from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from napcat import (
    At,
    Face,
    File,
    Forward,
    Image,
    Json,
    MFace,
    MessageEvent,
    NodeInline,
    NodeReference,
    Record,
    Reply,
    Text,
)

from core.domain import (
    ConversationType,
    InboundMessage,
    MessageSegment,
    MessageSegmentType,
    scope_for,
)


class MessageAdapterError(ValueError):
    """The event is not a supported, safe inbound message."""


@dataclass(frozen=True, slots=True)
class NapCatReply:
    text: str | None = None
    segments: tuple[dict[str, Any], ...] = ()
    reply_to_message_id: str | None = None
    mention_user_id: str | None = None


class NapCatMessageAdapter:
    def adapt(self, event: MessageEvent | dict[str, Any]) -> InboundMessage:
        if isinstance(event, dict):
            try:
                event = MessageEvent.from_dict(event)
            except Exception as exc:
                raise MessageAdapterError("invalid NapCat message event") from exc
        message_type = getattr(event, "message_type", None)
        if message_type not in {"group", "private"}:
            raise MessageAdapterError("unsupported message type")
        message_id = getattr(event, "message_id", None)
        user_id = getattr(event, "user_id", None)
        sender = getattr(event, "sender", None)
        self_id = getattr(event, "self_id", None)
        if message_id is None or user_id is None or sender is None:
            raise MessageAdapterError("message event misses required fields")

        if message_type == "group":
            group_id = getattr(event, "group_id", None)
            if group_id is None:
                raise MessageAdapterError("group message misses group_id")
            conversation_type = ConversationType.GROUP
            scope_id = scope_for(conversation_type, group_id=group_id)
            normalized_group_id = str(group_id)
        else:
            conversation_type = ConversationType.PRIVATE
            scope_id = scope_for(conversation_type, user_id=user_id)
            normalized_group_id = None

        segments = tuple(self._segment(segment) for segment in event.message)
        received_at = datetime.fromtimestamp(float(getattr(event, "time", 0)), tz=UTC)
        sender_id = str(user_id)
        sender_name = str(
            getattr(sender, "card", None)
            or getattr(sender, "nickname", None)
            or sender_id
        )
        reply_to = next(
            (
                segment.data.get("id")
                for segment in segments
                if segment.type is MessageSegmentType.REPLY
                and segment.data.get("id") is not None
            ),
            None,
        )
        raw_text = str(getattr(event, "raw_message", "") or "").strip()
        return InboundMessage(
            message_id=str(message_id),
            conversation_type=conversation_type,
            scope_id=scope_id,
            sender_id=sender_id,
            sender_name=sender_name,
            segments=segments,
            received_at=received_at,
            is_self=(
                getattr(event, "post_type", "message") == "message_sent"
                or (self_id is not None and str(self_id) == sender_id)
            ),
            raw_text=raw_text,
            group_id=normalized_group_id,
            reply_to_message_id=str(reply_to) if reply_to is not None else None,
        )

    async def enrich(
        self,
        message: InboundMessage,
        client: Any,
        *,
        max_depth: int,
        max_nodes: int,
    ) -> InboundMessage:
        budget = [max(1, max_nodes)]
        seen: set[str] = set()
        segments = await self._expand_segments(
            message.segments,
            client,
            depth=0,
            max_depth=max(0, max_depth),
            budget=budget,
            seen=seen,
        )
        return replace(message, segments=segments)

    def _segment(self, segment: Any) -> MessageSegment:
        if isinstance(segment, Mapping):
            segment_type = str(segment.get("type") or "unknown")
            raw_data = segment.get("data", {})
            data = dict(raw_data) if isinstance(raw_data, Mapping) else {}
        else:
            segment_type = str(getattr(segment, "_type", "") or "unknown")
            data = {}
            if is_dataclass(segment):
                data = {
                    item.name: getattr(segment, item.name)
                    for item in fields(segment)
                    if not item.name.startswith("_")
                }
        if segment_type == "text":
            return MessageSegment(
                type=MessageSegmentType.TEXT,
                text=str(self._value(segment, data, "text") or ""),
                data=data,
            )
        if segment_type == "at":
            return MessageSegment(
                type=MessageSegmentType.AT,
                data={"qq": str(getattr(segment, "qq", "")), **data},
            )
        if segment_type == "reply":
            return MessageSegment(
                type=MessageSegmentType.REPLY,
                data={"id": str(getattr(segment, "id", "")), **data},
            )
        if segment_type == "face":
            return MessageSegment(
                type=MessageSegmentType.FACE,
                data={"id": str(self._value(segment, data, "id")), **data},
            )
        if segment_type == "mface":
            return MessageSegment(
                type=MessageSegmentType.MFACE,
                data=data,
            )
        if segment_type == "image":
            return MessageSegment(
                type=MessageSegmentType.IMAGE,
                data={
                    "file": getattr(segment, "file", None),
                    "url": getattr(segment, "url", None),
                    **data,
                },
            )
        if segment_type == "record":
            return MessageSegment(
                type=MessageSegmentType.RECORD,
                data={
                    "file": self._value(segment, data, "file"),
                    "url": self._value(segment, data, "url"),
                    **data,
                },
            )
        if segment_type == "forward":
            return MessageSegment(
                type=MessageSegmentType.FORWARD,
                data={
                    "id": str(self._value(segment, data, "id") or ""),
                    "content": self._value(segment, data, "content"),
                    **data,
                },
            )
        if segment_type == "node":
            return MessageSegment(
                type=MessageSegmentType.NODE,
                data=data,
            )
        if segment_type == "json":
            return MessageSegment(
                type=MessageSegmentType.JSON,
                data=data,
            )
        if segment_type == "file":
            return MessageSegment(
                type=MessageSegmentType.FILE,
                data={
                    "file": getattr(segment, "file", None),
                    "url": getattr(segment, "url", None),
                    **data,
                },
            )
        return MessageSegment(
            type=MessageSegmentType.UNKNOWN,
            text=str(self._value(segment, data, "text") or ""),
            data={"type": segment_type, **data},
        )

    async def _expand_segments(
        self,
        segments: tuple[MessageSegment, ...],
        client: Any,
        *,
        depth: int,
        max_depth: int,
        budget: list[int],
        seen: set[str],
    ) -> tuple[MessageSegment, ...]:
        expanded: list[MessageSegment] = []
        for segment in segments:
            data = dict(segment.data)
            if segment.type is MessageSegmentType.REPLY:
                reply_id = str(data.get("id") or "")
                if reply_id and depth < max_depth:
                    quoted = await self._get_message(client, reply_id)
                    if quoted:
                        data["quoted"] = await self._quoted_payload(
                            quoted,
                            client,
                            depth=depth + 1,
                            max_depth=max_depth,
                            budget=budget,
                            seen=seen,
                        )
                expanded.append(replace(segment, data=data))
                continue
            if segment.type is not MessageSegmentType.FORWARD:
                expanded.append(segment)
                continue
            forward_id = str(data.get("id") or "")
            if not forward_id:
                expanded.append(
                    replace(segment, data={**data, "expand_error": "missing_id"})
                )
                continue
            if depth >= max_depth:
                expanded.append(replace(segment, data={**data, "truncated": True}))
                continue
            if forward_id in seen:
                expanded.append(
                    replace(
                        segment,
                        data={**data, "truncated": True, "expand_error": "recursive"},
                    )
                )
                continue
            if budget[0] <= 0:
                expanded.append(
                    replace(
                        segment,
                        data={**data, "truncated": True, "expand_error": "node_limit"},
                    )
                )
                continue
            seen.add(forward_id)
            payload = await self._get_forward(client, forward_id)
            if not payload:
                expanded.append(
                    replace(segment, data={**data, "expand_error": "unavailable"})
                )
                continue
            raw_messages = payload.get("messages", [])
            children: list[dict[str, Any]] = []
            for item in raw_messages:
                if budget[0] <= 0:
                    break
                budget[0] -= 1
                children.append(
                    await self._forward_node(
                        item,
                        client,
                        depth=depth + 1,
                        max_depth=max_depth,
                        budget=budget,
                        seen=seen,
                    )
                )
            expanded.append(
                replace(
                    segment,
                    data={
                        **data,
                        "messages": children,
                        "truncated": len(children) < len(raw_messages),
                    },
                )
            )
        return tuple(expanded)

    async def _forward_node(
        self,
        item: Any,
        client: Any,
        *,
        depth: int,
        max_depth: int,
        budget: list[int],
        seen: set[str],
    ) -> dict[str, Any]:
        if not isinstance(item, Mapping):
            return {"type": "unknown", "text": "[无法解析的转发节点]"}
        raw_segments = item.get("message", item.get("content", []))
        if not isinstance(raw_segments, list):
            raw_segments = []
        segments = tuple(self._segment(value) for value in raw_segments)
        segments = await self._expand_segments(
            segments,
            client,
            depth=depth,
            max_depth=max_depth,
            budget=budget,
            seen=seen,
        )
        sender = item.get("sender", {})
        return {
            "type": "node",
            "user_id": str(item.get("user_id") or sender.get("user_id") or ""),
            "nickname": str(item.get("nickname") or sender.get("nickname") or ""),
            "time": item.get("time"),
            "segments": [
                {"type": value.type.value, "text": value.text, "data": value.data}
                for value in segments
            ],
        }

    async def _get_message(self, client: Any, message_id: str) -> dict[str, Any] | None:
        try:
            result = await client.call_action("get_msg", {"message_id": message_id})
        except Exception:  # noqa: BLE001 - enrichment must not reject the message
            return None
        return dict(result) if isinstance(result, Mapping) else None

    async def _get_forward(self, client: Any, message_id: str) -> dict[str, Any] | None:
        try:
            result = await client.get_forward_msg(message_id=message_id)
        except Exception:  # noqa: BLE001 - enrichment must not reject the message
            try:
                result = await client.call_action(
                    "get_forward_msg", {"message_id": message_id}
                )
            except Exception:  # noqa: BLE001 - enrichment must not reject the message
                return None
        return dict(result) if isinstance(result, Mapping) else None

    async def _quoted_payload(
        self,
        payload: Mapping[str, Any],
        client: Any,
        *,
        depth: int,
        max_depth: int,
        budget: list[int],
        seen: set[str],
    ) -> dict[str, Any]:
        raw_segments = payload.get("message", [])
        if not isinstance(raw_segments, list):
            raw_segments = []
        segments = tuple(self._segment(value) for value in raw_segments)
        segments = await self._expand_segments(
            segments,
            client,
            depth=depth,
            max_depth=max_depth,
            budget=budget,
            seen=seen,
        )
        sender = payload.get("sender", {})
        return {
            "message_id": str(payload.get("message_id") or ""),
            "sender_id": str(payload.get("user_id") or sender.get("user_id") or ""),
            "sender_name": str(sender.get("nickname") or sender.get("card") or ""),
            "time": payload.get("time"),
            "segments": [
                {"type": value.type.value, "text": value.text, "data": value.data}
                for value in segments
            ],
        }

    @staticmethod
    def _value(segment: Any, data: dict[str, Any], name: str) -> Any:
        return data.get(name, getattr(segment, name, None))


class NapCatSender:
    """Translate validated domain replies into NapCat SDK messages."""

    def __init__(self, client: Any, *, allowed_path_root: str = "data"):
        self.client = client
        self.allowed_path_root = Path(allowed_path_root).resolve()

    async def send(
        self,
        scope_id: str,
        reply: NapCatReply,
    ) -> tuple[str, ...]:
        prefix_segments = []
        if reply.reply_to_message_id is not None:
            prefix_segments.append(Reply(id=reply.reply_to_message_id))
        if reply.mention_user_id is not None:
            prefix_segments.append(At(qq=reply.mention_user_id))
        content_segments = []
        record_segments = []
        for item in reply.segments:
            item_type = item.get("type")
            if item_type == "text":
                content_segments.append(Text(text=str(item.get("text", ""))))
            elif item_type == "at":
                content_segments.append(At(qq=str(item["qq"])))
            elif item_type == "face":
                content_segments.append(Face(id=str(item["id"])))
            elif item_type == "mface":
                content_segments.append(
                    MFace(
                        emoji_package_id=int(item["emoji_package_id"]),
                        emoji_id=str(item["emoji_id"]),
                        key=str(item["key"]),
                        summary=str(item.get("summary", "")),
                    )
                )
            elif item_type == "image":
                content_segments.append(
                    Image(file=self._safe_media_file(item["file"]))
                )
            elif item_type == "file":
                content_segments.append(
                    File(file=self._safe_media_file(item["file"]))
                )
            elif item_type == "record":
                record_segments.append(
                    Record(file=self._safe_media_file(item["file"]))
                )
            elif item_type == "json":
                content_segments.append(Json(data=item["data"]))
            elif item_type == "forward":
                content_segments.append(Forward(id=str(item["id"])))
            elif item_type == "node_reference":
                content_segments.append(NodeReference(id=str(item["id"])))
            elif item_type == "node_inline":
                content_segments.append(
                    NodeInline(
                        nickname=str(item["nickname"]),
                        content=item["content"],
                    )
                )
            else:
                raise MessageAdapterError(f"unsupported outbound segment: {item_type}")
        if reply.text:
            content_segments.append(Text(text=reply.text))
        if not prefix_segments and not content_segments and not record_segments:
            raise MessageAdapterError("empty reply")

        message_ids = []
        for record in record_segments:
            message_ids.append(await self._send_message(scope_id, [record]))
        remaining_segments = [*prefix_segments, *content_segments]
        if remaining_segments:
            message_ids.append(
                await self._send_message(scope_id, remaining_segments)
            )
        return tuple(message_ids)

    async def _send_message(self, scope_id: str, segments: list[Any]) -> str:
        if scope_id.startswith("group:"):
            response = await self.client.send_group_msg(
                group_id=scope_id.removeprefix("group:"),
                message=segments,
            )
        elif scope_id.startswith("private:"):
            response = await self.client.send_private_msg(
                user_id=scope_id.removeprefix("private:"),
                message=segments,
            )
        else:
            raise MessageAdapterError("invalid reply scope")
        message_id = (
            response.get("message_id") if isinstance(response, dict) else response
        )
        if message_id is None:
            raise RuntimeError("NapCat reply did not return message_id")
        return str(message_id)

    def _safe_media_file(self, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise MessageAdapterError("media file is empty")
        candidate = value.strip()
        if candidate.startswith(("https://", "http://", "base64://")):
            return candidate
        if candidate.startswith("file://"):
            candidate = candidate.removeprefix("file://")
        path = Path(candidate).resolve()
        try:
            path.relative_to(self.allowed_path_root)
        except ValueError as exc:
            raise MessageAdapterError(
                "media path is outside approved data directory"
            ) from exc
        if not path.is_file():
            raise MessageAdapterError("media file does not exist")
        return "base64://" + base64.b64encode(path.read_bytes()).decode("ascii")

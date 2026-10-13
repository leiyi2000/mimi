import base64
import html
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

import aiofiles
from napcat import Face, Image, Message, MFace, Text

from domain import ArchivedRecall
from rendering.assets import is_image


@dataclass(frozen=True)
class ArchivedMessagePart:
    kind: str
    data: dict[str, str]


class ArchivedMessageParser:
    _segment_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"\[CQ:(?P<kind>[A-Za-z0-9_-]+)(?P<arguments>(?:,[^\]]*)?)\]",
    )

    def parse(self, raw_message: str | None) -> tuple[ArchivedMessagePart, ...]:
        if not raw_message:
            return ()

        parts: list[ArchivedMessagePart] = []
        cursor = 0
        for match in self._segment_pattern.finditer(raw_message):
            if match.start() > cursor:
                parts.append(
                    ArchivedMessagePart(
                        kind="text",
                        data={"text": html.unescape(raw_message[cursor : match.start()])},
                    )
                )
            parts.append(
                ArchivedMessagePart(
                    kind=match.group("kind").lower(),
                    data=self._parse_arguments(match.group("arguments")),
                )
            )
            cursor = match.end()
        if cursor < len(raw_message):
            parts.append(
                ArchivedMessagePart(
                    kind="text",
                    data={"text": html.unescape(raw_message[cursor:])},
                )
            )
        return tuple(parts)

    def _parse_arguments(self, arguments: str) -> dict[str, str]:
        data: dict[str, str] = {}
        for argument in arguments.removeprefix(",").split(","):
            if not argument:
                continue
            key, separator, value = argument.partition("=")
            if separator:
                data[key] = html.unescape(value)
        return data


class OriginalMessageBuilder:
    _placeholders: ClassVar[dict[str, str]] = {
        "record": "[语音]",
        "video": "[视频]",
        "file": "[文件]",
        "forward": "[合并转发]",
        "json": "[卡片]",
        "xml": "[卡片]",
        "at": "[@成员]",
        "reply": "[回复]",
    }

    def __init__(
        self,
        message_parser: ArchivedMessageParser | None = None,
        timezone_name: str = "Asia/Shanghai",
    ):
        self.message_parser = message_parser or ArchivedMessageParser()
        self.timezone = ZoneInfo(timezone_name)

    async def build(self, record: ArchivedRecall) -> list[Message]:
        if record.sent_at is None:
            segments: list[Message] = [Text(text="原消息未存储")]
        else:
            segments = await self._content_segments(record)
        segments.append(Text(text=self._attribution(record)))
        return segments

    async def _content_segments(self, record: ArchivedRecall) -> list[Message]:
        segments: list[Message] = []
        image_index = 0
        for part in self.message_parser.parse(record.raw_message):
            if part.kind == "text":
                if text := part.data.get("text"):
                    segments.append(Text(text=text))
            elif part.kind == "image":
                segments.append(
                    await self._image_segment(record.local_image_paths, image_index)
                )
                image_index += 1
            elif part.kind == "face":
                segments.append(self._face_segment(part.data))
            elif part.kind == "mface":
                segments.append(self._market_face_segment(part.data))
            else:
                segments.append(
                    Text(text=self._placeholders.get(part.kind, "[消息]"))
                )

        while image_index < len(record.local_image_paths):
            segments.append(
                await self._image_segment(record.local_image_paths, image_index)
            )
            image_index += 1
        return segments or [Text(text="消息内容未存储")]

    async def _image_segment(
        self,
        paths: tuple[str | None, ...],
        index: int,
    ) -> Message:
        path = paths[index] if index < len(paths) else None
        if path is None:
            return Text(text="[图片未存储]")
        content = await self._read_image(Path(path))
        if content is None:
            return Text(text="[图片不可用]")
        encoded = base64.b64encode(content).decode()
        return Image(file=f"base64://{encoded}")

    def _face_segment(self, data: dict[str, str]) -> Message:
        face_id = data.get("id")
        if not face_id:
            return Text(text="[表情]")
        try:
            chain_count = (
                int(data["chainCount"]) if data.get("chainCount") is not None else None
            )
        except ValueError:
            return Text(text="[表情]")
        return Face(
            id=face_id,
            resultId=data.get("resultId"),
            chainCount=chain_count,
        )

    def _market_face_segment(self, data: dict[str, str]) -> Message:
        try:
            return MFace(
                emoji_package_id=int(data["emoji_package_id"]),
                emoji_id=data["emoji_id"],
                key=data["key"],
                summary=data["summary"],
            )
        except (KeyError, ValueError):
            return Text(text="[表情]")

    def _attribution(self, record: ArchivedRecall) -> str:
        name = self._visible_name(record.card) or self._visible_name(record.nickname)
        display_name = name or "群成员"
        if record.sent_at is None:
            sent_at = "未知时间"
        else:
            sent_at = datetime.fromtimestamp(
                record.sent_at,
                tz=self.timezone,
            ).strftime("%m-%d %H:%M")
        return f"\n—— {display_name} · 发送于 {sent_at}"

    def _visible_name(self, value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.strip()
        if not normalized or not any(character.isalnum() for character in normalized):
            return None
        return normalized

    async def _read_image(self, path: Path) -> bytes | None:
        try:
            async with aiofiles.open(path, "rb") as file:
                content = await file.read()
        except OSError:
            return None
        return content if is_image(content) else None

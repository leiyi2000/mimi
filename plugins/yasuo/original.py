import base64
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiofiles
from napcat import Image, Message, Text

from content import RecallContentParser
from domain import ArchivedRecall
from rendering.assets import is_image


class OriginalMessageBuilder:
    def __init__(
        self,
        content_parser: RecallContentParser | None = None,
        timezone_name: str = "Asia/Shanghai",
    ):
        self.content_parser = content_parser or RecallContentParser()
        self.timezone = ZoneInfo(timezone_name)

    async def build(self, record: ArchivedRecall) -> list[Message]:
        if record.sent_at is None:
            segments: list[Message] = [Text(text="原消息未存储")]
        else:
            segments = await self._content_segments(record)
        segments.append(Text(text=self._attribution(record)))
        return segments

    async def _content_segments(self, record: ArchivedRecall) -> list[Message]:
        parsed = self.content_parser.parse(record.raw_message)
        segments: list[Message] = [Text(text=parsed.text)] if parsed.text else []
        image_count = max(parsed.image_count, len(record.local_image_paths))
        for index in range(image_count):
            path = (
                record.local_image_paths[index]
                if index < len(record.local_image_paths)
                else None
            )
            if path is None:
                segments.append(Text(text="[图片未存储]"))
                continue
            content = await self._read_image(Path(path))
            if content is None:
                segments.append(Text(text="[图片不可用]"))
                continue
            encoded = base64.b64encode(content).decode()
            segments.append(Image(file=f"base64://{encoded}"))
        return segments or [Text(text="消息内容未存储")]

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

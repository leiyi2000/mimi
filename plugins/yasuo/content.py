import html
import re
from typing import ClassVar

from domain import ParsedContent


class RecallContentParser:
    _segment_pattern = re.compile(
        r"\[CQ:(?P<kind>[A-Za-z0-9_-]+)(?:,[^\]]*)?\]",
    )
    _labels: ClassVar[dict[str, str]] = {
        "record": "[语音]",
        "video": "[视频]",
        "file": "[文件]",
        "forward": "[合并转发]",
        "json": "[卡片]",
        "xml": "[卡片]",
        "face": "[表情]",
        "mface": "[表情]",
        "at": "[@成员]",
        "reply": "[回复]",
    }

    def __init__(self, max_text_length: int = 1000):
        self.max_text_length = max_text_length

    def parse(self, raw_message: str | None) -> ParsedContent:
        if not raw_message:
            return ParsedContent(text=None, image_count=0)

        image_count = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal image_count
            kind = match.group("kind").lower()
            if kind == "image":
                image_count += 1
                return ""
            return self._labels.get(kind, "[消息]")

        text = self._segment_pattern.sub(replace, raw_message)
        text = html.unescape(text).strip()
        if not text:
            return ParsedContent(text=None, image_count=image_count)
        if len(text) > self.max_text_length:
            text = f"{text[: self.max_text_length]}…（内容已截断）"
        return ParsedContent(text=text, image_count=image_count)

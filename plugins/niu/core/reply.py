from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, ClassVar

from core.rendering import AutomaticReplyRenderer
from core.tools import (
    Artifact,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
)


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FinalizedReply:
    text: str
    artifacts: tuple[Artifact, ...]


class ReplyPolicy:
    def __init__(
        self,
        automatic_renderer: AutomaticReplyRenderer | None = None,
    ) -> None:
        self.automatic_renderer = automatic_renderer

    async def finalize(
        self,
        *,
        text: str,
        artifacts: tuple[Artifact, ...],
        title_hint: str,
        suppress_text: bool = False,
    ) -> FinalizedReply:
        text = "" if suppress_text else self._strip_urls(text)
        if not text and not artifacts:
            text = "无法直接回复 URL。"
        if self.automatic_renderer is not None and not artifacts:
            try:
                rendered = await self.automatic_renderer.apply(
                    text,
                    title_hint=title_hint,
                )
            except Exception:
                log.exception("automatic rich reply rendering failed")
            else:
                if rendered is not None:
                    text, artifact = rendered
                    artifacts = (artifact,)
        return FinalizedReply(text, artifacts)

    @staticmethod
    def _strip_urls(text: str) -> str:
        text = re.sub(
            r"\[([^\]]+)\]\(https?://[^)\s]+\)",
            r"\1",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"https?://[^\s<>'\"`]+",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\bwww\.[^\s<>'\"`]+",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"[ \t]+([，。！？；：,.!?;:])", r"\1", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


class ReplyTools:
    _MIME_TYPES: ClassVar[dict[str, str]] = {
        "txt": "text/plain",
        "md": "text/markdown",
        "json": "application/json",
        "csv": "text/csv",
    }

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            Tool(
                name="mention_current_sender",
                description=(
                    "Explicitly mention the current sender in the final QQ text "
                    "reply when their attention is genuinely needed."
                ),
                input_schema={"type": "object", "properties": {}},
                handler=self.mention_current_sender,
                skill_name="reply",
            )
        )
        registry.register(
            Tool(
                name="attach_text_file",
                description=(
                    "Attach generated text as a TXT, Markdown, JSON, or CSV file."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "Display filename without directories.",
                        },
                        "format": {
                            "type": "string",
                            "enum": sorted(self._MIME_TYPES),
                        },
                        "content": {"type": "string"},
                    },
                    "required": ["filename", "format", "content"],
                },
                handler=self.attach_text_file,
                read_only=False,
                skill_name="reply",
                completion_required=True,
            )
        )

    async def mention_current_sender(
        self,
        _context: ToolContext,
        _arguments: dict[str, Any],
    ) -> ToolResult:
        return ToolResult(
            "FINAL_REPLY_WILL_MENTION_CURRENT_SENDER",
            mention_sender=True,
        )

    async def attach_text_file(
        self, _context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        filename = str(arguments["filename"]).strip()
        file_format = str(arguments["format"])
        content = str(arguments["content"])
        if (
            not 1 <= len(filename) <= 100
            or file_format not in self._MIME_TYPES
            or not 1 <= len(content) <= 100_000
        ):
            raise ValueError("invalid text file")
        return ToolResult(
            "FILE_ATTACHED",
            artifacts=(
                Artifact(
                    kind="file",
                    data=content.encode("utf-8"),
                    mime_type=self._MIME_TYPES[file_format],
                    title=filename,
                ),
            ),
        )

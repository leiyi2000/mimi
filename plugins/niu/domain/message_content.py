from __future__ import annotations

import base64
import html
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class InputImage:
    data: bytes
    mime_type: str
    label: str
    source: str = "current"


@dataclass(frozen=True, slots=True)
class MentionedMember:
    user_id: int
    nickname: str


@dataclass(frozen=True, slots=True)
class QuotedMessage:
    message_id: str
    sender_id: int
    sender_nickname: str
    text: str
    emojis: tuple[str, ...] = ()
    images: tuple[InputImage, ...] = ()

    def summary(self) -> str:
        parts = [self.text] if self.text else []
        parts.extend(f"[表情：{emoji}]" for emoji in self.emojis)
        parts.extend(
            f"[引用图片 {index}：{image.label}]"
            for index, image in enumerate(self.images)
        )
        return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class UserMessage:
    text: str
    emojis: tuple[str, ...] = ()
    images: tuple[InputImage, ...] = ()
    mentions: tuple[MentionedMember, ...] = ()
    quoted: QuotedMessage | None = None

    @property
    def is_empty(self) -> bool:
        return (
            not self.text
            and not self.emojis
            and not self.images
            and self.quoted is None
        )

    def history_text(self) -> str:
        current = self._current_summary()
        parts = [current] if current else []
        if self.quoted is not None:
            quoted = self.quoted
            parts.append(
                "[引用消息："
                f"{quoted.sender_nickname} (QQ: {quoted.sender_id})]\n"
                f"{quoted.summary()}"
            )
        return "\n".join(parts)

    def _current_summary(self) -> str:
        parts = [self.text] if self.text else []
        parts.extend(f"[表情：{emoji}]" for emoji in self.emojis)
        parts.extend(
            f"[图片 {index}：{image.label}]"
            for index, image in enumerate(self.images)
        )
        return "\n".join(parts)

    def api_content(self, user_id: int, nickname: str) -> list[dict[str, Any]]:
        summary = self._current_summary()
        lines = [
            "<group-message>",
            (
                f'<sender id="{user_id}" '
                f'nickname="{html.escape(nickname, quote=True)}" />'
            ),
        ]
        if self.mentions:
            lines.append("<mentions>")
            lines.extend(
                (
                    f'<member id="{member.user_id}" '
                    f'nickname="{html.escape(member.nickname, quote=True)}" />'
                )
                for member in self.mentions
            )
            lines.append("</mentions>")
        lines.append(f"<content>{html.escape(summary)}</content>")
        if self.quoted is not None:
            quoted = self.quoted
            lines.extend(
                (
                    (
                        f'<quoted-message id="{html.escape(quoted.message_id, quote=True)}" '
                        f'sender-id="{quoted.sender_id}" '
                        "sender-nickname="
                        f'"{html.escape(quoted.sender_nickname, quote=True)}">'
                    ),
                    f"<text>{html.escape(quoted.summary())}</text>",
                    "</quoted-message>",
                )
            )
        all_images = self.images + (
            self.quoted.images if self.quoted is not None else ()
        )
        for index, image in enumerate(all_images):
            lines.append(
                f'<image index="{index}" source="{html.escape(image.source, quote=True)}" '
                f'label="{html.escape(image.label, quote=True)}" />'
            )
        lines.append("</group-message>")
        metadata = "\n".join(lines)
        content: list[dict[str, Any]] = [{"type": "text", "text": metadata}]
        content.extend(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image.mime_type,
                    "data": base64.b64encode(image.data).decode("ascii"),
                },
            }
            for image in all_images
        )
        return content

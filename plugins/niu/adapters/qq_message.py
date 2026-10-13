from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Protocol

from napcat import At, Face, Image, MFace, Reply, Text
from napcat.types.messages import MessageSegment, UnknownMessageSegment

from adapters.napcat_context import (
    NapCatContextAdapter,
    NapCatContextError,
    QuotedPayload,
)
from domain.message_content import (
    InputImage,
    MentionedMember,
    QuotedMessage,
    UserMessage,
)
from providers.media import DownloadedMedia, MediaError


ALLOWED_INPUT_IMAGE_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}


class InputMessageError(RuntimeError):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class Downloader(Protocol):
    async def download(
        self, url: str, allowed_mime_types: set[str]
    ) -> DownloadedMedia: ...

    def validate_image(self, media: DownloadedMedia) -> None: ...


class QQMessageAdapter:
    def __init__(
        self,
        *,
        downloader: Downloader,
        max_images: int,
    ) -> None:
        self.downloader = downloader
        self.max_images = max_images

    @staticmethod
    def text(segments: Sequence[MessageSegment | UnknownMessageSegment]) -> str:
        return "".join(
            segment.text for segment in segments if isinstance(segment, Text)
        )

    @staticmethod
    def mentions(
        segments: Sequence[MessageSegment | UnknownMessageSegment],
        user_id: int,
    ) -> bool:
        return any(
            isinstance(segment, At) and segment.qq == str(user_id)
            for segment in segments
        )

    @staticmethod
    def reply_text(content: str) -> str:
        text = content.strip()
        if "```" in text:
            return text
        return re.sub(r"\s*\r?\n+\s*", " ", text)

    @staticmethod
    def is_passive_sticker(segment: object) -> bool:
        return (
            isinstance(segment, Image)
            and (
                segment.sub_type == 1
                or segment.file.strip().lower() == "marketface"
            )
        )

    async def download_passive_sticker(self, segment: Image) -> InputImage:
        if not self.is_passive_sticker(segment):
            raise ValueError("image is not a passive sticker")
        return await self._download_image(segment, source="passive-sticker")

    async def parse(
        self,
        segments: Sequence[MessageSegment | UnknownMessageSegment],
        *,
        trigger_prefix: str,
        group_id: int,
        bot_id: int,
        context: NapCatContextAdapter,
    ) -> UserMessage:
        replies = [segment for segment in segments if isinstance(segment, Reply)]
        if len(replies) > 1:
            raise InputMessageError("一条消息只能引用一条消息。")
        try:
            text, mentions = await self._ordered_text(
                segments,
                context=context,
                group_id=group_id,
                bot_id=bot_id,
                trigger_prefix=trigger_prefix,
            )
            quoted = (
                await self._quoted_message(
                    replies[0],
                    context=context,
                    group_id=group_id,
                )
                if replies
                else None
            )
        except NapCatContextError:
            raise InputMessageError(
                "引用消息读取失败，请取消引用后重试，或重新发送原内容。"
            ) from None

        current_image_segments = [
            segment for segment in segments if isinstance(segment, Image)
        ]
        quoted_image_segments = (
            [
                segment
                for segment in quoted[1]
                if isinstance(segment, Image)
            ]
            if quoted is not None
            else []
        )
        if len(current_image_segments) + len(quoted_image_segments) > self.max_images:
            raise InputMessageError(
                f"一条消息最多支持 {self.max_images} 张图片。"
            )
        emojis = tuple(
            description
            for segment in segments
            if (description := self._emoji_description(segment)) is not None
        )
        try:
            current_images = await asyncio.gather(
                *(
                    self._download_image(segment, source="current")
                    for segment in current_image_segments
                )
            )
            quoted_message = (
                await self._parse_quoted(
                    quoted[0],
                    quoted[1],
                    quoted_image_segments,
                )
                if quoted is not None
                else None
            )
        except (MediaError, ValueError):
            raise InputMessageError(
                "图片读取失败，请发送 HTTPS 图片，并确认格式和大小符合限制。"
            ) from None
        return UserMessage(
            text=text,
            emojis=emojis,
            images=tuple(current_images),
            mentions=mentions,
            quoted=quoted_message,
        )

    async def _ordered_text(
        self,
        segments: Sequence[MessageSegment | UnknownMessageSegment],
        *,
        context: NapCatContextAdapter,
        group_id: int,
        bot_id: int,
        trigger_prefix: str,
    ) -> tuple[str, tuple[MentionedMember, ...]]:
        parts: list[str] = []
        members: dict[int, MentionedMember] = {}
        for segment in segments:
            if isinstance(segment, Text):
                parts.append(segment.text)
                continue
            if not isinstance(segment, At) or segment.qq == str(bot_id):
                continue
            try:
                user_id = int(segment.qq)
            except ValueError:
                continue
            member = members.get(user_id)
            if member is None:
                nickname = await context.member_name(
                    group_id=group_id,
                    user_id=user_id,
                    fallback=segment.name,
                )
                member = MentionedMember(user_id, nickname)
                members[user_id] = member
            parts.append(f"@{member.nickname} (QQ: {member.user_id})")
        text = "".join(parts).strip()
        if text.startswith(trigger_prefix):
            text = text[len(trigger_prefix) :].lstrip(" \t,，:：")
        return text, tuple(members.values())

    async def _quoted_message(
        self,
        reply: Reply,
        *,
        context: NapCatContextAdapter,
        group_id: int,
    ) -> tuple[
        QuotedPayload,
        tuple[MessageSegment | UnknownMessageSegment, ...],
    ]:
        payload = await context.quoted_message(group_id=group_id, reply=reply)
        return payload, payload.segments

    async def _parse_quoted(
        self,
        payload: QuotedPayload,
        segments: Sequence[MessageSegment | UnknownMessageSegment],
        image_segments: Sequence[Image],
    ) -> QuotedMessage:
        text = "".join(
            (
                segment.text
                if isinstance(segment, Text)
                else (
                    f"@{segment.name or f'QQ用户{segment.qq}'} (QQ: {segment.qq})"
                    if isinstance(segment, At)
                    else ""
                )
            )
            for segment in segments
        ).strip()
        emojis = tuple(
            description
            for segment in segments
            if (description := self._emoji_description(segment)) is not None
        )
        images = await asyncio.gather(
            *(
                self._download_image(segment, source="quoted")
                for segment in image_segments
            )
        )
        return QuotedMessage(
            message_id=payload.message_id,
            sender_id=payload.sender_id,
            sender_nickname=payload.sender_nickname,
            text=text,
            emojis=emojis,
            images=tuple(images),
        )

    async def _download_image(
        self, segment: Image, *, source: str
    ) -> InputImage:
        url = next(
            (
                candidate
                for candidate in (segment.url, segment.file)
                if candidate and candidate.startswith("https://")
            ),
            None,
        )
        if url is None:
            raise MediaError("INPUT_IMAGE_URL_MISSING")
        media = await self.downloader.download(url, ALLOWED_INPUT_IMAGE_TYPES)
        self.downloader.validate_image(media)
        label = (segment.summary or segment.name or "图片").strip()[:100]
        return InputImage(
            media.data,
            media.mime_type,
            label or "图片",
            source=source,
        )

    @staticmethod
    def _emoji_description(
        segment: MessageSegment | UnknownMessageSegment,
    ) -> str | None:
        if isinstance(segment, Face):
            suffix = (
                f"，连击 {segment.chainCount} 次"
                if segment.chainCount and segment.chainCount > 1
                else ""
            )
            return f"QQ 表情 ID {segment.id}{suffix}"
        if isinstance(segment, MFace):
            return segment.summary.strip() or f"商城表情 {segment.emoji_id}"
        return None

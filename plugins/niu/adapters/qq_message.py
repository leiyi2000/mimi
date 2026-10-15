from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class _RecentImages:
    segments: tuple[Image, ...]
    stored_at: float


class QQMessageAdapter:
    def __init__(
        self,
        *,
        downloader: Downloader,
        max_images: int,
        recent_image_ttl: float = 300,
    ) -> None:
        if recent_image_ttl <= 0:
            raise ValueError("recent image ttl must be positive")
        self.downloader = downloader
        self.max_images = max_images
        self.recent_image_ttl = recent_image_ttl
        self._recent_images: dict[tuple[int, int], _RecentImages] = {}

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
    def has_reply(
        segments: Sequence[MessageSegment | UnknownMessageSegment],
    ) -> bool:
        return any(isinstance(segment, Reply) for segment in segments)

    @staticmethod
    def has_images(
        segments: Sequence[MessageSegment | UnknownMessageSegment],
    ) -> bool:
        return any(isinstance(segment, Image) for segment in segments)

    @classmethod
    def has_emojis(
        cls,
        segments: Sequence[MessageSegment | UnknownMessageSegment],
    ) -> bool:
        return any(cls._emoji_description(segment) is not None for segment in segments)

    @staticmethod
    def only_mentions_others(
        segments: Sequence[MessageSegment | UnknownMessageSegment],
        bot_id: int,
    ) -> bool:
        mentions_other = any(
            isinstance(segment, At) and segment.qq != str(bot_id)
            for segment in segments
        )
        if not mentions_other or any(isinstance(segment, Reply) for segment in segments):
            return False
        return not any(
            (
                isinstance(segment, Text) and bool(segment.text.strip())
            )
            or isinstance(segment, (Face, Image, MFace))
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

    def remember_recent_images(
        self,
        segments: Sequence[MessageSegment | UnknownMessageSegment],
        *,
        group_id: int,
        user_id: int,
    ) -> None:
        self._purge_recent_images()
        images = tuple(
            segment for segment in segments if isinstance(segment, Image)
        )
        if images:
            self._recent_images[(group_id, user_id)] = _RecentImages(
                segments=images[-self.max_images :],
                stored_at=time.monotonic(),
            )

    async def parse(
        self,
        segments: Sequence[MessageSegment | UnknownMessageSegment],
        *,
        trigger_prefix: str,
        group_id: int,
        bot_id: int,
        user_id: int | None = None,
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
        recent_key = (
            (group_id, user_id)
            if user_id is not None
            and not current_image_segments
            and quoted is None
            else None
        )
        recent_image_segments = (
            self._get_recent_images(recent_key) if recent_key is not None else ()
        )
        if recent_image_segments:
            current_image_segments = list(recent_image_segments)
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
                    self._download_image(
                        segment,
                        source="recent" if recent_image_segments else "current",
                    )
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
        if recent_image_segments and recent_key is not None:
            self._recent_images.pop(recent_key, None)
        return UserMessage(
            text=text,
            emojis=emojis,
            images=tuple(current_images),
            mentions=mentions,
            quoted=quoted_message,
        )

    def _get_recent_images(
        self,
        key: tuple[int, int],
    ) -> tuple[Image, ...]:
        self._purge_recent_images()
        batch = self._recent_images.get(key)
        return batch.segments if batch is not None else ()

    def _purge_recent_images(self) -> None:
        cutoff = time.monotonic() - self.recent_image_ttl
        self._recent_images = {
            key: batch
            for key, batch in self._recent_images.items()
            if batch.stored_at >= cutoff
        }

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

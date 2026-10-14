from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from core.tools import (
    Artifact,
    Tool,
    ToolContext,
    ToolImage,
    ToolRegistry,
    ToolResult,
)


class StickerStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Sticker:
    id: str
    kind: str
    summary: str
    emotion: str
    tags: tuple[str, ...]
    created_by: int
    created_at: int
    use_count: int = 0
    last_used_at: int = 0
    file_name: str = ""
    mime_type: str = ""
    emoji_package_id: int = 0
    emoji_id: str = ""
    key: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "summary": self.summary,
            "emotion": self.emotion,
            "tags": list(self.tags),
            "use_count": self.use_count,
        }


@dataclass(slots=True)
class StickerIndex:
    records: dict[str, Sticker]
    quota_date: str = ""
    daily_updates: int = 0
    daily_classifications: int = 0


class StickerStore:
    def __init__(
        self,
        root: Path,
        *,
        max_items: int,
        daily_update_limit: int,
        daily_classification_limit: int = 5,
        today: Callable[[], date] | None = None,
        choose: Callable[[Sequence[Sticker]], Sticker] | None = None,
    ) -> None:
        self.root = root / "stickers" / "groups"
        self.max_items = max_items
        self.daily_update_limit = daily_update_limit
        self.daily_classification_limit = daily_classification_limit
        self.today = today or date.today
        self.choose = choose or random.choice
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, group_id: int) -> asyncio.Lock:
        return self._locks.setdefault(group_id, asyncio.Lock())

    def _group_dir(self, group_id: int) -> Path:
        if group_id < 0:
            raise ValueError("invalid group id")
        return self.root / str(group_id)

    def _reset_daily_quota(self, index: StickerIndex) -> None:
        current_date = self.today().isoformat()
        if index.quota_date != current_date:
            index.quota_date = current_date
            index.daily_updates = 0
            index.daily_classifications = 0

    def _consume_daily_update(self, index: StickerIndex) -> None:
        self._reset_daily_quota(index)
        if index.daily_updates >= self.daily_update_limit:
            raise StickerStoreError("STICKER_DAILY_UPDATE_LIMIT")
        index.daily_updates += 1

    def _evict_for_insert(self, index: StickerIndex) -> Sticker | None:
        if len(index.records) < self.max_items:
            return None
        victim = self.choose(tuple(index.records.values()))
        del index.records[victim.id]
        return victim

    async def _delete_sticker_file(
        self,
        group_id: int,
        sticker: Sticker | None,
    ) -> None:
        if sticker is None or not sticker.file_name:
            return
        path = self._group_dir(group_id) / sticker.file_name
        await asyncio.to_thread(path.unlink, True)

    async def collect_mface(
        self,
        *,
        group_id: int,
        user_id: int,
        emoji_package_id: int,
        emoji_id: str,
        key: str,
        summary: str,
    ) -> bool:
        digest = hashlib.sha256(
            f"{emoji_package_id}:{emoji_id}".encode()
        ).hexdigest()[:24]
        sticker = Sticker(
            id=f"mface-{digest}",
            kind="mface",
            summary=summary.strip()[:200] or f"商城表情 {emoji_id}",
            emotion="",
            tags=(),
            created_by=user_id,
            created_at=int(time.time()),
            emoji_package_id=emoji_package_id,
            emoji_id=emoji_id,
            key=key,
        )
        async with self._lock(group_id):
            index = await self._load(group_id)
            existing = index.records.get(sticker.id)
            if existing is not None:
                if existing.key != key or existing.summary != sticker.summary:
                    index.records[sticker.id] = replace(
                        existing,
                        key=key,
                        summary=sticker.summary,
                    )
                    await self._save(group_id, index)
                return False
            self._consume_daily_update(index)
            victim = self._evict_for_insert(index)
            index.records[sticker.id] = sticker
            await self._save(group_id, index)
            await self._delete_sticker_file(group_id, victim)
        return True

    async def collect_image(
        self,
        *,
        context: ToolContext,
        image_index: int,
        summary: str,
        emotion: str,
        tags: tuple[str, ...],
    ) -> Sticker:
        if not 0 <= image_index < len(context.images):
            raise ValueError("invalid image index")
        image = context.images[image_index]
        return await self._collect_image(
            group_id=context.group_id,
            user_id=context.user_id,
            image=image,
            summary=summary,
            emotion=emotion,
            tags=tags,
        )

    async def collect_passive_image(
        self,
        *,
        group_id: int,
        user_id: int,
        image: ToolImage,
    ) -> Sticker:
        return await self._collect_image(
            group_id=group_id,
            user_id=user_id,
            image=image,
            summary=image.label or "QQ 表情",
            emotion="",
            tags=(),
        )

    async def reserve_classification(self, group_id: int) -> bool:
        async with self._lock(group_id):
            index = await self._load(group_id)
            self._reset_daily_quota(index)
            if (
                index.daily_classifications
                >= self.daily_classification_limit
            ):
                return False
            index.daily_classifications += 1
            await self._save(group_id, index)
        return True

    async def apply_classification(
        self,
        *,
        group_id: int,
        sticker_id: str,
        summary: str,
        emotion: str,
        tags: tuple[str, ...],
    ) -> Sticker:
        summary = summary.strip()
        emotion = emotion.strip()
        tags = tuple(
            dict.fromkeys(tag.strip()[:40] for tag in tags if tag.strip())
        )[:12]
        if not 1 <= len(summary) <= 200 or len(emotion) > 80:
            raise ValueError("invalid sticker metadata")
        async with self._lock(group_id):
            index = await self._load(group_id)
            current = index.records.get(sticker_id)
            if current is None:
                raise StickerStoreError("STICKER_NOT_FOUND")
            if current.kind != "image":
                raise StickerStoreError("STICKER_NOT_CLASSIFIABLE")
            updated = replace(
                current,
                summary=summary,
                emotion=emotion,
                tags=tags,
            )
            index.records[sticker_id] = updated
            await self._save(group_id, index)
        return updated

    async def pending_classifications(
        self,
    ) -> list[tuple[int, Sticker, ToolImage]]:
        if not self.root.exists():
            return []
        pending: list[tuple[int, Sticker, ToolImage]] = []
        for group_dir in sorted(self.root.iterdir()):
            if not group_dir.is_dir() or not group_dir.name.isdigit():
                continue
            group_id = int(group_dir.name)
            async with self._lock(group_id):
                index = await self._load(group_id)
                self._reset_daily_quota(index)
                remaining = max(
                    0,
                    self.daily_classification_limit
                    - index.daily_classifications,
                )
                candidates = [
                    sticker
                    for sticker in index.records.values()
                    if self.needs_classification(sticker)
                ][:remaining]
            for sticker in candidates:
                path = group_dir / sticker.file_name
                try:
                    data = await asyncio.to_thread(path.read_bytes)
                except OSError:
                    continue
                pending.append(
                    (
                        group_id,
                        sticker,
                        ToolImage(
                            data,
                            sticker.mime_type,
                            sticker.summary,
                        ),
                    )
                )
        return pending

    @staticmethod
    def needs_classification(sticker: Sticker) -> bool:
        return (
            sticker.kind == "image"
            and bool(sticker.file_name)
            and not sticker.emotion
            and not sticker.tags
        )

    async def _collect_image(
        self,
        *,
        group_id: int,
        user_id: int,
        image: ToolImage,
        summary: str,
        emotion: str,
        tags: tuple[str, ...],
    ) -> Sticker:
        summary = summary.strip()
        emotion = emotion.strip()
        tags = tuple(
            dict.fromkeys(tag.strip()[:40] for tag in tags if tag.strip())
        )[:12]
        if not 1 <= len(summary) <= 200 or len(emotion) > 80:
            raise ValueError("invalid sticker metadata")
        digest = hashlib.sha256(image.data).hexdigest()
        sticker_id = f"image-{digest[:24]}"
        extension = {
            "image/gif": ".gif",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }.get(image.mime_type)
        if extension is None:
            raise ValueError("unsupported sticker image")
        async with self._lock(group_id):
            index = await self._load(group_id)
            existing = index.records.get(sticker_id)
            if existing is not None:
                return existing
            self._consume_daily_update(index)
            victim = self._evict_for_insert(index)
            group_dir = self._group_dir(group_id)
            group_dir.mkdir(parents=True, exist_ok=True)
            file_name = f"{sticker_id}{extension}"
            path = group_dir / file_name
            temporary = path.with_suffix(f"{path.suffix}.tmp-{uuid.uuid4().hex}")
            try:
                await asyncio.to_thread(temporary.write_bytes, image.data)
                await asyncio.to_thread(os.replace, temporary, path)
                sticker = Sticker(
                    id=sticker_id,
                    kind="image",
                    summary=summary,
                    emotion=emotion,
                    tags=tags,
                    created_by=user_id,
                    created_at=int(time.time()),
                    file_name=file_name,
                    mime_type=image.mime_type,
                )
                index.records[sticker.id] = sticker
                await self._save(group_id, index)
                await self._delete_sticker_file(group_id, victim)
            except Exception:
                temporary.unlink(missing_ok=True)
                path.unlink(missing_ok=True)
                raise
        return sticker

    async def find(
        self, group_id: int, query: str, limit: int
    ) -> list[Sticker]:
        records = list((await self._load(group_id)).records.values())
        terms = tuple(
            dict.fromkeys(part.lower() for part in query.split() if part)
        )

        def rank(sticker: Sticker) -> tuple[int, int, int]:
            text = " ".join(
                (sticker.summary, sticker.emotion, *sticker.tags)
            ).lower()
            matches = sum(term in text for term in terms)
            return matches, -sticker.use_count, sticker.created_at

        records.sort(key=rank, reverse=True)
        return records[:limit]

    async def update(
        self,
        *,
        group_id: int,
        sticker_id: str,
        summary: str,
        emotion: str,
        tags: tuple[str, ...],
    ) -> Sticker:
        summary = summary.strip()
        emotion = emotion.strip()
        tags = tuple(
            dict.fromkeys(tag.strip()[:40] for tag in tags if tag.strip())
        )[:12]
        if not 1 <= len(summary) <= 200 or len(emotion) > 80:
            raise ValueError("invalid sticker metadata")
        async with self._lock(group_id):
            index = await self._load(group_id)
            current = index.records.get(sticker_id)
            if current is None:
                raise StickerStoreError("STICKER_NOT_FOUND")
            updated = replace(
                current,
                summary=summary,
                emotion=emotion,
                tags=tags,
            )
            if updated == current:
                return current
            self._consume_daily_update(index)
            index.records[sticker_id] = updated
            await self._save(group_id, index)
        return updated

    async def artifact(self, group_id: int, sticker_id: str) -> Artifact:
        async with self._lock(group_id):
            index = await self._load(group_id)
            sticker = index.records.get(sticker_id)
            if sticker is None:
                raise StickerStoreError("STICKER_NOT_FOUND")
            now = int(time.time())
            index.records[sticker_id] = replace(
                sticker,
                use_count=sticker.use_count + 1,
                last_used_at=now,
            )
            await self._save(group_id, index)
        if sticker.kind == "image":
            path = self._group_dir(group_id) / sticker.file_name
            try:
                data = await asyncio.to_thread(path.read_bytes)
            except OSError as exc:
                raise StickerStoreError("STICKER_FILE_MISSING") from exc
            return Artifact("image", data, sticker.mime_type, sticker.summary)
        payload = json.dumps(
            {
                "emoji_package_id": sticker.emoji_package_id,
                "emoji_id": sticker.emoji_id,
                "key": sticker.key,
                "summary": sticker.summary,
            },
            ensure_ascii=False,
        ).encode()
        return Artifact("mface", payload, "application/json", sticker.summary)

    async def delete(self, group_id: int, sticker_id: str) -> bool:
        async with self._lock(group_id):
            index = await self._load(group_id)
            sticker = index.records.get(sticker_id)
            if sticker is None:
                return False
            self._consume_daily_update(index)
            del index.records[sticker_id]
            await self._save(group_id, index)
            if sticker.file_name:
                path = self._group_dir(group_id) / sticker.file_name
                await asyncio.to_thread(path.unlink, True)
        return True

    async def _load(self, group_id: int) -> StickerIndex:
        path = self._group_dir(group_id) / "index.json"
        if not path.exists():
            return StickerIndex({})
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            payload = json.loads(raw)
            if isinstance(payload, list):
                items = payload
                quota_date = ""
                daily_updates = 0
                daily_classifications = 0
            elif isinstance(payload, dict):
                items = payload["stickers"]
                quota = payload["quota"]
                quota_date = str(quota["date"])
                daily_updates = int(quota["updates"])
                daily_classifications = int(
                    quota.get("classifications", 0)
                )
            else:
                raise TypeError("invalid sticker index")
            if (
                not isinstance(items, list)
                or daily_updates < 0
                or daily_classifications < 0
                or len(items) > self.max_items
            ):
                raise TypeError("invalid sticker index")
            records = {
                str(item["id"]): Sticker(
                    **{
                        **item,
                        "tags": tuple(item.get("tags", ())),
                    }
                )
                for item in items
            }
            return StickerIndex(
                records=records,
                quota_date=quota_date,
                daily_updates=daily_updates,
                daily_classifications=daily_classifications,
            )
        except (
            OSError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise StickerStoreError("STICKER_INDEX_CORRUPTED") from exc

    async def _save(self, group_id: int, index: StickerIndex) -> None:
        group_dir = self._group_dir(group_id)
        group_dir.mkdir(parents=True, exist_ok=True)
        path = group_dir / "index.json"
        temporary = path.with_suffix(f".json.tmp-{uuid.uuid4().hex}")
        content = json.dumps(
            {
                "schema_version": 2,
                "quota": {
                    "date": index.quota_date,
                    "updates": index.daily_updates,
                    "classifications": index.daily_classifications,
                },
                "stickers": [
                    asdict(record) for record in index.records.values()
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await asyncio.to_thread(temporary.write_text, content, encoding="utf-8")
        await asyncio.to_thread(os.replace, temporary, path)


class StickerTools:
    def __init__(self, store: StickerStore) -> None:
        self.store = store

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            Tool(
                name="collect_current_image_as_sticker",
                description=(
                    "Save one image from the current message as a reusable "
                    "group sticker after visually identifying it."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "image_index": {"type": "integer", "minimum": 0},
                        "summary": {"type": "string", "maxLength": 200},
                        "emotion": {"type": "string", "maxLength": 80},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 12,
                        },
                    },
                    "required": [
                        "image_index",
                        "summary",
                        "emotion",
                        "tags",
                    ],
                },
                handler=self.collect,
                read_only=False,
                skill_name="stickers",
                completion_required=True,
            )
        )
        registry.register(
            Tool(
                name="find_group_stickers",
                description=(
                    "Find reusable stickers from the current group's library. "
                    "Choose the final sticker yourself from returned metadata."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 30,
                        },
                    },
                    "required": ["query"],
                },
                handler=self.find,
                skill_name="stickers",
            )
        )
        registry.register(
            Tool(
                name="send_group_sticker",
                description="Send one chosen sticker from the current group library.",
                input_schema={
                    "type": "object",
                    "properties": {"sticker_id": {"type": "string"}},
                    "required": ["sticker_id"],
                },
                handler=self.send,
                read_only=False,
                skill_name="stickers",
                completion_required=True,
            )
        )
        registry.register(
            Tool(
                name="organize_group_sticker",
                description="Correct the summary, emotion, and tags of a sticker.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "sticker_id": {"type": "string"},
                        "summary": {"type": "string", "maxLength": 200},
                        "emotion": {"type": "string", "maxLength": 80},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 12,
                        },
                    },
                    "required": [
                        "sticker_id",
                        "summary",
                        "emotion",
                        "tags",
                    ],
                },
                handler=self.organize,
                read_only=False,
                skill_name="stickers",
                completion_required=True,
            )
        )
        registry.register(
            Tool(
                name="delete_group_sticker",
                description="Delete a broken, duplicate, or explicitly unwanted sticker.",
                input_schema={
                    "type": "object",
                    "properties": {"sticker_id": {"type": "string"}},
                    "required": ["sticker_id"],
                },
                handler=self.delete,
                read_only=False,
                skill_name="stickers",
                completion_required=True,
            )
        )

    async def collect(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        try:
            sticker = await self.store.collect_image(
                context=context,
                image_index=int(arguments["image_index"]),
                summary=str(arguments["summary"]),
                emotion=str(arguments["emotion"]),
                tags=tuple(str(tag) for tag in arguments["tags"]),
            )
        except StickerStoreError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(f"STICKER_COLLECTED id={sticker.id}")

    async def find(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        stickers = await self.store.find(
            context.group_id,
            str(arguments["query"]),
            int(arguments.get("limit", 20)),
        )
        return ToolResult(
            json.dumps(
                [sticker.public_dict() for sticker in stickers],
                ensure_ascii=False,
            )
        )

    async def send(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        try:
            artifact = await self.store.artifact(
                context.group_id, str(arguments["sticker_id"])
            )
        except StickerStoreError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult("STICKER_READY", artifacts=(artifact,))

    async def organize(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        try:
            sticker = await self.store.update(
                group_id=context.group_id,
                sticker_id=str(arguments["sticker_id"]),
                summary=str(arguments["summary"]),
                emotion=str(arguments["emotion"]),
                tags=tuple(str(tag) for tag in arguments["tags"]),
            )
        except StickerStoreError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(f"STICKER_UPDATED id={sticker.id}")

    async def delete(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        try:
            deleted = await self.store.delete(
                context.group_id, str(arguments["sticker_id"])
            )
        except StickerStoreError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult("STICKER_DELETED" if deleted else "STICKER_NOT_FOUND")

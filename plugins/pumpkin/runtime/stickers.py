from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, UnidentifiedImageError

from core.domain import (
    MessageRecord,
    MessageSegmentType,
    OutboxStatus,
    StickerAsset,
    StickerAssetStatus,
    StickerCandidate,
    StickerCandidateStatus,
    StickerCategory,
    StickerSafety,
    ToolContext,
    ToolResult,
)
from persistence.repositories import StickerStore
from providers.media import MediaProviderError, MessageMediaProvider
from providers.stickers import StickerClassifier, StickerClassifierError
from runtime.outbox import OutboxSender
from runtime.reply_ledger import TurnReplyLedger
from runtime.tool_results import error, ok


MAX_ACTIVE_STICKERS = 50
MAX_DAILY_REPLACEMENTS = 10
MAX_STAGED_STICKERS = 100
MAX_CANDIDATE_ATTEMPTS = 3
MAX_STICKER_IMAGE_SIDE = 16_384
MAX_STICKER_IMAGE_PIXELS = 25_000_000
WORKER_LEASE_SECONDS = 300
WORKER_IDLE_SECONDS = 60
STICKER_TIMEZONE = ZoneInfo("Asia/Shanghai")

_EXTENSIONS = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class StickerService:
    def __init__(
        self,
        *,
        store: StickerStore,
        media_provider: MessageMediaProvider,
        classifier: StickerClassifier,
        outbox: OutboxSender,
        reply_ledger: TurnReplyLedger,
        data_dir: str,
        idempotency_key,
    ) -> None:
        self.store = store
        self.media_provider = media_provider
        self.classifier = classifier
        self.outbox = outbox
        self.reply_ledger = reply_ledger
        self.data_dir = Path(data_dir).resolve()
        self.root = self.data_dir / "stickers"
        self.idempotency_key = idempotency_key
        self._wake = asyncio.Event()
        self._state_lock = asyncio.Lock()
        self._asset_lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._closing = False
        self._failure: BaseException | None = None
        self._idle = asyncio.Event()
        self._idle.set()

    async def start(self) -> None:
        self._closing = False
        for name in ("active", "staged", "archive", "quarantine"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        await self.reconcile_files()
        await self.store.recover_sticker_candidates(
            claimed_before=datetime.now(UTC)
            - timedelta(seconds=WORKER_LEASE_SECONDS)
        )
        await self.wake()

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def wake(self) -> None:
        async with self._state_lock:
            if self._closing:
                return
            self._wake.set()
            self._idle.clear()
            if self._worker is None or self._worker.done():
                self._failure = None
                self._worker = asyncio.create_task(
                    self._run(),
                    name="pumpkin-sticker-collector",
                )

    def raise_background_failure(self) -> None:
        if self._failure is not None:
            raise self._failure

    async def when_idle(self) -> None:
        await self._idle.wait()
        self.raise_background_failure()

    async def enqueue(self, record: MessageRecord) -> int:
        count = await self.store.enqueue_sticker_candidates(record.message)
        if count:
            await self.wake()
        return count

    async def _run(self) -> None:
        try:
            while not self._closing:
                processed = await self.process_once()
                if processed:
                    continue
                async with self._state_lock:
                    self._wake.clear()
                if await self.process_once():
                    continue
                async with self._state_lock:
                    if self._wake.is_set():
                        continue
                    self._idle.set()
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=WORKER_IDLE_SECONDS,
                    )
                except TimeoutError:
                    pass
                if self._wake.is_set():
                    self._idle.clear()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._failure = exc
            self._idle.set()
            raise

    async def process_once(self) -> bool:
        candidate = await self.store.claim_sticker_candidate(
            now=datetime.now(UTC)
        )
        if candidate is None:
            return await self._promote_staged_once()
        await self._process_candidate(candidate)
        return True

    async def _process_candidate(self, candidate: StickerCandidate) -> None:
        record = await self.store.get_archived_message(
            candidate.scope_id,
            candidate.source_message_id,
        )
        if (
            record is None
            or candidate.segment_index >= len(record.message.segments)
        ):
            await self._finish_failed(
                candidate,
                "STICKER_SOURCE_NOT_FOUND",
                retryable=False,
            )
            return
        segment = record.message.segments[candidate.segment_index]
        try:
            fetched = await self.media_provider.fetch(
                candidate.segment_type.value,
                segment.data,
                preserve_animation=True,
            )
            perceptual_hash, preview = await asyncio.to_thread(
                _image_preview,
                fetched.content,
            )
        except MediaProviderError as exc:
            await self._finish_failed(
                candidate,
                exc.code,
                retryable=exc.retryable,
            )
            return
        except ValueError:
            await self._finish_failed(
                candidate,
                "STICKER_MEDIA_INVALID",
                retryable=False,
            )
            return

        content_sha256 = hashlib.sha256(fetched.content).hexdigest()
        async with self._asset_lock:
            if await self.store.is_sticker_removed(
                content_sha256,
                perceptual_hash,
            ):
                await self.store.finish_sticker_candidate(
                    candidate.candidate_id,
                    status=StickerCandidateStatus.REJECTED.value,
                    error_code="STICKER_REMOVED_BY_REQUEST",
                )
                return
            duplicate = await self.store.get_sticker_by_hash(
                content_sha256,
                perceptual_hash,
            )
            if duplicate is not None:
                duplicate = await self.store.record_sticker_seen(
                    duplicate.sticker_id,
                    scope_id=candidate.scope_id,
                    seen_at=datetime.now(UTC),
                )
                await self.store.finish_sticker_candidate(
                    candidate.candidate_id,
                    status=StickerCandidateStatus.ACCEPTED.value,
                    asset_id=duplicate.sticker_id,
                )
        if duplicate is not None:
            if duplicate.status is StickerAssetStatus.STAGED:
                await self._promote_asset(duplicate)
            return

        try:
            classification = await self.classifier.classify(
                preview,
                content_type="image/png",
            )
        except StickerClassifierError as exc:
            await self._finish_failed(
                candidate,
                exc.code,
                retryable=exc.retryable,
            )
            return
        if classification.safety is not StickerSafety.ALLOWED:
            await self.store.finish_sticker_candidate(
                candidate.candidate_id,
                status=StickerCandidateStatus.REJECTED.value,
                error_code=classification.safety.value,
            )
            return

        extension = _EXTENSIONS.get(fetched.content_type)
        if extension is None:
            await self._finish_failed(
                candidate,
                "STICKER_MEDIA_INVALID",
                retryable=False,
            )
            return
        sticker_id = uuid.uuid4().hex
        staged_path = self.root / "staged" / f"{sticker_id}{extension}"
        async with self._asset_lock:
            if await self.store.is_sticker_removed(
                content_sha256,
                perceptual_hash,
            ):
                await self.store.finish_sticker_candidate(
                    candidate.candidate_id,
                    status=StickerCandidateStatus.REJECTED.value,
                    error_code="STICKER_REMOVED_BY_REQUEST",
                )
                return
            duplicate = await self.store.get_sticker_by_hash(
                content_sha256,
                perceptual_hash,
            )
            if duplicate is not None:
                duplicate = await self.store.record_sticker_seen(
                    duplicate.sticker_id,
                    scope_id=candidate.scope_id,
                    seen_at=datetime.now(UTC),
                )
                await self.store.finish_sticker_candidate(
                    candidate.candidate_id,
                    status=StickerCandidateStatus.ACCEPTED.value,
                    asset_id=duplicate.sticker_id,
                )
                asset = duplicate
            else:
                await asyncio.to_thread(
                    _atomic_write,
                    staged_path,
                    fetched.content,
                )
                try:
                    asset = await self.store.create_sticker_asset(
                        sticker_id=sticker_id,
                        content_sha256=content_sha256,
                        perceptual_hash=perceptual_hash,
                        content_type=fetched.content_type,
                        file_extension=extension,
                        relative_path=self._relative(staged_path),
                        classification=classification,
                        scope_id=candidate.scope_id,
                        seen_at=datetime.now(UTC),
                    )
                except BaseException:
                    staged_path.unlink(missing_ok=True)
                    raise
                await self.store.finish_sticker_candidate(
                    candidate.candidate_id,
                    status=StickerCandidateStatus.ACCEPTED.value,
                    asset_id=asset.sticker_id,
                )
        await self._promote_asset(asset)
        await self._prune_staged()

    async def _finish_failed(
        self,
        candidate: StickerCandidate,
        code: str,
        *,
        retryable: bool,
    ) -> None:
        can_retry = retryable and candidate.attempt_count < MAX_CANDIDATE_ATTEMPTS
        delay = 2 ** max(0, candidate.attempt_count - 1)
        await self.store.finish_sticker_candidate(
            candidate.candidate_id,
            status=(
                StickerCandidateStatus.RETRYABLE_FAILED.value
                if can_retry
                else StickerCandidateStatus.TERMINAL_FAILED.value
            ),
            error_code=code,
            next_attempt_at=(
                datetime.now(UTC) + timedelta(seconds=delay)
                if can_retry
                else None
            ),
        )

    async def _promote_staged_once(self) -> bool:
        staged = await self.store.list_staged_stickers()
        for asset in staged:
            if await self._promote_asset(asset):
                return True
        return False

    async def _promote_asset(self, candidate: StickerAsset) -> bool:
        async with self._asset_lock:
            current = await self.store.get_sticker(candidate.sticker_id)
            if (
                current is None
                or current.status is not StickerAssetStatus.STAGED
            ):
                return False
            return await self._promote_asset_locked(current)

    async def _promote_asset_locked(self, candidate: StickerAsset) -> bool:
        active = await self.store.list_active_stickers()
        now = datetime.now(UTC)
        active_path = self.root / "active" / (
            f"{candidate.sticker_id}{candidate.file_extension}"
        )
        staged_path = self._absolute(candidate.relative_path)
        if len(active) < MAX_ACTIVE_STICKERS:
            await asyncio.to_thread(_atomic_move, staged_path, active_path)
            await self.store.activate_sticker(
                candidate.sticker_id,
                relative_path=self._relative(active_path),
                activated_at=now,
            )
            return True

        local_date = now.astimezone(STICKER_TIMEZONE).date().isoformat()
        if (
            await self.store.sticker_replacement_count(local_date)
            >= MAX_DAILY_REPLACEMENTS
        ):
            return False
        victim = _replacement_victim(candidate, active)
        if victim is None:
            return False
        archive_path = (
            self.root
            / "archive"
            / local_date
            / f"{victim.sticker_id}{victim.file_extension}"
        )
        victim_path = self._absolute(victim.relative_path)
        await asyncio.to_thread(_atomic_move, victim_path, archive_path)
        try:
            await asyncio.to_thread(_atomic_move, staged_path, active_path)
            await self.store.replace_sticker(
                activated_sticker_id=candidate.sticker_id,
                activated_relative_path=self._relative(active_path),
                archived_sticker_id=victim.sticker_id,
                archived_relative_path=self._relative(archive_path),
                local_date=local_date,
                reason="higher_quality_or_category_coverage",
                changed_at=now,
            )
        except BaseException:
            if archive_path.exists() and not victim_path.exists():
                await asyncio.to_thread(_atomic_move, archive_path, victim_path)
            if active_path.exists() and not staged_path.exists():
                await asyncio.to_thread(_atomic_move, active_path, staged_path)
            raise
        return True

    async def reconcile_files(self) -> None:
        for asset in await self.store.list_sticker_assets():
            if asset.status is StickerAssetStatus.REMOVED:
                matches = tuple(self.root.glob(f"**/{asset.sticker_id}.*"))
                for match in matches:
                    await asyncio.to_thread(_unlink_file, match)
                continue
            expected = self._absolute(asset.relative_path)
            if expected.exists():
                continue
            matches = tuple(self.root.glob(f"**/{asset.sticker_id}.*"))
            if len(matches) == 1:
                await asyncio.to_thread(_atomic_move, matches[0], expected)

    async def _prune_staged(self) -> None:
        staged = await self.store.list_staged_stickers()
        for asset in staged[MAX_STAGED_STICKERS:]:
            path = self._absolute(asset.relative_path)
            if await self.store.delete_staged_sticker(asset.sticker_id):
                path.unlink(missing_ok=True)

    async def search_stickers(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        del context
        try:
            category = StickerCategory(str(arguments["category"]))
        except (KeyError, ValueError):
            return error("invalid sticker category", "INVALID_STICKER_CATEGORY")
        limit = max(1, min(int(arguments.get("limit", 6)), 8))
        requested_tags = {
            str(item).strip().casefold()
            for item in arguments.get("tags", [])
            if str(item).strip()
        }
        assets = await self.store.list_active_stickers(category=category)
        if requested_tags:
            assets = tuple(
                asset
                for asset in assets
                if requested_tags.intersection(
                    item.casefold() for item in asset.tags
                )
            )
        if not assets:
            return error("no matching active sticker", "STICKER_NOT_FOUND")
        return ok(
            {
                "stickers": [
                    {
                        "sticker_id": asset.sticker_id,
                        "category": asset.category.value,
                        "tags": list(asset.tags),
                        "summary": asset.summary,
                        "tone": asset.tone,
                        "recently_used": asset.last_used_at is not None,
                    }
                    for asset in assets[:limit]
                ]
            }
        )

    async def send_sticker(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        sticker_id = str(arguments.get("sticker_id") or "").strip()
        key = self.idempotency_key(
            context,
            "sticker",
            str(arguments["idempotency_key"]),
        )
        cached = await self.store.idempotency_get(key)
        if cached is not None:
            message_ids = tuple(
                str(item) for item in cached.get("message_ids", [])
            )
            self.reply_ledger.record(context.run_id, message_ids)
            return ok(cached)
        if await self.store.get_sticker_send_for_run(context.run_id) is not None:
            return error(
                "one sticker has already been sent in this turn",
                "STICKER_TURN_LIMIT_REACHED",
            )
        if (
            len(self.reply_ledger.message_ids(context.run_id)) + 1
            > context.max_reply_messages
        ):
            return error(
                "reply message limit exceeded for this turn",
                "REPLY_BUDGET_EXCEEDED",
            )
        async with self._asset_lock:
            return await self._send_sticker_locked(
                context,
                sticker_id=sticker_id,
                key=key,
            )

    async def _send_sticker_locked(
        self,
        context: ToolContext,
        *,
        sticker_id: str,
        key: str,
    ) -> ToolResult:
        asset = await self.store.get_sticker(sticker_id)
        if asset is None or asset.status is not StickerAssetStatus.ACTIVE:
            return error("sticker is not active", "STICKER_NOT_ACTIVE")
        path = self._absolute(asset.relative_path)
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest()
            != asset.content_sha256
        ):
            quarantine = (
                self.root
                / "quarantine"
                / f"{asset.sticker_id}{asset.file_extension}"
            )
            if path.exists():
                await asyncio.to_thread(_atomic_move, path, quarantine)
            await self.store.quarantine_sticker(
                asset.sticker_id,
                relative_path=self._relative(quarantine),
            )
            return error("sticker asset is invalid", "STICKER_ASSET_INVALID")
        item = await self.outbox.submit(
            idempotency_key=key,
            scope_id=context.scope_id,
            source_event_id=context.run_id,
            payload={
                "text": "",
                "original_text": "",
                "segments": [{"type": "image", "file": str(path)}],
                "source": "sticker",
            },
        )
        sent_at = datetime.now(UTC) if item.status is OutboxStatus.SENT else None
        await self.store.record_sticker_send(
            run_id=context.run_id,
            sticker_id=asset.sticker_id,
            scope_id=context.scope_id,
            outbox_id=item.outbox_id,
            status=item.status.value,
            sent_at=sent_at,
        )
        if item.status is OutboxStatus.FAILED:
            return error("sticker send failed", "OUTBOX_SEND_FAILED")
        if item.status is OutboxStatus.UNKNOWN:
            return error(
                "sticker send outcome is unknown; it will not be retried",
                "OUTBOX_SEND_UNKNOWN",
            )
        if item.status is not OutboxStatus.SENT:
            return error("outbox did not reach a terminal state", "OUTBOX_INVALID")
        content = {
            "sent": True,
            "sticker_id": asset.sticker_id,
            "message_ids": list(item.message_ids),
        }
        await self.store.idempotency_put(key, content)
        self.reply_ledger.record(context.run_id, item.message_ids)
        return ok(content)

    async def remove_quoted_sticker(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        key = self.idempotency_key(
            context,
            "sticker-removal",
            str(arguments["idempotency_key"]),
        )
        cached = await self.store.idempotency_get(key)
        if cached is not None:
            return ok(cached)
        if context.message_id is None or context.sender_id is None:
            return error(
                "a quoted group message is required",
                "STICKER_REMOVAL_QUOTE_REQUIRED",
            )
        source = await self.store.get_archived_message(
            context.scope_id,
            context.message_id,
        )
        if source is None or source.message.group_id is None:
            return error(
                "a quoted group message is required",
                "STICKER_REMOVAL_QUOTE_REQUIRED",
            )
        replies = tuple(
            segment
            for segment in source.message.segments
            if segment.type is MessageSegmentType.REPLY
        )
        if len(replies) != 1:
            return error(
                "a single quoted message is required",
                "STICKER_REMOVAL_QUOTE_REQUIRED",
            )
        quoted = replies[0].data.get("quoted")
        if not isinstance(quoted, Mapping):
            return error(
                "quoted message is unavailable",
                "STICKER_REMOVAL_SOURCE_UNAVAILABLE",
            )
        quoted_message_id = str(quoted.get("message_id") or "")
        raw_segments = quoted.get("segments")
        if not quoted_message_id or not isinstance(raw_segments, list):
            return error(
                "quoted message is unavailable",
                "STICKER_REMOVAL_SOURCE_UNAVAILABLE",
            )
        media = [
            (index, item)
            for index, item in enumerate(raw_segments)
            if isinstance(item, Mapping)
            and item.get("type")
            in {
                MessageSegmentType.IMAGE.value,
                MessageSegmentType.MFACE.value,
            }
        ]
        if not media:
            return error(
                "quoted message has no removable sticker",
                "STICKER_REMOVAL_MEDIA_REQUIRED",
            )
        if len(media) != 1:
            return error(
                "quoted message has multiple removable media items",
                "STICKER_REMOVAL_TARGET_AMBIGUOUS",
            )
        segment_index, item = media[0]
        data = item.get("data")
        if not isinstance(data, Mapping):
            return error(
                "quoted media is unavailable",
                "STICKER_REMOVAL_SOURCE_UNAVAILABLE",
            )
        try:
            fetched = await self.media_provider.fetch(
                str(item["type"]),
                dict(data),
                preserve_animation=True,
            )
            perceptual_hash, _preview = await asyncio.to_thread(
                _image_preview,
                fetched.content,
            )
        except MediaProviderError as exc:
            return error(
                "quoted media could not be read",
                exc.code,
                retryable=exc.retryable,
            )
        except (KeyError, ValueError):
            return error(
                "quoted media is invalid",
                "STICKER_MEDIA_INVALID",
            )
        content_sha256 = hashlib.sha256(fetched.content).hexdigest()
        async with self._asset_lock:
            asset, created = await self.store.remove_sticker(
                content_sha256=content_sha256,
                perceptual_hash=perceptual_hash,
                requester_id=context.sender_id,
                scope_id=context.scope_id,
                source_message_id=quoted_message_id,
                segment_index=segment_index,
                removed_at=datetime.now(UTC),
            )
        if asset is not None:
            path = self._absolute(asset.relative_path)
            try:
                await asyncio.to_thread(_unlink_file, path)
            except OSError:
                return error(
                    "sticker was disabled but its file cleanup is pending",
                    "STICKER_REMOVAL_CLEANUP_FAILED",
                    retryable=True,
                )
        outcome = (
            "blocked_only"
            if asset is None
            else "removed"
            if created
            else "already_removed"
        )
        content = {"removed": True, "outcome": outcome}
        await self.store.idempotency_put(key, content)
        await self.wake()
        return ok(content)

    def _relative(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.data_dir))

    def _absolute(self, relative_path: str) -> Path:
        path = (self.data_dir / relative_path).resolve()
        if path != self.data_dir and self.data_dir not in path.parents:
            raise ValueError("sticker path escapes data directory")
        return path


def _replacement_victim(
    candidate: StickerAsset,
    active: tuple[StickerAsset, ...],
) -> StickerAsset | None:
    counts = Counter(asset.category for asset in active)
    same_category = [
        asset for asset in active if asset.category is candidate.category
    ]
    if same_category:
        pool = same_category
    else:
        max_count = max(counts.values())
        pool = [
            asset for asset in active if counts[asset.category] == max_count
        ]
    victim = min(pool, key=_quality_score)
    if candidate.category not in counts:
        return victim
    return victim if _quality_score(candidate) > _quality_score(victim) else None


def _quality_score(asset: StickerAsset) -> float:
    return (
        asset.confidence * 100
        + min(asset.seen_count, 20) * 2
        + min(asset.use_count, 20) * 1.5
    )


def _image_preview(content: bytes) -> tuple[str, bytes]:
    try:
        with Image.open(BytesIO(content)) as image:
            image.seek(0)
            width, height = image.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_STICKER_IMAGE_SIDE
                or height > MAX_STICKER_IMAGE_SIDE
                or width * height > MAX_STICKER_IMAGE_PIXELS
            ):
                raise ValueError("invalid image dimensions")
            image.load()
            preview = image.convert("RGBA")
    except (
        Image.DecompressionBombError,
        EOFError,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ) as exc:
        raise ValueError("invalid sticker image") from exc
    grayscale = preview.convert("L").resize((9, 8))
    pixels = list(grayscale.get_flattened_data())
    bits = 0
    for row in range(8):
        for column in range(8):
            bits = (bits << 1) | (
                pixels[row * 9 + column]
                > pixels[row * 9 + column + 1]
            )
    output = BytesIO()
    preview.thumbnail((768, 768))
    preview.save(output, format="PNG")
    rgb_preview = preview.convert("RGB")
    color_preview = rgb_preview.resize((1, 1))
    rgb = color_preview.getpixel((0, 0))
    color_preview.close()
    rgb_preview.close()
    preview.close()
    grayscale.close()
    color = "".join(f"{value:02x}" for value in rgb)
    return f"{bits:016x}{color}", output.getvalue()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_move(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    _fsync_directory(target.parent)
    if source.parent != target.parent:
        _fsync_directory(source.parent)


def _unlink_file(path: Path) -> None:
    if not path.exists():
        return
    path.unlink()
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

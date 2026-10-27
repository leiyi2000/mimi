from __future__ import annotations

import uuid
from datetime import UTC, datetime

from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from core.domain import (
    InboundMessage,
    MessageSegmentType,
    StickerAsset,
    StickerAssetStatus,
    StickerCandidate,
    StickerCandidateStatus,
    StickerCategory,
    StickerClassification,
)
from persistence.models import (
    StickerAssetRow,
    StickerCandidateRow,
    StickerReplacementRow,
    StickerSendRow,
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class StickerRepositoryMixin:
    async def enqueue_sticker_candidates(self, message: InboundMessage) -> int:
        if message.is_self or message.group_id is None:
            return 0
        created = 0
        for index, segment in enumerate(message.segments):
            if segment.type not in {
                MessageSegmentType.IMAGE,
                MessageSegmentType.MFACE,
            }:
                continue
            try:
                await StickerCandidateRow.create(
                    candidate_id=uuid.uuid4().hex,
                    source_message_id=message.message_id,
                    scope_id=message.scope_id,
                    segment_index=index,
                    segment_type=segment.type.value,
                    status=StickerCandidateStatus.PENDING.value,
                )
                created += 1
            except IntegrityError:
                continue
        return created

    async def recover_sticker_candidates(
        self,
        *,
        claimed_before: datetime,
    ) -> int:
        return await StickerCandidateRow.filter(
            status=StickerCandidateStatus.PROCESSING.value,
            claimed_at__lte=claimed_before,
        ).update(
            status=StickerCandidateStatus.PENDING.value,
            claimed_at=None,
            error_code="STICKER_WORKER_INTERRUPTED",
        )

    async def claim_sticker_candidate(
        self,
        *,
        now: datetime,
    ) -> StickerCandidate | None:
        async with self._sticker_lock:
            rows = await StickerCandidateRow.filter(
                status__in=[
                    StickerCandidateStatus.PENDING.value,
                    StickerCandidateStatus.RETRYABLE_FAILED.value,
                ]
            ).order_by("created_at")
            row = next(
                (
                    item
                    for item in rows
                    if item.next_attempt_at is None
                    or _aware(item.next_attempt_at) <= now
                ),
                None,
            )
            if row is None:
                return None
            row.status = StickerCandidateStatus.PROCESSING.value
            row.attempt_count += 1
            row.claimed_at = now
            row.error_code = None
            await row.save()
            return self._candidate(row)

    async def finish_sticker_candidate(
        self,
        candidate_id: str,
        *,
        status: str,
        asset_id: str | None = None,
        error_code: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> None:
        await StickerCandidateRow.filter(candidate_id=candidate_id).update(
            status=status,
            asset_id=asset_id,
            error_code=error_code,
            next_attempt_at=next_attempt_at,
            claimed_at=None,
        )

    async def get_sticker_by_hash(
        self,
        content_sha256: str,
        perceptual_hash: str,
    ) -> StickerAsset | None:
        row = await StickerAssetRow.get_or_none(content_sha256=content_sha256)
        if row is None:
            row = await StickerAssetRow.get_or_none(
                perceptual_hash=perceptual_hash
            )
        return self._asset(row) if row is not None else None

    async def record_sticker_seen(
        self,
        sticker_id: str,
        *,
        scope_id: str,
        seen_at: datetime,
    ) -> StickerAsset:
        async with self._sticker_lock:
            row = await StickerAssetRow.get(sticker_id=sticker_id)
            scopes = {str(item) for item in row.seen_scopes}
            scopes.add(scope_id)
            row.seen_scopes = sorted(scopes)
            row.seen_count += 1
            row.last_seen_at = seen_at
            await row.save()
            return self._asset(row)

    async def create_sticker_asset(
        self,
        *,
        sticker_id: str,
        content_sha256: str,
        perceptual_hash: str,
        content_type: str,
        file_extension: str,
        relative_path: str,
        classification: StickerClassification,
        scope_id: str,
        seen_at: datetime,
    ) -> StickerAsset:
        row = await StickerAssetRow.create(
            sticker_id=sticker_id,
            status=StickerAssetStatus.STAGED.value,
            content_sha256=content_sha256,
            perceptual_hash=perceptual_hash,
            content_type=content_type,
            file_extension=file_extension,
            relative_path=relative_path,
            category=classification.category.value,
            tags=list(classification.tags),
            summary=classification.summary,
            tone=classification.tone,
            confidence=classification.confidence,
            seen_scopes=[scope_id],
            first_seen_at=seen_at,
            last_seen_at=seen_at,
        )
        return self._asset(row)

    async def list_active_stickers(
        self,
        *,
        category: StickerCategory | None = None,
    ) -> tuple[StickerAsset, ...]:
        query = StickerAssetRow.filter(status=StickerAssetStatus.ACTIVE.value)
        if category is not None:
            query = query.filter(category=category.value)
        rows = await query.order_by("-confidence", "-seen_count", "sticker_id")
        return tuple(self._asset(row) for row in rows)

    async def list_staged_stickers(self) -> tuple[StickerAsset, ...]:
        rows = await StickerAssetRow.filter(
            status=StickerAssetStatus.STAGED.value
        ).order_by("-confidence", "-seen_count", "first_seen_at")
        return tuple(self._asset(row) for row in rows)

    async def list_sticker_assets(self) -> tuple[StickerAsset, ...]:
        rows = await StickerAssetRow.all().order_by("created_at")
        return tuple(self._asset(row) for row in rows)

    async def delete_staged_sticker(self, sticker_id: str) -> bool:
        deleted = await StickerAssetRow.filter(
            sticker_id=sticker_id,
            status=StickerAssetStatus.STAGED.value,
        ).delete()
        return bool(deleted)

    async def activate_sticker(
        self,
        sticker_id: str,
        *,
        relative_path: str,
        activated_at: datetime,
    ) -> StickerAsset:
        async with self._sticker_lock:
            row = await StickerAssetRow.get(sticker_id=sticker_id)
            row.status = StickerAssetStatus.ACTIVE.value
            row.relative_path = relative_path
            row.activated_at = activated_at
            row.archived_at = None
            await row.save()
            return self._asset(row)

    async def replace_sticker(
        self,
        *,
        activated_sticker_id: str,
        activated_relative_path: str,
        archived_sticker_id: str,
        archived_relative_path: str,
        local_date: str,
        reason: str,
        changed_at: datetime,
    ) -> tuple[StickerAsset, StickerAsset]:
        async with self._sticker_lock:
            async with in_transaction() as connection:
                activated = await StickerAssetRow.filter(
                    sticker_id=activated_sticker_id
                ).using_db(connection).get()
                archived = await StickerAssetRow.filter(
                    sticker_id=archived_sticker_id
                ).using_db(connection).get()
                activated.status = StickerAssetStatus.ACTIVE.value
                activated.relative_path = activated_relative_path
                activated.activated_at = changed_at
                activated.archived_at = None
                archived.status = StickerAssetStatus.ARCHIVED.value
                archived.relative_path = archived_relative_path
                archived.archived_at = changed_at
                await activated.save(using_db=connection)
                await archived.save(using_db=connection)
                await StickerReplacementRow.create(
                    replacement_id=uuid.uuid4().hex,
                    local_date=local_date,
                    activated_sticker_id=activated_sticker_id,
                    archived_sticker_id=archived_sticker_id,
                    reason=reason[:255],
                    using_db=connection,
                )
            return self._asset(activated), self._asset(archived)

    async def sticker_replacement_count(self, local_date: str) -> int:
        return await StickerReplacementRow.filter(local_date=local_date).count()

    async def quarantine_sticker(
        self,
        sticker_id: str,
        *,
        relative_path: str,
    ) -> None:
        await StickerAssetRow.filter(sticker_id=sticker_id).update(
            status=StickerAssetStatus.QUARANTINED.value,
            relative_path=relative_path,
        )

    async def get_sticker(self, sticker_id: str) -> StickerAsset | None:
        row = await StickerAssetRow.get_or_none(sticker_id=sticker_id)
        return self._asset(row) if row is not None else None

    async def get_sticker_send_for_run(self, run_id: str):
        return await StickerSendRow.get_or_none(run_id=run_id)

    async def record_sticker_send(
        self,
        *,
        run_id: str,
        sticker_id: str,
        scope_id: str,
        outbox_id: str,
        status: str,
        sent_at: datetime | None,
    ) -> None:
        try:
            await StickerSendRow.create(
                send_id=uuid.uuid4().hex,
                run_id=run_id,
                sticker_id=sticker_id,
                scope_id=scope_id,
                outbox_id=outbox_id,
                status=status,
                sent_at=sent_at,
            )
        except IntegrityError:
            return
        if sent_at is not None:
            row = await StickerAssetRow.get(sticker_id=sticker_id)
            row.use_count += 1
            row.last_used_at = sent_at
            await row.save()

    @staticmethod
    def _candidate(row: StickerCandidateRow) -> StickerCandidate:
        return StickerCandidate(
            candidate_id=row.candidate_id,
            source_message_id=row.source_message_id,
            scope_id=row.scope_id,
            segment_index=row.segment_index,
            segment_type=MessageSegmentType(row.segment_type),
            status=StickerCandidateStatus(row.status),
            attempt_count=row.attempt_count,
            next_attempt_at=_aware(row.next_attempt_at),
            asset_id=row.asset_id,
            error_code=row.error_code,
        )

    @staticmethod
    def _asset(row: StickerAssetRow) -> StickerAsset:
        return StickerAsset(
            sticker_id=row.sticker_id,
            status=StickerAssetStatus(row.status),
            content_sha256=row.content_sha256,
            perceptual_hash=row.perceptual_hash,
            content_type=row.content_type,
            file_extension=row.file_extension,
            relative_path=row.relative_path,
            category=StickerCategory(row.category),
            tags=tuple(str(item) for item in row.tags),
            summary=row.summary,
            tone=row.tone,
            confidence=float(row.confidence),
            seen_count=row.seen_count,
            scope_count=len(row.seen_scopes),
            use_count=row.use_count,
            first_seen_at=_aware(row.first_seen_at),
            last_seen_at=_aware(row.last_seen_at),
            activated_at=_aware(row.activated_at),
            last_used_at=_aware(row.last_used_at),
            archived_at=_aware(row.archived_at),
        )

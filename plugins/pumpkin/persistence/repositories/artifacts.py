from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from persistence.models import ArtifactAssessmentRow, ArtifactRow

from core.domain import ArtifactDeliveryStatus, ArtifactRef


class ArtifactRepositoryMixin:
    async def save_artifact(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
        artifact: ArtifactRef,
        content_sha256: str | None = None,
        source_fingerprint: str | None = None,
        strategy_fingerprint: str | None = None,
    ) -> None:
        await ArtifactRow.get_or_create(
            artifact_id=artifact.artifact_id,
            defaults={
                "task_id": task_id,
                "revision": revision,
                "scope_id": scope_id,
                "kind": artifact.kind,
                "name": artifact.name,
                "path": artifact.path,
                "size_bytes": artifact.size_bytes,
                "verified": artifact.verified,
            },
        )
        await ArtifactAssessmentRow.get_or_create(
            artifact_id=artifact.artifact_id,
            defaults={
                "task_id": task_id,
                "revision": revision,
                "scope_id": scope_id,
                "content_sha256": content_sha256,
                "source_fingerprint": source_fingerprint,
                "strategy_fingerprint": strategy_fingerprint,
                "delivery_status": ArtifactDeliveryStatus.CANDIDATE.value,
            },
        )

    async def get_artifacts(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
        artifact_ids: tuple[str, ...],
    ) -> tuple[ArtifactRef, ...]:
        rows = await ArtifactRow.filter(
            task_id=task_id,
            revision=revision,
            scope_id=scope_id,
            artifact_id__in=list(artifact_ids),
            verified=True,
        )
        by_id = {
            row.artifact_id: ArtifactRef(
                artifact_id=row.artifact_id,
                kind=row.kind,
                name=row.name,
                size_bytes=row.size_bytes,
                verified=row.verified,
                path=row.path,
            )
            for row in rows
        }
        if set(by_id) != set(artifact_ids):
            raise ValueError("artifact is missing, unverified or outside task scope")
        return tuple(by_id[item] for item in artifact_ids)

    async def artifact_rejection_fingerprints(
        self,
        *,
        task_id: str,
        scope_id: str,
        artifact_ids: tuple[str, ...],
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        if not artifact_ids:
            return (), (), ()
        rows = await ArtifactAssessmentRow.filter(
            task_id=task_id,
            scope_id=scope_id,
            artifact_id__in=list(artifact_ids),
        )
        found = {row.artifact_id for row in rows}
        missing = set(artifact_ids).difference(found)
        if missing:
            artifacts = await ArtifactRow.filter(
                task_id=task_id,
                scope_id=scope_id,
                artifact_id__in=list(missing),
                verified=True,
            )
            for artifact in artifacts:
                digest = _artifact_digest(artifact.path)
                rows.append(
                    await ArtifactAssessmentRow.create(
                        artifact_id=artifact.artifact_id,
                        task_id=task_id,
                        revision=artifact.revision,
                        scope_id=scope_id,
                        content_sha256=digest,
                        delivery_status=(
                            ArtifactDeliveryStatus.DELIVERY_READY.value
                        ),
                    )
                )
        if {row.artifact_id for row in rows} != set(artifact_ids):
            raise ValueError("rejected artifact is outside task scope")
        return (
            tuple(
                dict.fromkeys(
                    row.content_sha256 for row in rows if row.content_sha256
                )
            ),
            tuple(
                dict.fromkeys(
                    row.source_fingerprint
                    for row in rows
                    if row.source_fingerprint
                )
            ),
            tuple(
                dict.fromkeys(
                    row.strategy_fingerprint
                    for row in rows
                    if row.strategy_fingerprint
                )
            ),
        )

    async def mark_artifacts_delivery_ready(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
        artifact_ids: tuple[str, ...],
        using_db: Any = None,
    ) -> None:
        if not artifact_ids:
            return
        query = ArtifactAssessmentRow.filter(
            task_id=task_id,
            revision=revision,
            scope_id=scope_id,
            artifact_id__in=list(artifact_ids),
            delivery_status=ArtifactDeliveryStatus.CANDIDATE.value,
        )
        if using_db is not None:
            query = query.using_db(using_db)
        updated = await query.update(
            delivery_status=ArtifactDeliveryStatus.DELIVERY_READY.value
        )
        if updated != len(set(artifact_ids)):
            raise ValueError("artifact cannot be marked delivery ready")

    async def delivery_ready_artifacts(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
    ) -> tuple[ArtifactRef, ...]:
        assessments = await ArtifactAssessmentRow.filter(
            task_id=task_id,
            revision=revision,
            scope_id=scope_id,
            delivery_status=ArtifactDeliveryStatus.DELIVERY_READY.value,
        ).order_by("id")
        if not assessments:
            return ()
        return await self.get_artifacts(
            task_id=task_id,
            revision=revision,
            scope_id=scope_id,
            artifact_ids=tuple(row.artifact_id for row in assessments),
        )

    async def artifact_assessments(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
        artifact_ids: tuple[str, ...],
    ) -> tuple[ArtifactAssessmentRow, ...]:
        rows = await ArtifactAssessmentRow.filter(
            task_id=task_id,
            revision=revision,
            scope_id=scope_id,
            artifact_id__in=list(artifact_ids),
        )
        by_id = {row.artifact_id: row for row in rows}
        missing = set(artifact_ids).difference(by_id)
        if missing:
            artifacts = await ArtifactRow.filter(
                task_id=task_id,
                revision=revision,
                scope_id=scope_id,
                artifact_id__in=list(missing),
                verified=True,
            )
            for artifact in artifacts:
                row = await ArtifactAssessmentRow.create(
                    artifact_id=artifact.artifact_id,
                    task_id=task_id,
                    revision=revision,
                    scope_id=scope_id,
                    content_sha256=_artifact_digest(artifact.path),
                    delivery_status=ArtifactDeliveryStatus.CANDIDATE.value,
                )
                by_id[row.artifact_id] = row
        if set(by_id) != set(artifact_ids):
            raise ValueError("artifact assessment is missing or outside task scope")
        return tuple(by_id[artifact_id] for artifact_id in artifact_ids)


def _artifact_digest(path_value: str | None) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()

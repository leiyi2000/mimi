from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.config import PumpkinConfig
from core.domain import (
    ArtifactDeliveryStatus,
    ArtifactRef,
    CorrectionViolation,
    TaskStatus,
    ToolContext,
    ToolResult,
    acceptance_criterion_id,
)
from core.tasking import TaskService, TaskStateError
from persistence.repositories import ArtifactStore
from runtime.task_events import ConversationInboxPublisher


log = logging.getLogger(__name__)


class ArtifactService:
    """Validate, persist, and gate task artifacts."""

    def __init__(
        self,
        config: PumpkinConfig,
        *,
        store: ArtifactStore,
        task_service: TaskService,
        task_lock: Callable[[str], Any],
        task_event_notifier: ConversationInboxPublisher,
    ) -> None:
        self.config = config
        self.store = store
        self.task_service = task_service
        self.task_lock = task_lock
        self.task_event_notifier = task_event_notifier

    async def write(
        self,
        context: ToolContext,
        *,
        filename: str,
        kind: str,
        content: bytes,
        source_ref: Any | None = None,
        strategy_ref: Any | None = None,
    ) -> ArtifactRef:
        if context.task_id is None or context.revision is None:
            raise ValueError("task context is missing")
        if kind not in {"image", "file", "audio", "text"}:
            raise ValueError("unsupported artifact kind")
        safe_name = Path(filename).name
        if (
            not safe_name
            or safe_name in {".", ".."}
            or safe_name != filename
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", safe_name)
        ):
            raise ValueError("invalid artifact filename")
        if not content or len(content) > 20 * 1024 * 1024:
            raise ValueError("artifact is empty or too large")
        if kind == "image" and not content.startswith(
            (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8", b"RIFF")
        ):
            raise ValueError("artifact is not a recognized image")
        if kind == "audio" and not _recognized_audio(content):
            raise ValueError("artifact is not a recognized audio")
        root = (
            Path(self.config.data_dir)
            / "artifacts"
            / context.task_id
            / str(context.revision)
        ).resolve()
        root.mkdir(parents=True, exist_ok=True)
        artifact_id = uuid.uuid4().hex
        path = (root / f"{artifact_id}-{safe_name}").resolve()
        path.relative_to(root)
        fd, temporary = tempfile.mkstemp(prefix=f".{safe_name}.", dir=root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        artifact = ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            name=safe_name,
            size_bytes=len(content),
            verified=True,
            path=str(path),
        )
        await self.store.save_artifact(
            task_id=context.task_id,
            revision=context.revision,
            scope_id=context.scope_id,
            artifact=artifact,
            content_sha256=hashlib.sha256(content).hexdigest(),
            source_fingerprint=self.provenance_fingerprint(source_ref),
            strategy_fingerprint=self.provenance_fingerprint(strategy_ref),
        )
        return artifact

    async def gate(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
        artifact_ids: tuple[str, ...],
        require_delivery_ready: bool,
    ) -> ToolResult | None:
        try:
            violation = await self._correction_violation(
                task_id=task_id,
                revision=revision,
                scope_id=scope_id,
                artifact_ids=artifact_ids,
                require_delivery_ready=require_delivery_ready,
            )
        except ValueError as exc:
            return _error(str(exc), "ARTIFACT_ASSESSMENT_MISSING")
        if violation is None:
            return None
        if violation.correction_id == "delivery-readiness":
            return _error(
                "artifact is not explicitly delivery ready",
                "ARTIFACT_NOT_DELIVERY_READY",
            )
        return await self.correction_violation_result(
            task_id=task_id,
            revision=revision,
            violation=violation,
        )

    async def _correction_violation(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
        artifact_ids: tuple[str, ...],
        require_delivery_ready: bool,
    ) -> CorrectionViolation | None:
        if not artifact_ids:
            return None
        assessments = await self.store.artifact_assessments(
            task_id=task_id,
            revision=revision,
            scope_id=scope_id,
            artifact_ids=artifact_ids,
        )
        if require_delivery_ready and any(
            row.delivery_status != ArtifactDeliveryStatus.DELIVERY_READY.value
            for row in assessments
        ):
            return CorrectionViolation(
                correction_id="delivery-readiness",
                reasons=("not_delivery_ready",),
                violation_count=1,
            )
        corrections = await self.store.list_corrections(
            task_id,
            through_revision=revision,
        )
        for correction in corrections:
            reasons: list[str] = []
            for assessment in assessments:
                if assessment.artifact_id in correction.rejected_artifact_ids:
                    reasons.append("artifact")
                if (
                    assessment.content_sha256
                    and assessment.content_sha256 in correction.rejected_hashes
                ):
                    reasons.append("content")
                if (
                    assessment.source_fingerprint
                    and assessment.source_fingerprint
                    in correction.rejected_sources
                ):
                    reasons.append("source")
                if (
                    assessment.strategy_fingerprint
                    and assessment.strategy_fingerprint
                    in correction.rejected_strategies
                ):
                    reasons.append("strategy")
                if assessment.delivery_status == ArtifactDeliveryStatus.REJECTED.value:
                    reasons.append("rejected")
            if reasons:
                scoped_reasons = tuple(sorted(set(reasons)))
                violation_counts = []
                for reason in scoped_reasons:
                    violation_counts.append(
                        await self.store.record_correction_violation(
                            correction.correction_id,
                            revision=revision,
                            reason=reason,
                        )
                    )
                return CorrectionViolation(
                    correction_id=correction.correction_id,
                    reasons=scoped_reasons,
                    violation_count=max(violation_counts),
                )
        return None

    async def acceptance_criteria_gate(
        self,
        *,
        task_id: str,
        revision: int,
        accepted_criteria: tuple[str, ...],
    ) -> ToolResult | None:
        submitted = {item.strip() for item in accepted_criteria if item.strip()}
        for correction in await self.store.list_corrections(
            task_id,
            through_revision=revision,
        ):
            missing = tuple(
                criterion
                for index, criterion in enumerate(correction.acceptance_criteria)
                if criterion.strip() not in submitted
                and acceptance_criterion_id(
                    correction.correction_id,
                    index,
                )
                not in submitted
            )
            if not missing:
                continue
            count = await self.store.record_correction_violation(
                correction.correction_id,
                revision=revision,
                reason="acceptance_criteria",
            )
            return await self.correction_violation_result(
                task_id=task_id,
                revision=revision,
                violation=CorrectionViolation(
                    correction_id=correction.correction_id,
                    reasons=("acceptance_criteria",),
                    violation_count=count,
                ),
            )
        return None

    async def correction_violation_result(
        self,
        *,
        task_id: str,
        revision: int,
        violation: CorrectionViolation,
    ) -> ToolResult:
        if violation.violation_count < 2:
            return _error(
                {
                    "message": "artifact violates an active user correction",
                    "correction_id": violation.correction_id,
                    "reasons": list(violation.reasons),
                    "remaining_attempts": 1,
                },
                "CORRECTION_VIOLATION",
            )
        row = await self.store.get_task(task_id)
        if (
            row is not None
            and row.revision == revision
            and TaskStatus(row.status) in {TaskStatus.RUNNING, TaskStatus.QUEUED}
        ):
            try:
                async with self.task_lock(task_id):
                    event = await self.task_service.transition(
                        task_id,
                        revision,
                        TaskStatus.FAILED,
                        summary="task stopped after repeated correction violations",
                    )
                await self.task_event_notifier.notify(event)
            except TaskStateError:
                log.warning(
                    "failed to stop task after correction violation task=%s",
                    task_id,
                )
        return _error(
            {
                "message": "task stopped after repeated correction violations",
                "correction_id": violation.correction_id,
                "reasons": list(violation.reasons),
            },
            "CORRECTION_REPEAT_LIMIT",
        )

    @staticmethod
    def provenance_fingerprint(value: Any | None) -> str | None:
        if value is None:
            return None
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _recognized_audio(content: bytes) -> bool:
    return (
        content.startswith((b"ID3", b"fLaC"))
        or (
            len(content) >= 2
            and content[0] == 0xFF
            and content[1] & 0xE0 == 0xE0
        )
        or (
            len(content) >= 12
            and content[:4] == b"RIFF"
            and content[8:12] == b"WAVE"
        )
    )


def _error(content: Any, code: str) -> ToolResult:
    return ToolResult(content=content, is_error=True, error_code=code)

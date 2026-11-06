from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.domain import ModelMessage, ModelResponse, TaskCorrection, ToolCall, TaskStatus
from test_task_corrections import _context, _create_running_task, _runtime
from core.domain import AgentRole, acceptance_criterion_id
from core.tasking import TaskStateError


class VerificationProvider:
    def __init__(self, *, verdict="passed", forged=False, mutate=None, fail=False,
                 missing_evidence=False, extra_fields=False):
        self.verdict = verdict
        self.forged = forged
        self.mutate = mutate
        self.fail = fail
        self.missing_evidence = missing_evidence
        self.extra_fields = extra_fields
        self.calls = 0

    async def stream(self, messages, tools, *, mode):
        import json

        self.calls += 1
        assert mode == "task"
        payload = json.loads(messages[1].content[0]["text"])
        if self.mutate:
            self.mutate()
        if self.fail:
            raise RuntimeError("verification unavailable")
        yield ModelResponse(
            message=ModelMessage(
                role="assistant",
                content="",
                tool_calls=(ToolCall("report-1", tools[0].name, {
                    "criteria": [
                        {
                            "criterion_id": item["criterion_id"],
                            "verdict": self.verdict,
                            "evidence_ids": (
                                [] if self.missing_evidence else (
                                ["invented"]
                                if self.forged
                                else [payload["evidence"][0]["evidence_id"]]
                                )
                            ),
                        }
                        for item in payload["criteria"]
                    ],
                    **({"unexpected": "do not persist arbitrary model text"}
                       if self.extra_fields else {}),
                }),),
            ),
            request_id="verification-request",
            provider="fixture",
            model="fixture",
        )


async def corrected_candidate(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime)
    correction = TaskCorrection(
        correction_id="correction-verification",
        task_id="task-1",
        scope_id="group:1",
        source_message_id="correction-message",
        from_revision=0,
        to_revision=1,
        instruction="补齐正文",
        acceptance_criteria=("包含正文",),
        rejected_artifact_ids=(),
        rejected_hashes=(),
        rejected_sources=(),
        rejected_strategies=(),
    )
    await runtime.store.create_correction(correction)
    context = _context(role=AgentRole.TASK, run_id="task-run", revision=1)
    artifact = await runtime.write_artifact(
        context, filename="result.txt", kind="text", content="正文".encode(),
    )
    arguments = {
        "status": "completed",
        "summary": "完成",
        "remaining": [],
        "accepted_criteria": [acceptance_criterion_id(correction.correction_id, 0)],
        "artifact_ids": [artifact.artifact_id],
        "idempotency_key": "complete",
    }
    return runtime, context, artifact, arguments


async def test_condition_ids_do_not_verify_actual_content(tmp_path):
    runtime, context, _, arguments = await corrected_candidate(tmp_path)
    result = await runtime.task_commands.complete_task(context, arguments)
    assert result.is_error
    assert result.error_code == "TASK_VERIFICATION_UNAVAILABLE"
    assert (await runtime.store.get_task("task-1")).status == TaskStatus.RUNNING.value
    assert not await runtime.store.delivery_ready_artifacts(
        task_id="task-1", revision=1, scope_id="group:1",
    )
    await runtime.store.close()


@pytest.mark.parametrize(
    ("provider", "code"),
    [
        (VerificationProvider(verdict="failed"), "TASK_VERIFICATION_REJECTED"),
        (VerificationProvider(verdict="unknown"), "TASK_VERIFICATION_REJECTED"),
        (VerificationProvider(forged=True), "TASK_VERIFICATION_INVALID"),
        (VerificationProvider(fail=True), "TASK_VERIFICATION_UNAVAILABLE"),
        (VerificationProvider(missing_evidence=True), "TASK_VERIFICATION_INVALID"),
        (VerificationProvider(extra_fields=True), "TASK_VERIFICATION_INVALID"),
    ],
)
async def test_unproven_results_preserve_candidates(tmp_path, provider, code):
    runtime, context, artifact, arguments = await corrected_candidate(tmp_path)
    runtime.task_verification.provider = provider
    result = await runtime.task_commands.complete_task(context, arguments)
    assert result.error_code == code
    assert Path(artifact.path).is_file()
    assert (await runtime.store.get_task("task-1")).status == "running"
    await runtime.store.close()


async def test_verified_content_completes_and_retry_does_not_recheck(tmp_path):
    runtime, context, _, arguments = await corrected_candidate(tmp_path)
    provider = VerificationProvider()
    runtime.task_verification.provider = provider
    result = await runtime.task_commands.complete_task(context, arguments)
    assert not result.is_error
    assert not (await runtime.task_commands.complete_task(context, arguments)).is_error
    assert provider.calls == 1
    events = await runtime.store.events("task-1")
    assert any(item.evidence for item in events)
    await runtime.store.close()


async def test_candidate_change_during_verification_rejects_completion(tmp_path):
    runtime, context, artifact, arguments = await corrected_candidate(tmp_path)
    runtime.task_verification.provider = VerificationProvider(
        mutate=lambda: Path(artifact.path).write_text("changed"),
    )
    result = await runtime.task_commands.complete_task(context, arguments)
    assert result.error_code == "TASK_VERIFICATION_STALE"
    assert (await runtime.store.get_task("task-1")).status == "running"
    await runtime.store.close()


async def test_stale_scope_does_not_consume_model_request(tmp_path):
    runtime, context, _, arguments = await corrected_candidate(tmp_path)
    provider = VerificationProvider()
    runtime.task_verification.provider = provider
    result = await runtime.task_commands.complete_task(
        replace(context, revision=2), {**arguments, "artifact_ids": []},
    )
    assert result.is_error
    assert provider.calls == 0
    await runtime.store.close()


async def test_successful_proof_is_reused_after_completion_transaction_rejection(tmp_path):
    runtime, context, _, arguments = await corrected_candidate(tmp_path)
    provider = VerificationProvider()
    runtime.task_verification.provider = provider
    complete = runtime.task_service.complete
    runtime.task_service.complete = AsyncMock(side_effect=TaskStateError("CAS rejected"))
    first = await runtime.task_commands.complete_task(context, arguments)
    assert first.error_code == "TASK_COMPLETION_REJECTED"
    runtime.task_service.complete = complete
    second = await runtime.task_commands.complete_task(context, arguments)
    assert not second.is_error
    assert provider.calls == 1
    await runtime.store.close()


async def test_unreadable_image_never_reaches_verifier(tmp_path):
    runtime, context, _, arguments = await corrected_candidate(tmp_path)
    provider = VerificationProvider()
    runtime.task_verification.provider = provider
    artifact = await runtime.write_artifact(
        context, filename="invalid.png", kind="image", content=b"\x89PNG\r\n\x1a\ninvalid",
    )
    result = await runtime.task_commands.complete_task(
        context, {**arguments, "artifact_ids": [artifact.artifact_id]},
    )
    assert result.error_code == "TASK_VERIFICATION_EVIDENCE_MISSING"
    assert provider.calls == 0
    await runtime.store.close()

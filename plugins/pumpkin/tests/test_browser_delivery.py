import asyncio
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from capabilities.browser import BrowserCapabilityPlugin
from core.agent_loop import _TurnToolBudget
from core.domain import AgentRole, ArtifactDeliveryStatus, TaskStatus, ToolCall
from core.tools import ToolBudgetLimits
from runtime.browser_agent import (
    BrowserAgentRunner,
    BrowserArtifact,
    BrowserArtifactMode,
    BrowserInteractionMode,
    BrowserRunResult,
    BrowserTaskStatus,
)
from providers.public_url import PublicUrlGuard
from test_browser_agent import (
    FakeProvider,
    FakeWorker,
    plan,
    public_resolver,
    response,
)
from test_task_corrections import _context, _create_running_task, _runtime
from test_task_verification import VerificationProvider


def screenshot():
    output = BytesIO()
    Image.new("RGB", (32, 24), "white").save(output, format="PNG")
    return output.getvalue()


def browser_report(
    *,
    run_id: str = "browser-run",
    interaction_mode: str = "read_only",
    start_url: str = "https://example.com",
    artifact_mode: str = "none",
    goal_verified: bool = False,
):
    return {
        "run_id": run_id,
        "goal_verified": goal_verified,
        "task_contract": {
            "goal": "test goal",
            "start_url": start_url,
            "expected_output": (
                "screenshot" if artifact_mode == "screenshot" else "summary"
            ),
            "interaction_mode": interaction_mode,
            "artifact_mode": artifact_mode,
            "required_capabilities": [],
            "required_evidence": [],
        },
    }


def test_browser_capability_exposes_only_goal_and_derives_stable_fingerprint():
    goal = "Inspect https://example.com/form and summarize it."
    assert BrowserCapabilityPlugin._fingerprint(f"  {goal}  ") == (
        BrowserCapabilityPlugin._fingerprint(goal)
    )


async def test_failed_browser_preserves_report_and_diagnostics_in_cached_result(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("browser-delegation",))
    runtime.task_runtime.runs["task-1"] = runtime.task_runtime.handoff_for_row(
        await runtime.store.get_task("task-1")
    )
    context = _context(role=AgentRole.TASK, run_id="browser-run", revision=1)
    report = {
        "run_id": "original-run", "goal_verified": False,
        "actions": [{"execution": "returned", "effect": "unknown"}],
        "task_contract": browser_report(
            interaction_mode="public_interaction",
        )["task_contract"],
    }
    runtime.browser_plugin.runner = SimpleNamespace(run=AsyncMock(
        return_value=BrowserRunResult(
            status=BrowserTaskStatus.FAILED, summary="model unavailable",
            error_code="MODEL_UNAVAILABLE", command_count=3,
            execution_report=report,
            artifacts=(BrowserArtifact(
                BrowserArtifactMode.SCREENSHOT, screenshot(), "image/png",
            ),),
        ),
    ))
    arguments = {"goal": "Inspect the panel at https://example.com and capture it."}
    try:
        original = await runtime.registry.execute("delegate_browser_task", context, arguments)
        cached = await runtime.registry.execute("delegate_browser_task", context, arguments)
        assert original.content["execution_report"] == report
        assert cached.content["execution_report"] == report
        assert original.content["artifact_ids"] == []
        assert len(original.content["diagnostic_artifact_ids"]) == 1
        assert runtime.browser_plugin.runner.run.await_count == 1
        conflict = await runtime.registry.execute(
            "delegate_browser_task",
            context,
            {"goal": "Retry the panel at https://example.com."},
        )
        assert conflict.content["previous_execution_report"] == report
    finally:
        await runtime.store.close()


async def test_goal_only_delegate_reuses_same_goal_and_blocks_replanning(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("browser-delegation",))
    runtime.task_runtime.runs["task-1"] = runtime.task_runtime.handoff_for_row(
        await runtime.store.get_task("task-1")
    )
    context = _context(role=AgentRole.TASK, run_id="browser-run", revision=1)
    runtime.browser_plugin.runner = SimpleNamespace(run=AsyncMock(
        return_value=BrowserRunResult(
            status=BrowserTaskStatus.PARTIALLY_COMPLETED,
            summary="not submitted", error_code="BROWSER_COMPLETION_UNVERIFIED",
            command_count=2,
        ),
    ))
    budget = _TurnToolBudget(ToolBudgetLimits(1, 1, 1, browser_delegate=4))
    tool = runtime.registry.get("delegate_browser_task")
    assert tool.input_schema["required"] == ["goal"]
    assert set(tool.input_schema["properties"]) == {"goal"}
    arguments = {"goal": "Inspect https://example.com/form and summarize it."}
    try:
        results = []
        for index, changes in enumerate(
            (
                {},
                {},
                {"goal": "Fill and submit https://example.com/form."},
                {"goal": "Inspect validation errors at https://example.com/form."},
            )
        ):
            call = ToolCall(f"browser-{index}", tool.name, {**arguments, **changes})
            result = budget.prepare(tool, call)
            if result is None:
                result = await runtime.registry.execute(
                    tool.name, context, call.arguments,
                )
                budget.finish(tool, call, result)
            results.append(result)
        assert results[0].content["contract_fingerprint"] == (
            results[1].content["contract_fingerprint"]
        )
        assert results[2].error_code == "BROWSER_REPLAY_BLOCKED"
        assert results[3].error_code == "BROWSER_REPLAY_BLOCKED"
        assert runtime.browser_plugin.runner.run.await_count == 1
        run_arguments = runtime.browser_plugin.runner.run.await_args.kwargs
        assert run_arguments["goal"] == arguments["goal"]
        assert run_arguments["original_requests"] == ()
        assert len(run_arguments["task_owner"]) == 64
        exhausted = budget.prepare(
            tool,
            ToolCall(
                "extra",
                tool.name,
                {"goal": "Another probe at https://example.com."},
            ),
        )
        assert exhausted.error_code == "TOOL_BUDGET_EXHAUSTED"
    finally:
        await runtime.store.close()


@pytest.mark.parametrize("previous_status", ["in_progress", "partially_completed", "completed"])
async def test_goal_only_run_cannot_promote_unverified_prior_interaction(
    tmp_path,
    previous_status,
):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("browser-delegation",))
    runtime.task_runtime.runs["task-1"] = runtime.task_runtime.handoff_for_row(
        await runtime.store.get_task("task-1")
    )
    context = _context(role=AgentRole.TASK, run_id="browser-run", revision=1)
    original_report = {"run_id": "original", "goal_verified": previous_status == "completed"}
    key = runtime.idempotency_key(
        context, "browser-operation",
        BrowserInteractionMode.PUBLIC_INTERACTION.operation_fingerprint,
    )
    await runtime.store.idempotency_put(key, {
        "status": previous_status, "execution_report": original_report,
    })
    runtime.browser_plugin.runner = SimpleNamespace(run=AsyncMock(
        return_value=BrowserRunResult(
            status=BrowserTaskStatus.COMPLETED, summary="new blank page",
            command_count=2,
            execution_report=browser_report(
                run_id="fresh",
                goal_verified=True,
            ),
        ),
    ))
    try:
        observation = await runtime.registry.execute("delegate_browser_task", context, {
            "goal": "Inspect the page at https://example.com.",
        })
        assert observation.content["execution_report"]["run_id"] == "fresh"
        completed = await runtime.task_commands.complete_task(context, {
            "status": "completed", "summary": "submitted", "remaining": [],
            "accepted_criteria": ["original interaction verified"],
            "artifact_ids": [], "idempotency_key": "completed",
        })
        if previous_status == "completed":
            assert not completed.is_error
        else:
            assert completed.error_code == "BROWSER_INTERACTION_UNVERIFIED"
            assert completed.content["execution_report"]["run_id"] == "original"
            partial = await runtime.task_commands.complete_task(context, {
                "status": "partially_completed", "summary": "outcome unknown",
                "remaining": ["confirm original interaction"], "accepted_criteria": [],
                "artifact_ids": [], "idempotency_key": "partial",
            })
            assert not partial.is_error
            assert partial.content["outcome"] == TaskStatus.PARTIALLY_COMPLETED.value
    finally:
        await runtime.store.close()


@pytest.mark.parametrize("shutdown", [False, True])
async def test_browser_task_cancel_and_shutdown_wait_for_release(tmp_path, shutdown):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("browser-delegation",))
    runtime.task_runtime.runs["task-1"] = runtime.task_runtime.handoff_for_row(
        await runtime.store.get_task("task-1")
    )
    context = _context(role=AgentRole.TASK, run_id="browser-run", revision=1)
    started, cleaning, released = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class WaitingProvider(FakeProvider):
        compiled = False

        async def stream(self, messages, tools, *, mode):
            if not self.compiled:
                self.compiled = True
                yield plan(start_url="https://example.com")
                return
            started.set()
            await asyncio.Event().wait()
            yield response("finish_browser_task", {
                "status": "partially_completed",
                "summary": "cancelled",
                "evidence": [],
            })

    class SlowWorker(FakeWorker):
        owner = ""

        async def create_run(self, *, contract, task_owner=""):
            self.owner = task_owner
            return await super().create_run(contract=contract, task_owner=task_owner)

        async def cancel_run(self, run):
            cleaning.set()
            await released.wait()
            await super().cancel_run(run)

    worker = SlowWorker()
    runtime.browser_plugin.runner = BrowserAgentRunner(
        provider=WaitingProvider([]), worker=worker,
        url_guard=PublicUrlGuard(public_resolver),
    )
    arguments = {"goal": "Inspect https://example.com and summarize the page."}
    execution = asyncio.create_task(runtime.browser_plugin.delegate(
        runtime.capability_services, context, arguments,
    ))
    runtime.task_runtime.tasks["task-1"] = execution
    stop = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        stop = asyncio.create_task(
            runtime.browser_plugin.close() if shutdown else
            runtime.task_runtime.cancel_task("task-1", reason="new revision", keep_inbox=False)
        )
        await asyncio.wait_for(cleaning.wait(), 1)
        assert not stop.done()
        assert not worker.cancelled
        assert len(worker.owner) == 64
        released.set()
        await asyncio.wait_for(stop, 1)
        assert worker.cancelled
        assert execution.cancelled()
        assert runtime.browser_plugin._active == {}
        if shutdown:
            result = await runtime.browser_plugin.delegate(
                runtime.capability_services, context, arguments,
            )
            assert result.error_code == "BROWSER_CLOSING"
    finally:
        released.set()
        execution.cancel()
        await asyncio.gather(execution, *([stop] if stop else []), return_exceptions=True)
        await runtime.store.close()


async def test_partial_browser_screenshots_cannot_be_completed_as_deliverables(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("browser-delegation",))
    runtime.task_runtime.runs["task-1"] = runtime.task_runtime.handoff_for_row(
        await runtime.store.get_task("task-1")
    )
    context = _context(role=AgentRole.TASK, run_id="browser-run", revision=1)
    runtime.browser_plugin.runner = SimpleNamespace(
        run=AsyncMock(return_value=BrowserRunResult(
            status=BrowserTaskStatus.PARTIALLY_COMPLETED,
            summary="budget exhausted",
            error_code="BROWSER_BUDGET_EXHAUSTED",
            command_count=2,
            execution_report=browser_report(
                start_url="https://example.com/form",
                artifact_mode="screenshot",
            ),
            artifacts=(BrowserArtifact(
                BrowserArtifactMode.SCREENSHOT, screenshot(), "image/png",
            ),),
        ))
    )
    try:
        result = await runtime.browser_plugin.delegate(
            runtime.capability_services, context,
            {
                "goal": "Capture the submitted form at https://example.com/form.",
            },
        )
        assert result.is_error
        assert result.artifacts == ()
        assert result.content["artifact_ids"] == []
        diagnostic_ids = tuple(result.content["diagnostic_artifact_ids"])
        assert len(diagnostic_ids) == 1
        assessment, = await runtime.store.artifact_assessments(
            task_id="task-1", revision=1, scope_id="group:1",
            artifact_ids=diagnostic_ids,
        )
        assert assessment.delivery_status == ArtifactDeliveryStatus.DIAGNOSTIC.value
        cached = await runtime.browser_plugin.delegate(
            runtime.capability_services, context,
            {
                "goal": "Capture the submitted form at https://example.com/form.",
            },
        )
        assert cached.is_error
        assert cached.error_code == "BROWSER_BUDGET_EXHAUSTED"
        assert cached.content["artifact_ids"] == []
        runtime.browser_plugin.runner.run.assert_awaited_once()
        conflict = await runtime.browser_plugin.delegate(
            runtime.capability_services, context,
            {
                "goal": "Inspect a different state at https://example.com/form.",
            },
        )
        assert conflict.error_code == "BROWSER_REPLAY_BLOCKED"
        runtime.browser_plugin.runner.run.assert_awaited_once()
        completion = await runtime.task_commands.complete_task(context, {
            "status": "partially_completed", "summary": "not submitted",
            "remaining": ["submit"], "accepted_criteria": [],
            "artifact_ids": list(diagnostic_ids), "idempotency_key": "complete",
        })
        assert completion.error_code == "ARTIFACT_DIAGNOSTIC_ONLY"
        assert (await runtime.store.get_task("task-1")).status == "running"
    finally:
        await runtime.store.close()


async def test_old_browser_screenshot_is_rechecked_before_sending(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime)
    context = _context(role=AgentRole.TASK, run_id="old-run", revision=1)
    artifact = await runtime.write_artifact(
        context, filename="browser-1.png", kind="image", content=screenshot(),
        strategy_ref={
            "tool": "delegate_browser_task", "artifact_mode": "screenshot",
        },
    )
    await runtime.task_service.complete(
        task_id="task-1", revision=1, scope_id="group:1",
        status=TaskStatus.PARTIALLY_COMPLETED, summary="not submitted",
        remaining=("submit",), accepted_criteria=("opened form",),
        artifacts=(artifact,), artifact_ids=(artifact.artifact_id,),
        event_id="old-completion", idempotency_key="old-complete",
        idempotency_result={},
    )
    provider = VerificationProvider(verdict="failed")
    runtime.task_verification.provider = provider
    runtime.outbox.submit = AsyncMock()
    try:
        result = await runtime.delivery_service.send(
            replace(context, agent_role=AgentRole.CHAT), {
                "task_id": "task-1", "revision": 1,
                "artifacts": [artifact.artifact_id], "text": "old screenshot",
                "idempotency_key": "send-old",
            },
        )
        assert result.error_code == "TASK_VERIFICATION_REJECTED"
        assert provider.calls == 1
        runtime.outbox.submit.assert_not_awaited()
    finally:
        await runtime.store.close()


def test_worker_default_font_directory_contains_system_fonts():
    dockerfile = (
        Path(__file__).parents[1] / "browser" / "Dockerfile"
    ).read_text()
    assert "fonts-noto-cjk" in dockerfile
    assert "fonts-noto-color-emoji" in dockerfile


@pytest.mark.parametrize("verdict", ["passed", "failed", "unknown"])
async def test_browser_screenshot_needs_content_verification_without_corrections(
    tmp_path, verdict,
):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime)
    context = _context(role=AgentRole.TASK, run_id="browser-run", revision=1)
    artifact = await runtime.write_artifact(
        context, filename="browser-1.png", kind="image", content=screenshot(),
        strategy_ref={
            "tool": "delegate_browser_task", "artifact_mode": "screenshot",
        },
    )
    provider = VerificationProvider(verdict=verdict)
    runtime.task_verification.provider = provider
    try:
        result = await runtime.task_commands.complete_task(context, {
            "status": "completed", "summary": "captured", "remaining": [],
            "accepted_criteria": ["result screenshot"],
            "artifact_ids": [artifact.artifact_id], "idempotency_key": "complete",
        })
        assert provider.calls == 1
        ready = await runtime.store.delivery_ready_artifacts(
            task_id="task-1", revision=1, scope_id="group:1",
        )
        if verdict == "passed":
            assert not result.is_error
            assert ready == (artifact,)
        else:
            assert result.error_code == "TASK_VERIFICATION_REJECTED"
            assert ready == ()
    finally:
        await runtime.store.close()


async def test_each_browser_image_requires_its_own_evidence(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime)
    context = _context(role=AgentRole.TASK, run_id="browser-run", revision=1)
    artifacts = [
        await runtime.write_artifact(
            context, filename=f"browser-{index}.png", kind="image",
            content=screenshot(),
            strategy_ref={
                "tool": "delegate_browser_task", "artifact_mode": "screenshot",
            },
        )
        for index in range(2)
    ]
    runtime.task_verification.provider = VerificationProvider()
    try:
        result = await runtime.task_commands.complete_task(context, {
            "status": "completed", "summary": "captured", "remaining": [],
            "accepted_criteria": ["result screenshots"],
            "artifact_ids": [item.artifact_id for item in artifacts],
            "idempotency_key": "complete",
        })
        assert result.error_code == "TASK_VERIFICATION_INVALID"
        assert (await runtime.store.get_task("task-1")).status == "running"
    finally:
        await runtime.store.close()


async def test_concurrent_identical_browser_delegations_share_execution(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("browser-delegation",))
    runtime.task_runtime.runs["task-1"] = runtime.task_runtime.handoff_for_row(
        await runtime.store.get_task("task-1")
    )
    context = _context(role=AgentRole.TASK, run_id="browser-run", revision=1)
    started, release = asyncio.Event(), asyncio.Event()

    async def run(**kwargs):
        assert kwargs["original_requests"] == ()
        started.set()
        await release.wait()
        return BrowserRunResult(
            status=BrowserTaskStatus.PARTIALLY_COMPLETED, summary="unconfirmed",
            error_code="BROWSER_COMPLETION_UNVERIFIED",
        )

    runtime.browser_plugin.runner = SimpleNamespace(run=AsyncMock(side_effect=run))
    arguments = {"goal": "Inspect https://example.com and summarize the page."}
    first = asyncio.create_task(runtime.browser_plugin.delegate(
        runtime.capability_services, context, arguments,
    ))
    second = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(runtime.browser_plugin.delegate(
            runtime.capability_services,
            context,
            arguments,
        ))
        release.set()
        results = await asyncio.gather(first, second)
        assert all(item.error_code == "BROWSER_COMPLETION_UNVERIFIED" for item in results)
        runtime.browser_plugin.runner.run.assert_awaited_once()
        assert runtime.browser_plugin._active == {}
        assert runtime.browser_plugin._active_keys == {}
    finally:
        release.set()
        await asyncio.gather(first, *( [second] if second else []), return_exceptions=True)
        await runtime.store.close()


@pytest.mark.parametrize("changed_goal", [False, True])
async def test_browser_goal_cannot_be_replanned_within_task_revision(
    tmp_path,
    changed_goal,
):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("browser-delegation",))
    runtime.task_runtime.runs["task-1"] = runtime.task_runtime.handoff_for_row(
        await runtime.store.get_task("task-1")
    )
    context = _context(role=AgentRole.TASK, run_id="browser-run", revision=1)
    runtime.browser_plugin.runner = SimpleNamespace(run=AsyncMock(
        return_value=BrowserRunResult(
            status=BrowserTaskStatus.PARTIALLY_COMPLETED, summary="unconfirmed",
            error_code="BROWSER_BUDGET_EXHAUSTED", command_count=71,
            execution_report=browser_report(
                interaction_mode="public_interaction",
                start_url="https://example.com/form",
            ),
        ),
    ))
    arguments = {
        "goal": "Fill and submit the public form at https://example.com/form."
    }
    try:
        first = await runtime.browser_plugin.delegate(
            runtime.capability_services, context, arguments,
        )
        second = await runtime.browser_plugin.delegate(
            runtime.capability_services, context, {
                "goal": (
                    "Finish submitting the form at https://example.com/form."
                    if changed_goal
                    else arguments["goal"]
                ),
            },
        )
        assert first.error_code == "BROWSER_BUDGET_EXHAUSTED"
        assert second.is_error
        assert second.error_code == (
            "BROWSER_REPLAY_BLOCKED" if changed_goal else "BROWSER_BUDGET_EXHAUSTED"
        )
        runtime.browser_plugin.runner.run.assert_awaited_once()
    finally:
        await runtime.store.close()


@pytest.mark.parametrize("new_url", [
    "https://example.com/form?attempt=second",
    "https://example.com/other-form",
    "https://another.example/form",
])
async def test_public_interaction_is_reserved_for_task_revision_not_model_url(tmp_path, new_url):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("browser-delegation",))
    runtime.task_runtime.runs["task-1"] = runtime.task_runtime.handoff_for_row(
        await runtime.store.get_task("task-1")
    )
    context = _context(role=AgentRole.TASK, run_id="browser-run", revision=1)
    runtime.browser_plugin.runner = SimpleNamespace(run=AsyncMock(
        return_value=BrowserRunResult(
            status=BrowserTaskStatus.PARTIALLY_COMPLETED, summary="unconfirmed",
            error_code="BROWSER_BUDGET_EXHAUSTED", command_count=90,
            execution_report=browser_report(
                interaction_mode="public_interaction",
                start_url="https://example.com/form?attempt=first",
            ),
        ),
    ))
    arguments = {
        "goal": (
            "Fill the public form at "
            "https://example.com/form?attempt=first."
        ),
    }
    try:
        await runtime.browser_plugin.delegate(runtime.capability_services, context, arguments)
        second = await runtime.browser_plugin.delegate(
            runtime.capability_services, context,
            {"goal": f"Fill the public form at {new_url}."},
        )
        assert second.error_code == "BROWSER_REPLAY_BLOCKED"
        runtime.browser_plugin.runner.run.assert_awaited_once()
        assert runtime.browser_plugin._operation_key(
            runtime.capability_services,
            replace(context, revision=2),
        ) != runtime.browser_plugin._operation_key(
            runtime.capability_services,
            context,
        )
    finally:
        await runtime.store.close()


async def test_browser_task_revision_accepts_one_complete_goal(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("browser-delegation",))
    runtime.task_runtime.runs["task-1"] = runtime.task_runtime.handoff_for_row(
        await runtime.store.get_task("task-1")
    )
    context = _context(role=AgentRole.TASK, run_id="browser-run", revision=1)
    runtime.browser_plugin.runner = SimpleNamespace(run=AsyncMock(
        return_value=BrowserRunResult(
            status=BrowserTaskStatus.COMPLETED,
            summary="inspected",
            command_count=2,
            execution_report=browser_report(goal_verified=True),
        ),
    ))
    arguments = {"goal": "Inspect the heading at https://example.com."}
    try:
        first = await runtime.browser_plugin.delegate(
            runtime.capability_services,
            context,
            arguments,
        )
        repeated = await runtime.browser_plugin.delegate(
            runtime.capability_services,
            context,
            arguments,
        )
        changed = await runtime.browser_plugin.delegate(
            runtime.capability_services,
            context,
            {"goal": "Inspect the links at https://example.com."},
        )
        for item in (first, repeated):
            assert not item.is_error
        assert changed.error_code == "BROWSER_REPLAY_BLOCKED"
        assert runtime.browser_plugin.runner.run.await_count == 1
    finally:
        await runtime.store.close()


@pytest.mark.parametrize("interrupted", [False, True])
async def test_browser_restart_retains_reservation_or_releases_safe_retry(tmp_path, interrupted):
    from capabilities.browser import BrowserCapabilityPlugin

    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("browser-delegation",))
    runtime.task_runtime.runs["task-1"] = runtime.task_runtime.handoff_for_row(
        await runtime.store.get_task("task-1")
    )
    context = _context(role=AgentRole.TASK, run_id="browser-run", revision=1)
    runner = SimpleNamespace(run=AsyncMock(
        side_effect=asyncio.CancelledError if interrupted else None,
        return_value=BrowserRunResult(
            status=BrowserTaskStatus.FAILED, summary="worker busy",
            error_code="BROWSER_WORKER_BUSY", retryable=True, command_count=0,
        ),
    ))
    runtime.browser_plugin.runner = runner
    arguments = {
        "goal": "Submit the public form at https://example.com/form."
    }
    try:
        if interrupted:
            with pytest.raises(asyncio.CancelledError):
                await runtime.browser_plugin.delegate(
                    runtime.capability_services, context, arguments,
                )
        else:
            first = await runtime.browser_plugin.delegate(
                runtime.capability_services, context, arguments,
            )
            assert first.retryable
        restarted = BrowserCapabilityPlugin()
        restarted.runner = SimpleNamespace(run=AsyncMock(return_value=BrowserRunResult(
            status=BrowserTaskStatus.COMPLETED,
            summary="submitted",
            command_count=4,
            execution_report=browser_report(
                interaction_mode="public_interaction",
                start_url="https://example.com/form",
                goal_verified=True,
            ),
        )))
        result = await restarted.delegate(
            runtime.capability_services, context,
            arguments,
        )
        if interrupted:
            assert result.error_code == "BROWSER_PREVIOUS_EXECUTION_UNKNOWN"
            restarted.runner.run.assert_not_awaited()
        else:
            assert not result.is_error
            restarted.runner.run.assert_awaited_once()
    finally:
        await runtime.store.close()

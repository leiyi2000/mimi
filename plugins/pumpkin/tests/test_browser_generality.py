import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from browser.sdk_protocol import SdkError, SdkMethod
from browser.models import BrowserExecutionState
from core.domain import AgentRole
from providers.model import ModelProviderError
from runtime.browser_agent import BrowserAgentRunner, BrowserRunResult, BrowserTaskStatus
from test_browser_agent import FakeProvider, FakeWorker, finish, plan, response
from test_browser_dom_integration import find
from test_task_corrections import _context, _create_running_task, _runtime

pytest_plugins = ("test_browser_dom_integration",)


@pytest.mark.parametrize("mode", ["read_only", "public_interaction", "public_submission"])
async def test_all_browser_modes_block_unverified_parent_completion(tmp_path, mode):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("browser-delegation",))
    runtime.task_runtime.runs["task-1"] = runtime.task_runtime.handoff_for_row(
        await runtime.store.get_task("task-1")
    )
    context = _context(role=AgentRole.TASK, run_id="browser-run", revision=1)
    runtime.browser_plugin.runner = SimpleNamespace(run=AsyncMock(return_value=BrowserRunResult(
        BrowserTaskStatus.PARTIALLY_COMPLETED, "Not verified", command_count=1,
        execution_report={"goal_verified": False, "task_contract": {"interaction_mode": mode}},
    )))
    try:
        await runtime.browser_plugin.delegate(runtime.capability_services, context, {"goal": "Inspect page"})
        completed = await runtime.task_commands.complete_task(context, {
            "status": "completed", "summary": "done", "remaining": [],
            "accepted_criteria": ["page verified"], "artifact_ids": [], "idempotency_key": "complete",
        })
        assert completed.error_code == "BROWSER_INTERACTION_UNVERIFIED"
        assert (await runtime.store.get_task("task-1")).status == "running"
    finally:
        await runtime.store.close()


async def test_original_requests_reach_every_execution_decision():
    worker = FakeWorker()
    requests = ("Inspect and edit the draft; leave it unsent.", "Keep the optional note empty.")
    provider = FakeProvider([
        plan(), response("browser_execute", {"program": "return 1"}), finish(evidence=["opened"]),
    ])
    await BrowserAgentRunner(provider=provider, worker=worker).run(goal="Inspect page", original_requests=requests)
    for messages in provider.messages:
        assert json.loads(messages[-1].content)["original_user_requests"] == list(requests)


@pytest.mark.parametrize("method", ["submit", "click", "press", "navigate"])
@pytest.mark.parametrize("mode", ["read_only", "public_interaction", "public_submission"])
async def test_transient_model_failure_uses_same_final_verification(method, mode):
    class InterruptedProvider(FakeProvider):
        async def stream(self, messages, tools, *, mode):
            context = json.loads(messages[-1].content)
            if len(self.messages) == 2 and not context.get("finalization"):
                self.messages.append(messages)
                raise ModelProviderError("fixture timeout", code="MODEL_TIMEOUT", retryable=True)
            async for item in super().stream(messages, tools, mode=mode):
                yield item

    worker = FakeWorker()
    worker.final_text = "The draft is stored."
    worker.program_results = [{
        "visible_text": worker.final_text,
        "action_ledger": [{"action_sequence": 2, "method": method, "status": "succeeded"}],
    }]
    provider = InterruptedProvider([
        plan(interaction_mode=mode, expected_output="screenshot", artifact_mode="screenshot"),
        response("browser_execute", {"program": "return 1"}),
        finish(evidence=[worker.final_text]),
    ])
    result = await BrowserAgentRunner(provider=provider, worker=worker).run(
        goal="Store the draft and screenshot", original_requests=("Store the draft; do not publish.",),
    )
    assert result.status is BrowserTaskStatus.COMPLETED
    assert result.artifacts and result.execution_report["goal_verified"]
    assert len(worker.calls) == 1 and worker.cancelled
    context = json.loads(provider.messages[-1][-1].content)
    assert context["original_user_requests"] == ["Store the draft; do not publish."]
    assert context["finalization"]["reason_code"] == "MODEL_TIMEOUT"
    assert context["initial_observation"]["visible_text"] == worker.final_text
    assert provider.tools[-1] == {"finish_browser_task"}


@pytest.mark.parametrize("key,value,expected", [
    ("outcome_unknown", True, "MODEL_TIMEOUT"),
    ("finalization_id", "", "BROWSER_COMPLETION_UNVERIFIED"),
    ("digest", "invalid", "BROWSER_ARTIFACT_EVIDENCE_MISMATCH"),
])
async def test_finalization_never_bypasses_unknown_or_artifact_checks(key, value, expected):
    class InterruptedProvider(FakeProvider):
        async def stream(self, messages, tools, *, mode):
            if len(self.messages) == 1:
                self.messages.append(messages)
                raise ModelProviderError("fixture timeout", code="MODEL_TIMEOUT", retryable=True)
            async for item in super().stream(messages, tools, mode=mode):
                yield item

    worker = FakeWorker()
    worker.capture_overrides[key] = value
    result = await BrowserAgentRunner(provider=InterruptedProvider([
        plan(expected_output="screenshot", artifact_mode="screenshot"), finish(evidence=["opened"]),
    ]), worker=worker).run(goal="Inspect page")
    assert result.status is BrowserTaskStatus.PARTIALLY_COMPLETED
    assert not result.artifacts and result.error_code == expected


async def test_native_confirm_is_pending_until_explicit_model_choice(engine):
    await engine.page.set_content("""<button onclick="
    document.body.innerHTML=confirm('Continue operation?') ? '<h1>Accepted</h1>' : '<h1>Cancelled</h1>'
    ">Continue</button>""")
    button = await find(engine, role="button")
    receipt = await engine.sdk_call(SdkMethod.CLICK, {"ref": button["ref"]}, 2)
    assert receipt["status"] == "pending"
    observed = await engine.snapshot()
    assert observed.dialog["type"] == "confirm"
    assert observed.dialog["message"] == "Continue operation?"
    assert observed.observed_after_action_seq < receipt["action_sequence"]
    handled = await engine.sdk_call(SdkMethod.HANDLE_DIALOG, {
        "dialog_id": observed.dialog["dialog_id"], "accept": True,
    }, 3)
    assert handled["status"] == "succeeded"
    assert "Accepted" in (await engine.snapshot()).visible_text
    assert len(engine.ledger) == 1 and not engine.outcome_unknown


@pytest.mark.parametrize("key,expected", [("ControlOrMeta+A", "New"), ("Home", "NewOld")])
async def test_type_preserves_selection_and_caret_semantics(engine, key, expected):
    await engine.page.set_content("<label>Draft<input value=Old></label>")
    field = await find(engine, role="textbox")
    await engine.sdk_call(SdkMethod.PRESS, {"ref": field["ref"], "key": key}, 2)
    receipt = await engine.sdk_call(SdkMethod.TYPE, {"ref": field["ref"], "value": "New"}, 3)
    assert receipt["status"] == "succeeded"
    assert receipt["confirmed_state"] == {"value": expected}
    await engine.sdk_call(SdkMethod.ASSERT, {"kind": "value", "ref": field["ref"], "expected": expected}, 4)
    assert not engine.outcome_unknown


@pytest.mark.parametrize("accept", [True, False])
async def test_prompt_input_and_dismiss_are_explicit_choices(engine, accept):
    await engine.page.set_content("""<button onclick="
    let answer=prompt('Draft title?', 'Default'); document.querySelector('output').textContent=String(answer)
    ">Edit</button><output>Waiting</output>""")
    receipt = await engine.sdk_call(SdkMethod.CLICK, {"ref": (await find(engine, role="button"))["ref"]}, 1)
    dialog = (await engine.snapshot()).dialog
    assert receipt["status"] == "pending" and dialog["default_value"] == "Default"
    with pytest.raises(SdkError):
        await engine.sdk_call(SdkMethod.HANDLE_DIALOG, {"dialog_id": "stale", "accept": accept}, 2)
    with pytest.raises(SdkError):
        await engine.capture("screenshot")
    with pytest.raises(SdkError):
        await engine.sdk_call(SdkMethod.CLICK, {"ref": receipt["ref"]}, 3)
    await engine.sdk_call(SdkMethod.HANDLE_DIALOG, {
        "dialog_id": dialog["dialog_id"], "accept": accept, **({"prompt_text": "Fixture title"} if accept else {}),
    }, 4)
    observed = await engine.snapshot()
    assert not observed.dialog
    assert ("Fixture title" if accept else "null") in observed.visible_text
    assert len(engine.ledger) == 1 and not engine.outcome_unknown


async def test_consecutive_dialogs_keep_the_same_action_pending(engine):
    await engine.page.set_content("""<button onclick="
    if(confirm('Continue?')) {alert('Notice'); document.body.innerHTML='<h1>Finished</h1>'}
    ">Continue</button>""")
    await engine.sdk_call(SdkMethod.CLICK, {"ref": (await find(engine, role="button"))["ref"]}, 1)
    first = (await engine.snapshot()).dialog
    receipt = await engine.sdk_call(SdkMethod.HANDLE_DIALOG, {"dialog_id": first["dialog_id"], "accept": True}, 2)
    second = (await engine.snapshot()).dialog
    assert receipt["status"] == "pending" and second["type"] == "alert"
    assert first["dialog_id"] != second["dialog_id"]
    await engine.sdk_call(SdkMethod.HANDLE_DIALOG, {"dialog_id": second["dialog_id"], "accept": True}, 3)
    assert "Finished" in (await engine.snapshot()).visible_text
    assert len(engine.ledger) == 1 and engine.ledger[0].status == "succeeded"


async def test_pending_action_report_merges_confirmation_without_duplicate():
    state = BrowserExecutionState()
    worker = FakeWorker()
    pending = {"action_sequence": 2, "method": "click", "status": "pending"}
    state.record({"action_ledger": [pending], "final_observation": worker.snapshot})
    state.record({"action_ledger": [{**pending, "status": "succeeded"}], "final_observation": worker.snapshot})
    assert len(state.actions) == 1 and state.actions[0]["status"] == "succeeded"


@pytest.mark.parametrize("trigger", ["deadline", "max_tokens"])
async def test_finalization_uses_reserved_time_and_remaining_decisions(trigger):
    class InterruptedProvider(FakeProvider):
        async def stream(self, messages, tools, *, mode):
            context = json.loads(messages[-1].content)
            if len(self.messages) == 2 and not context.get("finalization"):
                self.messages.append(messages)
                if trigger == "deadline":
                    await asyncio.Event().wait()
                else:
                    yield replace(finish(), finish_reason="max_tokens")
                return
            async for item in super().stream(messages, tools, mode=mode):
                yield item

    worker = FakeWorker()
    worker.final_text = "Draft saved."
    provider = InterruptedProvider([
        plan(), response("browser_execute", {"program": "return 1"}), finish(evidence=[worker.final_text]),
    ])
    runner = BrowserAgentRunner(
        provider=provider, worker=worker, timeout_seconds=.2 if trigger == "deadline" else 10,
    )
    started = asyncio.get_running_loop().time()
    result = await runner.run(goal="Save draft")
    assert result.status is BrowserTaskStatus.COMPLETED and len(worker.calls) == 1
    assert result.steps <= runner.max_steps and worker.cancelled
    if trigger == "deadline":
        assert asyncio.get_running_loop().time() - started < .5


async def test_exhausted_decisions_capture_final_page_without_extra_model_call():
    worker = FakeWorker()
    worker.final_text = "Current draft."
    provider = FakeProvider([plan(), response("browser_execute", {"program": "return 1"})])
    result = await BrowserAgentRunner(provider=provider, worker=worker, max_steps=2).run(goal="Inspect draft")
    assert result.status is BrowserTaskStatus.PARTIALLY_COMPLETED
    assert len(provider.messages) == 2 and not worker.calls

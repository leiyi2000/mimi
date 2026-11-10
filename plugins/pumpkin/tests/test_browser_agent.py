import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator

import pytest

from core.agent_loop import AgentLoop
from core.config import BrowserConfig
from core.domain import ModelMessage, ModelResponse, ToolCall
from browser.tools import PLAN_TOOL
from providers.browser_worker import BrowserWorkerError, BrowserWorkerRun
from runtime.browser_agent import BrowserAgentRunner, BrowserTaskStatus


async def public_resolver(host: str, port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


def response(name: str, arguments: dict | None = None) -> ModelResponse:
    return ModelResponse(
        message=ModelMessage("assistant", "", tool_calls=(ToolCall("call", name, arguments or {}),)),
        request_id="request", provider="fake", model="fake",
    )


def plan(
    *, start_url="https://example.com/form", expected_output="summary",
    interaction_mode="read_only", artifact_mode="none",
    required_capabilities=None, required_evidence=None,
):
    return response("define_browser_task", {
        "start_url": start_url, "expected_output": expected_output,
        "interaction_mode": interaction_mode, "artifact_mode": artifact_mode,
        "required_capabilities": required_capabilities or [], "required_evidence": required_evidence or [],
    })


def finish(*, status="completed", summary="done", evidence=None, facts=None, links=None):
    return response("finish_browser_task", {
        "status": status, "summary": summary, "evidence": evidence or [],
        "facts": facts or [], "links": links or [],
    })


class FakeProvider:
    def __init__(self, responses):
        self.responses, self.messages, self.tools = list(responses), [], []

    async def stream(self, messages, tools, *, mode) -> AsyncIterator[ModelResponse]:
        assert mode == "browser"
        self.messages.append(messages)
        self.tools.append({tool.name for tool in tools})
        yield self.responses.pop(0) if self.responses else finish(status="partially_completed")


class FakeWorker:
    def __init__(self):
        self.create_calls = 0
        self.capacity_failures = 0
        self.cancelled = False
        self.calls = []
        self.program_results = []
        self.final_text = "opened"
        self.capture_overrides = {}
        self.snapshot = {
            "url": "https://example.com/form", "title": "Example", "visible_text": "opened",
            "elements": [], "document_epoch": 1, "resource_version": 1,
            "observation_id": "initial", "observed_after_action_seq": 1,
            "blocked_requests": 0, "truncated": False,
        }

    async def create_run(self, *, contract, task_owner=""):
        self.create_calls += 1
        if self.create_calls <= self.capacity_failures:
            raise BrowserWorkerError("BROWSER_CAPACITY_EXHAUSTED", "busy", retryable=True)
        return BrowserWorkerRun(
            "run", "capability", task_owner=task_owner, sdk_version=1,
            initial_observation=dict(self.snapshot), engine="playwright",
            capabilities=frozenset({"playwright", "viewport_screenshot", "full_page_screenshot", "pdf", "full_page_content"}),
        )

    async def execute_program(self, run, *, execution_id, program, timeout_seconds):
        self.calls.append(program)
        value = self.program_results.pop(0) if self.program_results else {}
        self.snapshot["visible_text"] = value.pop("visible_text", "filled")
        self.snapshot["observed_after_action_seq"] += 1
        return {
            "status": "succeeded", "execution_id": execution_id,
            "return_value": {"done": True}, "sdk_call_count": 1, "action_ledger": [],
            "final_observation": dict(self.snapshot),
            "last_confirmed_action_seq": self.snapshot["observed_after_action_seq"],
            **value,
        }

    async def capture(self, run, *, mode, full_page=False, submission_evidence=()):
        self.snapshot.update(visible_text=self.final_text, observation_id="final")
        content = b"\x89PNG\r\n\x1a\nfixture" if mode == "screenshot" else b"%PDF-fixture"
        return {
            "final_observation": dict(self.snapshot),
            "observation_id": "final", "resource_version": 1,
            "finalization_id": "finalization", "outcome_unknown": False,
            "last_confirmed_action_seq": self.snapshot["observed_after_action_seq"],
            "content": base64.b64encode(content).decode(),
            "digest": hashlib.sha256(content).hexdigest(),
            "media_type": "image/png" if mode == "screenshot" else "application/pdf",
            "submission_verified": bool(submission_evidence),
            **self.capture_overrides,
        }

    async def heartbeat(self, run):
        pass

    async def cancel_run(self, run):
        self.cancelled = True


async def execute(provider, worker, **kwargs):
    runner = BrowserAgentRunner(provider=provider, worker=worker)
    assert isinstance(runner.agent_loop, AgentLoop)
    return await runner.run(goal="Read https://example.com/form", **kwargs)


async def test_profile_uses_shared_loop_and_only_program_tools():
    worker = FakeWorker()
    worker.final_text = "filled"
    provider = FakeProvider([
        plan(), response("browser_execute", {"program": 'return {"done": True}'}),
        finish(evidence=["filled"]),
    ])
    result = await execute(provider, worker)
    assert result.status is BrowserTaskStatus.COMPLETED
    assert provider.tools == [
        {"define_browser_task", "reject_browser_task"}, {"browser_execute", "finish_browser_task"},
        {"browser_execute", "finish_browser_task"},
    ]
    assert worker.cancelled and len(worker.calls) == 1


async def test_observation_program_keeps_cumulative_actions_in_model_context():
    worker = FakeWorker()
    action = {"action_sequence": 2, "method": "click", "ref": "entry", "status": "succeeded"}
    worker.program_results = [{"action_ledger": [action]}, {"action_ledger": []}]
    provider = FakeProvider([
        plan(), response("browser_execute", {"program": "return 1"}),
        response("browser_execute", {"program": "return 2"}), finish(evidence=["opened"]),
    ])
    await execute(provider, worker)
    context = json.loads(provider.messages[-1][-1].content)
    assert context["last_program"]["action_ledger"] == []
    assert context["action_ledger"] == [action]
    assert "final_observation" not in context["last_program"]


async def test_contract_without_optional_text_evidence_can_start():
    compiled = plan()
    compiled.message.tool_calls[0].arguments.pop("required_evidence")
    provider = FakeProvider([compiled, finish(evidence=["opened"])])
    worker = FakeWorker()
    result = await execute(provider, worker)
    assert result.status is BrowserTaskStatus.COMPLETED
    assert worker.create_calls == 1


async def test_generated_contract_cannot_require_a_guessed_success_message():
    worker = FakeWorker()
    worker.final_text = "Your response has been received. Thank you!"
    provider = FakeProvider([
        plan(
            interaction_mode="public_submission", expected_output="screenshot",
            artifact_mode="screenshot",
            required_evidence=[{"channel": "browser_snapshot", "text": "Submission successful"}],
        ),
        finish(evidence=[worker.final_text]),
    ])
    result = await execute(provider, worker)
    assert result.status is BrowserTaskStatus.COMPLETED
    assert result.artifacts and result.execution_report["goal_verified"]
    assert "required_evidence" not in PLAN_TOOL.input_schema["properties"]
    assert not result.execution_report["task_contract"]["required_evidence"]


async def test_default_budget_keeps_execution_available_for_a_fourth_program():
    worker = FakeWorker()
    worker.final_text = "Your response has been received."
    worker.program_results = [{}, {}, {}, {
        "action_ledger": [{
            "action_sequence": 5, "method": "click", "status": "succeeded",
        }],
    }]
    provider = FakeProvider([
        plan(
            interaction_mode="public_submission", expected_output="screenshot",
            artifact_mode="screenshot",
        ),
        *[response("browser_execute", {"program": f"return {index}"}) for index in range(4)],
        finish(evidence=[worker.final_text]),
    ])
    result = await execute(provider, worker)
    assert result.status is BrowserTaskStatus.COMPLETED
    assert result.artifacts and len(worker.calls) == 4
    assert "browser_execute" in provider.tools[4]


@pytest.mark.parametrize("overrides,expected", [
    ({"outcome_unknown": True}, "BROWSER_OUTCOME_UNKNOWN"),
    ({"finalization_id": ""}, "BROWSER_COMPLETION_UNVERIFIED"),
    ({"last_confirmed_action_seq": 99}, "BROWSER_COMPLETION_UNVERIFIED"),
])
async def test_completion_requires_final_evidence_and_action_coverage(overrides, expected):
    worker = FakeWorker()
    worker.capture_overrides = overrides
    result = await execute(FakeProvider([plan(), finish(evidence=["opened"])]), worker)
    assert result.status is BrowserTaskStatus.PARTIALLY_COMPLETED
    assert result.error_code == expected
    assert not result.artifacts


async def test_program_return_value_cannot_prove_completion():
    worker = FakeWorker()
    result = await execute(FakeProvider([
        plan(), response("browser_execute", {"program": "return {'done': True}"}),
        finish(evidence=["done"]),
    ]), worker)
    assert result.error_code == "BROWSER_COMPLETION_UNVERIFIED"


async def test_blocked_background_requests_do_not_replace_completion_evidence():
    worker = FakeWorker()
    worker.snapshot["blocked_requests"] = 2
    result = await execute(FakeProvider([plan(), finish(evidence=["opened"])]), worker)
    assert result.status is BrowserTaskStatus.COMPLETED
    assert result.execution_report["blocked_requests"] == 2
    worker = FakeWorker()
    worker.snapshot["blocked_requests"] = 2
    result = await execute(FakeProvider([plan(), finish(evidence=["missing"])]), worker)
    assert result.status is BrowserTaskStatus.PARTIALLY_COMPLETED
    assert result.error_code == "BROWSER_COMPLETION_UNVERIFIED"


async def test_facts_must_be_present_in_final_page():
    result = await execute(FakeProvider([plan(), finish(evidence=["opened"], facts=["invented fact"])]), FakeWorker())
    assert result.error_code == "BROWSER_COMPLETION_UNVERIFIED"


async def test_capture_refreshes_observation_binding():
    worker = FakeWorker()
    result = await execute(FakeProvider([
        plan(expected_output="screenshot", artifact_mode="screenshot"),
        finish(evidence=["opened"]),
    ]), worker)
    assert result.status is BrowserTaskStatus.COMPLETED
    assert result.artifacts[0].evidence_id == "final"
    assert result.artifacts[0].finalization_id == "finalization"


async def test_capture_digest_mismatch_is_rejected():
    worker = FakeWorker()
    worker.capture_overrides = {"digest": "wrong"}
    result = await execute(FakeProvider([
        plan(expected_output="screenshot", artifact_mode="screenshot"),
        finish(evidence=["opened"]),
    ]), worker)
    assert result.error_code == "BROWSER_ARTIFACT_EVIDENCE_MISMATCH"


async def test_invalid_source_is_returned_for_correction_without_dispatch():
    worker = FakeWorker()
    provider = FakeProvider([
        plan(), response("browser_execute", {"program": "import os"}), finish(evidence=["opened"]),
    ])
    result = await execute(provider, worker)
    assert result.status is BrowserTaskStatus.COMPLETED
    assert not worker.calls
    assert "BROWSER_PROGRAM_INVALID" in provider.messages[-1][-1].content


async def test_source_rejection_returns_exact_field_and_keeps_program_budget():
    worker = FakeWorker()
    provider = FakeProvider([
        plan(), response("browser_execute", {"program": "x = 1\nreturn browser._transport"}),
        response("browser_execute", {"program": "return 1"}), finish(evidence=["opened"]),
    ])
    result = await execute(provider, worker)
    context = json.loads(provider.messages[2][-1].content)
    assert context["last_program"]["error"]["field"] == "program.line[2]"
    assert context["remaining_programs"] == 8
    assert worker.calls == ["return 1"]
    assert result.status is BrowserTaskStatus.COMPLETED


async def test_program_history_preserves_progress_without_repeating_snapshots():
    worker = FakeWorker()
    worker.program_results = [
        {"return_value": {"progress": {"completed": ["first"], "pending": ["second"]}}},
        {"return_value": {"count": 2}},
    ]
    provider = FakeProvider([
        plan(), response("browser_execute", {"program": "return {'progress': 'first'}"}),
        response("browser_execute", {"program": "return 2"}), finish(evidence=["opened"]),
    ])
    await execute(provider, worker)
    context = json.loads(provider.messages[-1][-1].content)
    history = context["program_history"]
    assert history[0]["program"] == "return {'progress': 'first'}"
    assert history[0]["result"]["return_value"]["progress"]["pending"] == ["second"]
    assert history[1]["result"]["return_value"] == {"count": 2}
    assert all("final_observation" not in item["result"] for item in history)
    assert all("action_ledger" not in item["result"] for item in history)


async def test_program_history_is_bounded_with_explicit_omissions():
    worker = FakeWorker()
    worker.program_results = [
        {"return_value": {"progress": "a" * 20_000}},
        {"return_value": {"progress": "b" * 20_000}},
    ]
    provider = FakeProvider([
        plan(), response("browser_execute", {"program": "return 1"}),
        response("browser_execute", {"program": "return 2"}), finish(evidence=["opened"]),
    ])
    await execute(provider, worker)
    context = json.loads(provider.messages[-1][-1].content)
    assert len(json.dumps(context["program_history"], ensure_ascii=False).encode()) <= 24 * 1024
    assert context["program_history_omitted"] == 1
    assert context["program_history"][-1]["program"] == "return 2"


async def test_last_decision_reserves_finish_and_exposes_time_budget():
    worker = FakeWorker()
    provider = FakeProvider([plan(), finish(evidence=["opened"])])
    runner = BrowserAgentRunner(
        provider=provider, worker=worker, max_steps=2,
    )
    result = await runner.run(goal="Read https://example.com/form")
    context = json.loads(provider.messages[-1][-1].content)
    assert provider.tools[-1] == {"finish_browser_task"}
    assert context["remaining_decisions"] == 1
    assert 0 < context["remaining_seconds"] <= 720
    assert context["remaining_execution_seconds"] < context["remaining_seconds"]
    assert result.status is BrowserTaskStatus.COMPLETED


@pytest.mark.parametrize("bad_arguments,error", [
    ({"status": "unknown", "summary": "done", "evidence": ["opened"]}, "BROWSER_FINISH_INVALID"),
    ({"status": "completed", "summary": "done", "evidence": ["invented"]}, "BROWSER_COMPLETION_UNVERIFIED"),
])
async def test_finish_can_correct_arguments_once_without_replaying_actions(bad_arguments, error):
    worker = FakeWorker()
    provider = FakeProvider([
        plan(), response("finish_browser_task", bad_arguments), finish(evidence=["opened"]),
    ])
    result = await execute(provider, worker)
    assert result.status is BrowserTaskStatus.COMPLETED
    assert not worker.calls
    context = json.loads(provider.messages[-1][-1].content)
    assert context["finish_error"]["code"] == error
    assert provider.tools[-1] == {"finish_browser_task"}


@pytest.mark.parametrize("correction_status", ["completed", "partially_completed"])
async def test_finish_correction_does_not_loop_or_unfreeze_unknown_outcome(correction_status):
    worker = FakeWorker()
    provider = FakeProvider([
        plan(), finish(evidence=["missing"]), finish(status=correction_status, evidence=["missing"]),
    ])
    result = await execute(provider, worker)
    assert result.error_code == "BROWSER_COMPLETION_UNVERIFIED"
    assert len(provider.messages) == 3
    assert result.steps == 3
    worker = FakeWorker()
    worker.capture_overrides["outcome_unknown"] = True
    provider = FakeProvider([plan(), finish(evidence=["opened"]), finish(evidence=["opened"])])
    result = await execute(provider, worker)
    assert result.error_code == "BROWSER_OUTCOME_UNKNOWN"
    assert len(provider.messages) == 2
    assert not worker.calls


async def test_model_wall_timeout_releases_worker_with_finalization_time():
    class SlowProvider(FakeProvider):
        async def stream(self, messages, tools, *, mode):
            if not self.messages:
                self.messages.append(messages)
                yield plan()
                return
            await asyncio.Event().wait()

    worker = FakeWorker()
    runner = BrowserAgentRunner(
        provider=SlowProvider([]), worker=worker, model_timeout_seconds=.02,
    )
    result = await runner.run(goal="Read https://example.com/form")
    assert result.error_code == "MODEL_TIMEOUT"
    assert worker.cancelled


async def test_submission_contract_needs_no_original_authorization():
    worker = FakeWorker()
    result = await execute(FakeProvider([plan(interaction_mode="public_submission")]), worker)
    assert worker.create_calls == 1
    assert result.error_code != "BROWSER_INTERACTION_NOT_ALLOWED"


async def test_persistent_model_timeout_preserves_observation_without_claiming_goal_success():
    class SlowFinishProvider(FakeProvider):
        async def stream(self, messages, tools, *, mode):
            if len(self.messages) == 2:
                await asyncio.Event().wait()
            async for value in super().stream(messages, tools, mode=mode):
                yield value

    worker = FakeWorker()
    worker.final_text = "Accepted"
    worker.program_results = [{
        "visible_text": "Accepted",
        "action_ledger": [{
            "method": "submit", "action_sequence": 2, "status": "succeeded",
            "confirmed_state": {"success_text": "Accepted", "response_status": 200},
        }],
    }]
    runner = BrowserAgentRunner(
        provider=SlowFinishProvider([
            plan(interaction_mode="public_submission", expected_output="screenshot", artifact_mode="screenshot"),
            response("browser_execute", {"program": "return browser.snapshot().title"}),
        ]),
        worker=worker, model_timeout_seconds=.02,
    )
    result = await runner.run(
        goal="Submit https://example.com/form and screenshot",
        original_requests=("Submit https://example.com/form and screenshot",),
    )
    assert result.status is BrowserTaskStatus.PARTIALLY_COMPLETED
    assert not result.artifacts and not result.execution_report["goal_verified"]
    assert result.error_code == "MODEL_TIMEOUT"
    assert result.execution_report["last_observation"]["text"] == "Accepted"
    assert len(worker.calls) == 1 and worker.cancelled


async def test_submission_gate_uses_final_page_evidence_without_special_receipt():
    worker = FakeWorker()
    worker.final_text = "Accepted"
    result = await execute(
        FakeProvider([plan(interaction_mode="public_submission"), finish(evidence=["Accepted"])]),
        worker, original_requests=("Submit https://example.com/form",),
    )
    assert result.status is BrowserTaskStatus.COMPLETED
    worker.final_text = "opened"
    result = await execute(
        FakeProvider([plan(interaction_mode="public_submission"), finish(evidence=["Accepted"])]),
        worker,
    )
    assert result.error_code == "BROWSER_COMPLETION_UNVERIFIED"


async def test_finish_correction_keeps_state_checks_strict():
    worker = FakeWorker()
    worker.snapshot["elements"] = [{
        "ref": "input", "frame_id": "main", "document_epoch": 1,
        "role": "checkbox", "name": "Choice", "checked": True,
    }]
    bad_finish = finish(evidence=["opened"])
    bad_finish.message.tool_calls[0].arguments["checked_checks"] = [
        {"ref": "input", "expected": "true"},
    ]
    good_finish = finish(evidence=["opened"])
    good_finish.message.tool_calls[0].arguments["checked_checks"] = [
        {"ref": "input", "expected": True},
    ]
    provider = FakeProvider([plan(), bad_finish, good_finish])
    result = await execute(provider, worker)
    assert result.status is BrowserTaskStatus.COMPLETED
    assert json.loads(provider.messages[-1][-1].content)["finish_error"]["code"] == "BROWSER_STATE_CHECK_INVALID"
    assert not worker.calls


async def test_program_cannot_run_during_finish_argument_correction():
    worker = FakeWorker()
    provider = FakeProvider([
        plan(), finish(evidence=["missing"]), response("browser_execute", {"program": "return 1"}),
    ])
    result = await execute(provider, worker)
    assert result.error_code == "BROWSER_TOOL_REJECTED"
    assert not worker.calls


async def test_program_budget_keeps_finalization_available():
    worker = FakeWorker()
    worker.final_text = "filled"
    provider = FakeProvider([
        plan(), *[response("browser_execute", {"program": "return {}"}) for _ in range(8)],
        finish(evidence=["filled"]),
    ])
    result = await execute(provider, worker)
    assert result.status is BrowserTaskStatus.COMPLETED
    assert result.command_count == 8
    assert provider.tools[-1] == {"finish_browser_task"}

async def test_unknown_outcome_hides_program_execution():
    worker = FakeWorker()
    worker.program_results = [{"status": "outcome_unknown", "outcome_unknown": True}]
    provider = FakeProvider([
        plan(), response("browser_execute", {"program": "return {}"}),
        finish(status="partially_completed"),
    ])
    result = await execute(provider, worker)
    assert result.status is BrowserTaskStatus.PARTIALLY_COMPLETED
    assert provider.tools[-1] == {"finish_browser_task"}


async def test_compiler_may_choose_url_and_actions_from_goal_context():
    for task_plan in [plan(start_url="https://other.example"), plan(interaction_mode="public_interaction")]:
        worker = FakeWorker()
        result = await execute(FakeProvider([task_plan]), worker)
        assert worker.create_calls == 1
        assert result.error_code != "BROWSER_INTERACTION_NOT_ALLOWED"


async def test_capacity_wait_is_bounded():
    worker = FakeWorker()
    worker.capacity_failures = 100
    runner = BrowserAgentRunner(
        provider=FakeProvider([plan()]), worker=worker,
        policy=BrowserConfig(capacity_wait_seconds=.02, capacity_poll_seconds=.001),
    )
    result = await runner.run(goal="Read https://example.com/form")
    assert result.error_code == "BROWSER_CAPACITY_WAIT_TIMEOUT"
    assert result.retryable


async def test_cancellation_waits_for_worker_cleanup():
    started, cleaned = asyncio.Event(), asyncio.Event()

    class WaitingProvider(FakeProvider):
        async def stream(self, messages, tools, *, mode):
            if not started.is_set():
                started.set()
                await asyncio.Event().wait()
            yield finish()

    class SlowWorker(FakeWorker):
        async def cancel_run(self, run):
            await cleaned.wait()
            await super().cancel_run(run)

    worker = SlowWorker()
    runner = BrowserAgentRunner(provider=WaitingProvider([]), worker=worker)
    from runtime.browser_agent import BrowserArtifactMode, BrowserExpectedOutput, BrowserInteractionMode
    task = asyncio.create_task(runner.run_contract(
        goal="Read https://example.com/form", start_url="https://example.com/form",
        expected_output=BrowserExpectedOutput.SUMMARY, interaction_mode=BrowserInteractionMode.READ_ONLY,
        artifact_mode=BrowserArtifactMode.NONE,
    ))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    cleaned.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.cancelled

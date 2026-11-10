"""Run in the Worker image with a separate network-less Runner and SDK volume."""

import asyncio
import json
import os
from pathlib import Path

from browser.engine import PlaywrightRun
from browser.protocol import PROTOCOL_VERSION
from browser.sdk_protocol import SdkError
from browser.worker import BrowserWorker


PAGE = """<!doctype html><title>SDK integration</title><body>
<label for=name>Name</label><input id=name oninput="
const age=document.querySelector('#age');
if(age && !age.dataset.replaced) age.outerHTML='<input id=age data-replaced=1>'">
<label for=age>Age</label><input id=age><output>Ready</output></body>"""
SUBMISSION_PAGE = """<!doctype html><title>Public form</title>
<form action="https://example.com/accept" method=post>
<label>Name<input name=person required></label><label>Optional<input name=note></label>
<button type=submit>Send</button></form>"""
DIALOG_PAGE = """<!doctype html><title>Dialog fixture</title>
<button onclick="let value=prompt('Draft title?', 'Default');
if(value!==null && confirm('Store draft?')) document.querySelector('output').textContent=value;
">Edit</button><output>Waiting</output>"""


async def main() -> None:
    original_start = PlaywrightRun.start
    fixture_page = PAGE

    async def fixture_start(engine):
        await original_start(engine)

        async def route_fixture(route, request):
            if request.method == "GET" and request.url == "https://example.com/fixture":
                await route.fulfill(
                    status=200, content_type="text/html",
                    body=fixture_page,
                )
            elif request.url == "https://example.com/accept":
                await route.fulfill(
                    status=200, content_type="text/html",
                    body="<title>Receipt</title><h1>Saved successfully</h1>",
                )
            else:
                await route.abort()

        await engine.context.route("**/*", route_fixture)

    PlaywrightRun.start = fixture_start
    worker = BrowserWorker(
        token="fixture-control", runner_token=os.environ["BROWSER_CODE_RUNNER_TOKEN"],
        runner_socket=os.environ["BROWSER_CODE_RUNNER_SOCKET"],
        sdk_socket_dir=Path(os.environ["BROWSER_WORKER_SDK_SOCKET_DIR"]),
        heartbeat_timeout_seconds=60,
    )
    created = await worker.create_run(
        runtime_instance="fixture-instance", task_owner="fixture-owner",
        contract={"start_url": "https://example.com/fixture", "interaction_mode": "public_interaction"},
    )
    run = worker._run
    assert run
    assert created["sdk_version"] == 1 and created["initial_observation"]["elements"]
    first_request = {
        "execution_id": f"{run.run_id}-1", "timeout_seconds": 10,
        "program": """fields = browser.find(role="textbox")
browser.fill_many([{"ref": item.ref, "value": "Alice" if item.name == "Name" else "30"} for item in fields])
return {"done": True}""",
    }
    try:
        first = await worker.execute_program(run, first_request)
        assert first["status"] == "partially_executed", first
        assert first["program_error"]["code"] == "BROWSER_REF_STALE", first
        assert len(first["action_ledger"]) == 1
        assert first["action_ledger"][0]["confirmed_state"] == {"value": "Alice"}
        assert await worker.execute_program(run, first_request) == first
        try:
            await worker.execute_program(run, {**first_request, "program": "return {}"})
        except SdkError as exc:
            assert exc.fault.code == "BROWSER_EXECUTION_CONFLICT"
        else:
            raise AssertionError("conflicting execution ID was accepted")
        corrected = await worker.execute_program(run, {
            "execution_id": f"{run.run_id}-2", "timeout_seconds": 10,
            "program": """age = browser.find(role="textbox", name="Age", exact=True).one()
browser.fill(age.ref, "30")
browser.assert_value(age.ref, "30")
return {"age": browser.get(age.ref).value}""",
        })
        assert corrected["status"] == "succeeded", corrected
        assert corrected["return_value"] == {"age": "30"}
        capture = await worker.dispatch({
            "version": PROTOCOL_VERSION, "token": "fixture-control", "operation": "capture",
            "payload": {"run_id": run.run_id, "run_capability": run.capability,
                        "runtime_instance": "fixture-instance", "task_owner": "fixture-owner",
                        "mode": "screenshot"},
        })
        assert capture["finalization_id"] and capture["content"]
        assert capture["observed_after_action_seq"] >= corrected["last_confirmed_action_seq"]
        output = await worker.execute_program(run, {
            "execution_id": f"{run.run_id}-3", "timeout_seconds": 3,
            "program": 'print("a" * 100000)\nreturn {"ok": True}',
        })
        assert output["status"] == "succeeded" and output["output_truncated"], output
        assert len(output["stdout"]) <= 10240
        continuation = await worker.execute_program(run, {
            "execution_id": f"{run.run_id}-4", "timeout_seconds": 10,
            "program": """age = browser.find(role="textbox", name="Age", exact=True).one()
browser.assert_value(age.ref, "30")
return {"age": browser.get(age.ref).value}""",
        })
        assert continuation["status"] == "succeeded", continuation
        assert continuation["return_value"] == {"age": "30"}
        edited = await worker.execute_program(run, {
            "execution_id": f"{run.run_id}-5", "timeout_seconds": 10,
            "program": """field = browser.find(role="textbox", name="Age", exact=True).one()
browser.press(field.ref, "ControlOrMeta+A")
receipt = browser.type(field.ref, "42")
browser.assert_value(field.ref, "42")
return receipt""",
        })
        assert edited["status"] == "succeeded", edited
        assert edited["return_value"]["confirmed_state"] == {"value": "42"}
    finally:
        await worker._cleanup(run)
    assert worker._run is None and not run.sdk_socket.exists()
    assert all(item.revoked for item in run.programs.values())
    fixture_page = SUBMISSION_PAGE
    created = await worker.create_run(
        runtime_instance="fixture-instance", task_owner="fixture-submit",
        contract={"start_url": "https://example.com/fixture", "interaction_mode": "read_only"},
    )
    run = worker._run
    assert run
    try:
        submitted = await worker.execute_program(run, {
            "execution_id": f"{run.run_id}-submit", "timeout_seconds": 10,
            "program": """field = browser.find(role="textbox", name="Name", exact=True).one()
browser.fill(field.ref, "Test")
button = browser.find(role="button", name="Send", exact=True).one()
browser.click(button.ref)
browser.wait_for(text="Saved successfully")
return {"evidence":browser.snapshot().visible_text}""",
        })
        assert submitted["status"] == "succeeded", submitted
        assert submitted["return_value"]["evidence"] == "Saved successfully"
        assert submitted["action_ledger"][-1]["method"] == "click"
        capture = await run.engine.capture("screenshot")
        assert "Saved successfully" in capture["final_observation"]["visible_text"] and capture["content"]
        ledger_size = len(run.engine.ledger)
        refused = await worker.execute_program(run, {
            "execution_id": f"{run.run_id}-repeat", "timeout_seconds": 10,
            "program": 'browser.submit("old-ref", [])',
        })
        assert refused["program_error"]["code"] == "BROWSER_REF_NOT_FOUND", refused
        assert not refused["action_ledger"]
        assert len(run.engine.ledger) == ledger_size
    finally:
        await worker._cleanup(run)
    fixture_page = DIALOG_PAGE
    await worker.create_run(
        runtime_instance="fixture-instance", task_owner="fixture-dialog",
        contract={"start_url": "https://example.com/fixture", "interaction_mode": "read_only"},
    )
    run = worker._run
    assert run
    try:
        opened = await worker.execute_program(run, {
            "execution_id": f"{run.run_id}-dialog", "timeout_seconds": 10,
            "program": """button = browser.find(role="button").one()
receipt = browser.click(button.ref)
return {"receipt": receipt, "dialog": browser.snapshot().dialog}""",
        })
        assert opened["status"] == "succeeded", opened
        assert opened["return_value"]["receipt"]["status"] == "pending"
        assert opened["final_observation"]["dialog"]["type"] == "prompt"
        prompted = await worker.execute_program(run, {
            "execution_id": f"{run.run_id}-prompt", "timeout_seconds": 10,
            "program": """dialog = browser.snapshot().dialog
receipt = browser.handle_dialog(dialog["dialog_id"], True, "Fixture draft")
return {"receipt": receipt, "dialog": browser.snapshot().dialog}""",
        })
        assert prompted["status"] == "succeeded", prompted
        assert prompted["return_value"]["dialog"]["type"] == "confirm"
        assert prompted["action_ledger"][0]["status"] == "pending", prompted
        resolved = await worker.execute_program(run, {
            "execution_id": f"{run.run_id}-confirm", "timeout_seconds": 10,
            "program": """dialog = browser.snapshot().dialog
browser.handle_dialog(dialog["dialog_id"], True)
return {"text": browser.snapshot().visible_text}""",
        })
        assert resolved["status"] == "succeeded", resolved
        assert resolved["action_ledger"][0]["status"] == "succeeded", resolved
        assert resolved["return_value"]["text"] == "EditFixture draft", resolved
        assert not resolved["final_observation"]["dialog"]
        assert len(run.engine.ledger) == 2
        capture = await run.engine.capture("screenshot")
        assert capture["observed_after_action_seq"] >= resolved["action_ledger"][0]["action_sequence"]
        assert capture["content"]
    finally:
        await worker._cleanup(run)
    created = await worker.create_run(
        runtime_instance="fixture-instance", task_owner="fixture-timeout",
        contract={"start_url": "https://example.com/fixture", "interaction_mode": "read_only"},
    )
    run = worker._run
    assert run
    try:
        timeout = await worker.execute_program(run, {
            "execution_id": f"{run.run_id}-timeout", "timeout_seconds": .2, "program": "while True: pass",
        })
        assert timeout["status"] == "timed_out", timeout
        assert timeout["exit_code"] is not None
        assert not timeout["outcome_unknown"]
        started = asyncio.Event()
        sdk_request = worker.sdk_request

        async def track_start(active_run, request):
            reply = await sdk_request(active_run, request)
            if request.get("method") == "url":
                started.set()
            return reply

        worker.sdk_request = track_start
        execution = asyncio.create_task(worker.execute_program(run, {
            "execution_id": f"{run.run_id}-cancel", "timeout_seconds": 10,
            "program": "browser.url()\nwhile True: pass",
        }))
        await asyncio.wait_for(started.wait(), timeout=5)
        cleanup_started = asyncio.get_running_loop().time()
        await worker._cleanup(run)
        cancelled = await execution
        assert cancelled["status"] == "cancelled", cancelled
        assert asyncio.get_running_loop().time() - cleanup_started < 5
    finally:
        await worker._cleanup(run)
    print(json.dumps({
        "status": "passed", "checks": [
            "separate_runner_sdk", "partial_batch", "ref_stale_recovery",
            "execution_idempotency", "execution_conflict", "final_capture_coverage",
            "bounded_output", "program_timeout_reaped", "capability_revocation",
            "cancel_waits_for_process_cleanup",
            "native_click_submission", "submission_capture_evidence", "stale_submission_ref_rejected",
            "interaction_mode_not_permission", "sdk_goal_state_wait",
            "native_selection_editing", "dialog_prompt_input", "dialog_confirmation",
            "dialog_action_ledger_update", "dialog_final_capture",
        ],
    }))


if __name__ == "__main__":
    asyncio.run(main())

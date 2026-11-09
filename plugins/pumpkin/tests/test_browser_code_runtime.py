import asyncio
import json
from pathlib import Path

import pytest

from browser.code_runner import BrowserCodeRunner, BoundedOutput
from browser.code_validator import BrowserCodeValidator
from browser.safe_sdk import BrowserSdkError, SafeBrowserSdk
from browser.sdk_protocol import SDK_VERSION, SdkError


@pytest.mark.parametrize("program", [
    "import os", "open('secret')", "eval('1')", "browser.__dict__",
    "getattr(browser, 'raw_page')", "globals()", "class Escaped: pass",
])
def test_program_rejects_unsafe_language_access(program):
    with pytest.raises(SdkError) as raised:
        BrowserCodeValidator().validate(program)
    assert raised.value.fault.code == "BROWSER_PROGRAM_INVALID"


def test_program_accepts_loops_conditions_and_return():
    BrowserCodeValidator().validate(
        'items = browser.find(role="textbox")\n'
        'for item in items:\n'
        '    if not item.disabled:\n'
        '        browser.fill(item.ref, "example")\n'
        'return {"count": len(items)}'
    )


@pytest.mark.parametrize("program", [
    "return [name for _, name in [(1, 'a'), (2, 'b')]]",
    "_values = [1, 2]\nreturn sum(_values)",
])
def test_program_accepts_private_local_variable_names(program):
    tree = BrowserCodeValidator().validate(program)
    scope = {}
    exec(compile(tree, "<fixture>", "exec"), scope)  # noqa: S102
    assert scope["main"](None) in (["a", "b"], 3)


@pytest.mark.parametrize("program", [
    "return __builtins__", "return browser._transport",
    "return json.__dict__", "return ().__class__",
])
def test_program_keeps_runtime_names_and_private_attributes_blocked(program):
    with pytest.raises(SdkError) as raised:
        BrowserCodeValidator().validate(program)
    assert raised.value.fault.field == "program.line[1]"


def test_sdk_sequences_reads_and_actions_and_surfaces_error():
    requests = []

    def transport(request):
        requests.append(request)
        if request["method"] == "fill":
            return {"ok": False, "error": {
                "code": "BROWSER_REF_STALE", "message": "node replaced",
                "field": "ref", "sdk_call_index": 2,
            }}
        return {"ok": True, "result": "https://example.com"}

    sdk = SafeBrowserSdk(transport)
    assert sdk.url() == "https://example.com"
    with pytest.raises(BrowserSdkError) as raised:
        sdk.fill("old-ref", "value")
    assert raised.value.code == "BROWSER_REF_STALE"
    assert raised.value.sdk_call_index == 2
    assert [item["sequence"] for item in requests] == [1, 2]
    assert all("sdk_capability" not in item for item in requests)


async def test_output_is_bounded_but_fully_drained():
    reader = asyncio.StreamReader()
    reader.feed_data(b"a" * 20_000)
    reader.feed_eof()
    output = BoundedOutput(100)
    await output.drain(reader)
    assert output.text() == "a" * 100
    assert output.truncated
    assert reader.at_eof()


async def test_runner_rejects_auth_source_and_socket_escape(tmp_path):
    runner = BrowserCodeRunner(token="fixture-token", sdk_socket_dir=tmp_path)
    with pytest.raises(SdkError):
        await runner.dispatch({"version": SDK_VERSION, "token": "wrong", "operation": "health"})
    request = {
        "version": SDK_VERSION, "token": "fixture-token", "operation": "execute",
        "execution_id": "fixture-execution", "program": "return {}",
        "sdk_socket": str(Path(tmp_path).parent / "outside.sock"), "timeout_seconds": 1,
    }
    with pytest.raises(SdkError) as raised:
        await runner.dispatch(request)
    assert raised.value.fault.code == "BROWSER_WORKER_INVALID_REQUEST"
    assert not runner.executions
    request.update(program="import socket", sdk_socket=str(tmp_path / "run.sock"))
    with pytest.raises(SdkError):
        await runner.dispatch(request)
    assert not runner.executions


async def test_runner_execution_id_binds_all_inputs(tmp_path, monkeypatch):
    runner = BrowserCodeRunner(token="fixture-token", sdk_socket_dir=tmp_path)
    calls = []

    async def execute(request):
        calls.append(request["execution_id"])
        return {"status": "succeeded", "exit_code": 0}

    monkeypatch.setattr(runner, "_execute", execute)
    request = {
        "version": SDK_VERSION, "token": "fixture-token", "operation": "execute",
        "execution_id": "fixture-execution", "program": "return {}",
        "sdk_socket": str(tmp_path / "run.sock"), "timeout_seconds": 1,
    }
    first = await runner.dispatch(request)
    assert await runner.dispatch(json.loads(json.dumps(request))) == first
    assert calls == ["fixture-execution"]
    with pytest.raises(SdkError) as raised:
        await runner.dispatch({**request, "program": "return {'different': True}"})
    assert raised.value.fault.code == "BROWSER_EXECUTION_CONFLICT"

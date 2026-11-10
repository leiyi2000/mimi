"""Local Chromium fixtures for native actions, async pages and final evidence."""
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from browser.controller import BrowserAgentRunner
from browser.engine import PlaywrightRun
from browser.gate import BrowserResultGate
from browser.models import (
    BrowserArtifactMode, BrowserExecutionState, BrowserExpectedOutput,
    BrowserInteractionMode, BrowserTaskContract, BrowserTaskStatus,
)
from browser.safe_sdk import SafeBrowserSdk
from browser.sdk_protocol import SdkError, SdkMethod


PAGE = """<!doctype html><title>Choices</title><body><form>
<div><span>First question</span><div>
<input type=radio name=first value=a style="display:none">
<a href="javascript:;">Same answer</a></div></div>
<div><span>Second question</span><div>
<input type=radio name=second value=a style="display:none">
<a href="javascript:void(0);">Same answer</a></div></div>
<label>Name<input name=person required></label>
</form><script>
document.querySelectorAll('a').forEach(a => a.onclick = () => {
a.previousElementSibling.checked = true;
});
</script></body>"""


@pytest.fixture
async def engine(tmp_path):
    if os.environ.get("PUMPKIN_TEST_PLAYWRIGHT") != "1":
        pytest.skip("enable local Chromium with PUMPKIN_TEST_PLAYWRIGHT=1")
    run = PlaywrightRun(tmp_path, action_timeout_ms=2000)
    await run.start()
    await run.page.set_content(PAGE)
    try:
        yield run
    finally:
        await run.close()


async def find(engine, **query):
    return (await engine.sdk_call(SdkMethod.FIND, query, 1))[0]


async def finish(engine, arguments, actions=()):
    class Worker:
        async def capture(self, *_args, **kwargs):
            return await engine.capture(kwargs["mode"])

    return await BrowserResultGate().finish(
        SimpleNamespace(run=object(), worker=Worker()), BrowserExecutionState(actions=list(actions)),
        BrowserTaskContract(
            "Perform page task", "https://example.com", BrowserExpectedOutput.SCREENSHOT,
            BrowserInteractionMode.PUBLIC_INTERACTION, BrowserArtifactMode.SCREENSHOT,
        ), {"status": "completed", "summary": "Done", **arguments},
    )


async def test_dom_scope_disambiguates_repeated_custom_choices(engine):
    snapshot = await engine.snapshot()
    heading = next(e for e in snapshot.elements if e.local_text == "First question")
    choices = await engine.sdk_call(
        SdkMethod.FIND, {"role": "link", "within": heading.parent_ref}, 1,
    )
    assert len(choices) == 1
    assert (await engine.sdk_call(SdkMethod.CLICK, {"ref": choices[0]["ref"]}, 2))["status"] == "succeeded"
    final = await engine.snapshot()
    assert next(e for e in final.elements if e.form_name == "first").checked
    assert not next(e for e in final.elements if e.form_name == "second").checked
    assert next(e for e in final.elements if e.local_text == "First question").ref == heading.ref


async def test_page_javascript_link_keeps_native_semantics(engine):
    await engine.page.locator("a").first.evaluate(
        "node => node.href = \"javascript:void(document.title='Changed')\"",
    )
    link = await find(engine, role="link")
    await engine.sdk_call(SdkMethod.CLICK, {"ref": link["ref"]}, 2)
    assert (await engine.snapshot()).title == "Changed"


async def test_optional_fields_need_no_invented_completion_checks(engine):
    field = await find(engine, role="textbox")
    await engine.sdk_call(SdkMethod.FILL, {"ref": field["ref"], "value": "Test"}, 2)
    arguments = {
        "evidence": ["Choices"],
        "value_checks": [{"ref": field["ref"], "expected": "Test"}],
    }
    assert (await finish(engine, arguments)).status is BrowserTaskStatus.COMPLETED
    await engine.page.locator("form").evaluate("form => form.reset()")
    assert (await finish(engine, arguments)).error_code == "BROWSER_FINAL_STATE_MISMATCH"
    arguments["value_checks"][0]["expected"] = True
    assert (await finish(engine, arguments)).error_code == "BROWSER_STATE_CHECK_INVALID"
    assert (await finish(engine, {"evidence": ["Invented acceptance"]})).error_code == "BROWSER_COMPLETION_UNVERIFIED"


async def test_compact_observation_and_sdk_wait_keep_attribute_contract(engine):
    snapshot = (await engine.snapshot()).payload()
    compact = BrowserAgentRunner._model_observation(snapshot)
    assert [{**compact["element_defaults"], **e} for e in compact["elements"]] == json.loads(
        json.dumps(snapshot["elements"]),
    )
    loop = asyncio.get_running_loop()

    def transport(request):
        result = asyncio.run_coroutine_threadsafe(
            engine.sdk_call(SdkMethod(request["method"]), request["arguments"], request["sequence"]), loop,
        ).result(timeout=10)
        return {"ok": True, "result": result}

    def program():
        browser = SafeBrowserSdk(transport)
        field = browser.find(role="textbox").one()
        browser.fill(field.ref, "SDK test")
        observed = browser.wait_for(ref=field.ref)
        assert observed.title == "Choices"
        assert browser.wait_for(text="First question").elements
        return browser.get(field.ref).value

    assert await asyncio.to_thread(program) == "SDK test"


@pytest.fixture
async def submission_page(engine):
    fixture = SimpleNamespace(form="", mode="", requests=[], url="")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            path = urlsplit(self.path).path
            fixture.requests.append((self.command, path, body))
            status, headers = 200, {}
            if path == "/validate":
                content = "valid"
            elif path == "/accept" and fixture.mode in {"redirect", "write_redirect"}:
                status = 303 if fixture.mode == "redirect" else 307
                headers["Location"] = "/receipt"
                content = ""
            elif path in {"/accept", "/receipt"}:
                content = "<title>Receipt</title><h1>Accepted</h1><p>Your answer was saved.</p>"
            else:
                content = fixture.form
            encoded = content.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(encoded)

        do_POST = do_GET
        do_PUT = do_GET
        do_PATCH = do_GET
        do_DELETE = do_GET

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture.url = f"http://127.0.0.1:{server.server_port}/form"

    async def prepare(mode):
        fixture.mode = mode
        fixture.form = """<!doctype html><title>Form</title>
        <label>Name<input name=person required form=survey></label>
        <form id=survey action=/accept method=post>
        <label>Optional note<input name=note></label>
        <label>Optional choice<input type=checkbox name=choice></label>
        <button type=submit>Send</button></form>
        <form id=unrelated><label>Other form<input name=other required></label></form>"""
        if mode == "search":
            fixture.form = fixture.form.replace("method=post", "method=get")
        if mode in {"custom", "local", "multistep"}:
            fixture.form = """<!doctype html><title>Form</title><section>
            <label>Name<input name=person required></label>
            <label>Optional note<input name=note></label><button type=button>Send</button></section>"""
        if mode in {"ajax", "custom"}:
            fixture.form += """<script>document.querySelector('button').onclick = async event => {
            event.preventDefault(); await fetch('/validate', {method:'POST',body:'query'});
            const response = await fetch('/accept', {method:'PUT',body:'answer'});
            setTimeout(async () => {document.body.innerHTML = await response.text()}, 150); };</script>"""
        if mode == "local":
            fixture.form += """<script>document.querySelector('button').onclick = () => {
            setTimeout(() => document.body.innerHTML = '<h1>Accepted</h1>', 150); };</script>"""
        if mode == "multistep":
            fixture.form += """<script>document.querySelector('button').onclick = async () => {
            await fetch('/validate', {method:'PATCH'});
            document.body.innerHTML = '<button onclick="fetch(\\'/accept\\', {method:\\'DELETE\\'}).then(r=>r.text()).then(t=>document.body.innerHTML=t)">Confirm</button>';
            };</script>"""
        await engine.sdk_call(SdkMethod.NAVIGATE, {"url": fixture.url}, 1)
        return fixture

    try:
        yield prepare
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join()


@pytest.mark.parametrize("mode", ["native", "redirect", "write_redirect", "search", "ajax", "custom", "local"])
@pytest.mark.parametrize("action", ["click", "submit"])
async def test_generic_actions_follow_page_requests_and_capture(engine, submission_page, mode, action):
    fixture = await submission_page(mode)
    field = await find(engine, form_name="person")
    button = await find(engine, role="button")
    await engine.sdk_call(SdkMethod.FILL, {"ref": field["ref"], "value": "Test"}, 2)
    receipt = await engine.sdk_call(SdkMethod(action), {"ref": button["ref"]}, 3)
    assert receipt["status"] == "succeeded"
    observed = await engine.sdk_call(SdkMethod.WAIT_FOR, {"text": "Accepted"}, 4)
    assert "Accepted" in observed["visible_text"]
    assert sum(path == "/accept" for _, path, _ in fixture.requests) == (0 if mode == "local" else 1)
    if mode == "write_redirect":
        assert ("POST", "/receipt", b"person=Test&note=") in fixture.requests
    if mode in {"ajax", "custom"}:
        assert [method for method, path, _ in fixture.requests if path in {"/validate", "/accept"}] == ["POST", "PUT"]
    result = await finish(engine, {"evidence": ["Accepted"]}, [receipt])
    assert result.status is BrowserTaskStatus.COMPLETED and result.artifacts
    if os.environ.get("PUMPKIN_TEST_OUTPUT_DIR"):
        (Path(os.environ["PUMPKIN_TEST_OUTPUT_DIR"]) / f"browser-actions-{mode}-{action}.png").write_bytes(
            result.artifacts[0].content,
        )
    stale = await finish(engine, {"evidence": ["Optional note"]}, [receipt])
    assert stale.status is BrowserTaskStatus.PARTIALLY_COMPLETED and not stale.artifacts


async def test_enter_submits_and_native_validation_belongs_to_page(engine, submission_page):
    fixture = await submission_page("native")
    button = await find(engine, role="button")
    assert (await engine.sdk_call(SdkMethod.CLICK, {"ref": button["ref"]}, 2))["status"] == "succeeded"
    assert not any(path == "/accept" for _, path, _ in fixture.requests)
    field = await find(engine, form_name="person")
    await engine.sdk_call(SdkMethod.FILL, {"ref": field["ref"], "value": "Test"}, 3)
    await engine.sdk_call(SdkMethod.PRESS, {"ref": field["ref"], "key": "Enter"}, 4)
    await engine.sdk_call(SdkMethod.WAIT_FOR, {"text": "Accepted"}, 5)
    assert sum(path == "/accept" for _, path, _ in fixture.requests) == 1


async def test_multistep_actions_and_all_http_methods(engine, submission_page):
    fixture = await submission_page("multistep")
    button = await find(engine, role="button")
    await engine.sdk_call(SdkMethod.CLICK, {"ref": button["ref"]}, 2)
    await engine.sdk_call(SdkMethod.WAIT_FOR, {"text": "Confirm"}, 3)
    button = await find(engine, role="button")
    await engine.sdk_call(SdkMethod.SUBMIT, {"ref": button["ref"]}, 4)
    await engine.sdk_call(SdkMethod.WAIT_FOR, {"text": "Accepted"}, 5)
    assert [method for method, path, _ in fixture.requests if path in {"/validate", "/accept"}] == ["PATCH", "DELETE"]
    assert not engine.outcome_unknown


async def test_wait_timeout_does_not_freeze_or_replay_prior_action(engine):
    await engine.page.set_content("""<button onclick="setTimeout(()=>document.body.innerHTML='<h1>Done</h1>',200)">
    Start</button>""")
    button = await find(engine, role="button")
    await engine.sdk_call(SdkMethod.CLICK, {"ref": button["ref"]}, 1)
    with pytest.raises(SdkError, match="wait condition"):
        await engine.sdk_call(SdkMethod.WAIT_FOR, {"text": "Done", "timeout_ms": 20}, 2)
    await engine.sdk_call(SdkMethod.WAIT_FOR, {"text": "Done", "timeout_ms": 1000}, 3)
    assert len(engine.ledger) == 1 and not engine.outcome_unknown


async def test_background_stream_does_not_block_dom_completion(engine, submission_page):
    await submission_page("local")
    await engine.page.route("**/stream", lambda route: asyncio.sleep(5))
    await engine.page.evaluate("() => {fetch('/stream');}")
    button = await find(engine, role="button")
    await engine.sdk_call(SdkMethod.CLICK, {"ref": button["ref"]}, 1)
    await engine.sdk_call(SdkMethod.WAIT_FOR, {"text": "Accepted", "timeout_ms": 1000}, 2)
    result = await finish(engine, {"evidence": ["Accepted"]})
    assert result.status is BrowserTaskStatus.COMPLETED


async def test_cross_origin_navigation_and_iframe_are_available(engine, submission_page):
    fixture = await submission_page("local")
    destination = fixture.url.replace("127.0.0.1", "localhost")
    await engine.page.locator("body").evaluate(
        "(body, url) => body.insertAdjacentHTML('beforeend', `<iframe src='${url}'></iframe>`)", destination,
    )
    await engine.page.frame_locator("iframe").get_by_role("button").wait_for()
    observed = await engine.snapshot()
    assert len([e for e in observed.elements if e.role == "button"]) == 2
    assert not observed.unsupported_frames
    await engine.sdk_call(SdkMethod.NAVIGATE, {"url": destination}, 1)
    await engine.sdk_call(SdkMethod.WAIT_FOR, {"url": destination}, 2)
    assert engine.page.url == destination


async def test_popup_becomes_current_page(engine, submission_page):
    fixture = await submission_page("local")
    await engine.page.locator("body").evaluate(
        "(body, url) => body.insertAdjacentHTML('beforeend', `<a href='${url}' target='_blank'>Open</a>`)",
        fixture.url.replace("/form", "/receipt"),
    )
    link = await find(engine, role="link")
    await engine.sdk_call(SdkMethod.CLICK, {"ref": link["ref"]}, 1)
    await engine.sdk_call(SdkMethod.WAIT_FOR, {"text": "Accepted"}, 2)
    assert engine.page.url.endswith("/receipt")


async def test_password_input_is_usable_but_value_is_redacted(engine):
    await engine.page.set_content("<label>Password<input type=password></label>")
    field = await find(engine, tag="input")
    receipt = await engine.sdk_call(SdkMethod.FILL, {"ref": field["ref"], "value": "fixture-value"}, 1)
    assert receipt["status"] == "succeeded"
    assert receipt["confirmed_state"] == {"value": None}
    assert (await engine.snapshot()).elements[-1].value is None
    assert await engine.page.locator("input").input_value() == "fixture-value"

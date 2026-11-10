import os

import pytest

from browser.engine import PlaywrightRun
from browser.protocol import PROTOCOL_VERSION, WorkerErrorCode
from browser.sdk_protocol import SDK_VERSION, SdkError, SdkMethod
from browser.worker import BrowserRun, BrowserWorker, ProgramExecution


PAGE = """<!doctype html><title>Fixture</title><body>
<fieldset><legend>Profile</legend>
<label for="name">Name</label><input id="name">
<label for="age">Age</label><input id="age">
<input type="checkbox" id="hidden" style="display:none">
<label for="hidden">Hidden choice</label>
<button type="button" role="checkbox" aria-label="ARIA choice" aria-checked="false"
onclick="this.setAttribute('aria-checked', this.getAttribute('aria-checked')==='true'?'false':'true')">ARIA choice</button>
<label for="choice">Choice</label><select id="choice"><option value="a">A</option><option value="b">B</option></select>
<button type="button" aria-label="Replace" onclick="document.querySelector('#name').outerHTML='<input id=name>'">Replace</button>
<button type="button" aria-label="Rename" onclick="document.querySelector('#age').setAttribute('aria-label','Years')">Rename</button>
<button type="submit">Submit</button><input type="password" aria-label="Password">
</fieldset><output>Ready</output></body>"""


@pytest.fixture
async def live_engine(tmp_path):
    if os.environ.get("PUMPKIN_TEST_PLAYWRIGHT") != "1":
        pytest.skip("enable local browser fixtures with PUMPKIN_TEST_PLAYWRIGHT=1")
    from playwright.async_api import async_playwright
    driver = await async_playwright().start()
    browser = await driver.chromium.launch(headless=True)
    engine = PlaywrightRun(temp_dir=tmp_path, action_timeout_ms=500)
    engine.browser = browser
    engine.context = await browser.new_context()
    engine.page = await engine.context.new_page()
    engine.page.set_default_timeout(500)
    await engine.page.set_content(PAGE)
    try:
        yield engine
    finally:
        await engine.close()
        await driver.stop()


async def find(engine, **query):
    return await engine.sdk_call(SdkMethod.FIND, query, 1)


async def test_values_checks_selection_scroll_and_observations_keep_refs(live_engine):
    engine = live_engine
    names = await find(engine, role="textbox", name="Name", exact=True)
    ref = names[0]["ref"]
    before = await engine.snapshot()
    await engine.sdk_call(SdkMethod.FILL, {"ref": ref, "value": "Alice"}, 2)
    after = await engine.snapshot()
    assert after.resource_version > before.resource_version
    assert after.observation_id != before.observation_id
    assert (await find(engine, role="textbox", name="Name", exact=True))[0]["ref"] == ref
    await engine.sdk_call(SdkMethod.FILL, {"ref": ref, "value": "Alice"}, 3)
    noop = await engine.snapshot()
    assert noop.resource_version == after.resource_version
    assert noop.observation_id != after.observation_id
    assert noop.observed_after_action_seq > after.observed_after_action_seq
    await engine.sdk_call(SdkMethod.SCROLL, {"direction": "down"}, 4)
    assert (await engine.refs.project(ref))["value"] == "Alice"
    check = (await find(engine, role="checkbox", name="Hidden choice", exact=True))[0]
    await engine.sdk_call(SdkMethod.CHECK, {"ref": check["ref"]}, 5)
    assert (await engine.refs.project(check["ref"]))["checked"]
    assert (await find(engine, role="checkbox", name="Hidden choice", exact=True))[0]["ref"] == check["ref"]
    aria = (await find(engine, role="checkbox", name="ARIA choice", exact=True))[0]
    await engine.sdk_call(SdkMethod.CHECK, {"ref": aria["ref"]}, 6)
    select = (await find(engine, role="combobox"))[0]
    await engine.sdk_call(SdkMethod.SELECT, {"ref": select["ref"], "values": "b"}, 7)
    assert (await engine.refs.project(select["ref"]))["selected"] == ["b"]
    assert not engine.outcome_unknown
    assert all(item.status == "succeeded" for item in engine.ledger)


async def test_replacement_and_semantic_change_invalidate_only_target(live_engine):
    engine = live_engine
    name = (await find(engine, name="Name", role="textbox", exact=True))[0]
    age = (await find(engine, name="Age", role="textbox", exact=True))[0]
    replacement = (await find(engine, name="Replace", role="button", exact=True))[0]
    await engine.sdk_call(SdkMethod.CLICK, {"ref": replacement["ref"]}, 2)
    with pytest.raises(SdkError) as raised:
        await engine.sdk_call(SdkMethod.FILL, {"ref": name["ref"], "value": "Alice"}, 3)
    assert raised.value.fault.code == WorkerErrorCode.REF_STALE
    assert (await engine.refs.project(age["ref"]))["name"] == "Age"
    assert (await find(engine, name="Name", role="textbox", exact=True))[0]["ref"] != name["ref"]
    rename = (await find(engine, name="Rename", role="button", exact=True))[0]
    await engine.sdk_call(SdkMethod.CLICK, {"ref": rename["ref"]}, 4)
    renamed = (await find(engine, name="Years", role="textbox", exact=True))[0]
    assert renamed["ref"] != age["ref"]
    with pytest.raises(SdkError):
        await engine.refs.project(age["ref"])


async def test_iframe_navigation_does_not_invalidate_main_frame(live_engine):
    engine = live_engine
    main = (await find(engine, role="textbox", name="Name", exact=True))[0]
    await engine.page.evaluate("""() => {
        const frame = document.createElement('iframe');
        frame.srcdoc = '<label for=x>Frame field</label><input id=x>';
        document.body.appendChild(frame);
    }""")
    await engine.page.frame_locator("iframe").get_by_label("Frame field").wait_for()
    frame_ref = (await find(engine, role="textbox", name="Frame field", exact=True))[0]
    await engine.page.locator("iframe").evaluate(
        "frame => frame.srcdoc = '<label for=x>Frame field</label><input id=x>'",
    )
    await engine.page.frame_locator("iframe").get_by_label("Frame field").wait_for()
    new_frame = (await find(engine, role="textbox", name="Frame field", exact=True))[0]
    assert new_frame["ref"] != frame_ref["ref"]
    assert new_frame["document_epoch"] > frame_ref["document_epoch"]
    assert (await engine.refs.project(main["ref"]))["name"] == "Name"
    with pytest.raises(SdkError):
        await engine.refs.project(frame_ref["ref"])


async def test_shadow_dom_and_main_document_replacement(live_engine):
    engine = live_engine
    await engine.page.evaluate("""() => {
        const host = document.createElement('div');
        document.body.appendChild(host);
        host.attachShadow({mode:'open'}).innerHTML =
            '<label for=x>Shadow field</label><input id=x>';
    }""")
    ref = (await find(engine, role="textbox", name="Shadow field", exact=True))[0]
    await engine.sdk_call(SdkMethod.FILL, {"ref": ref["ref"], "value": "shadow"}, 2)
    await engine.page.goto("about:blank")
    await engine.page.set_content(PAGE)
    current = await engine.snapshot()
    assert current.document_epoch > ref["document_epoch"]
    with pytest.raises(SdkError):
        await engine.refs.project(ref["ref"])


async def test_batch_preflight_checks_refs_but_business_actions_are_unrestricted(live_engine):
    engine = live_engine
    ref = (await find(engine, role="textbox", name="Name", exact=True))[0]["ref"]
    with pytest.raises(SdkError) as raised:
        await engine.sdk_call(SdkMethod.FILL_MANY, {"items": [
            {"ref": ref, "value": "Alice"}, {"ref": "missing", "value": "no"},
        ]}, 2)
    assert raised.value.fault.field == "items[1].ref"
    assert (await engine.refs.project(ref))["value"] == ""
    for query in [{"role": "button", "name": "Submit"}, {"name": "Password"}]:
        target = (await find(engine, **query))[0]["ref"]
        assert (await engine.sdk_call(SdkMethod.CLICK, {"ref": target}, 3))["status"] == "succeeded"
    assert (await engine.sdk_call(SdkMethod.PRESS, {"ref": ref, "key": "Enter"}, 4))["status"] == "succeeded"


async def test_unknown_outcome_freezes_mutations_but_allows_observation(live_engine, monkeypatch):
    engine = live_engine
    ref = (await find(engine, role="textbox", name="Name", exact=True))[0]["ref"]

    async def interrupted():
        raise TimeoutError

    monkeypatch.setattr(PlaywrightRun, "_settle_page", lambda self: interrupted())
    with pytest.raises(SdkError) as raised:
        await engine.sdk_call(SdkMethod.FILL, {"ref": ref, "value": "Alice"}, 2)
    assert raised.value.fault.code == WorkerErrorCode.OUTCOME_UNKNOWN
    assert engine.outcome_unknown
    assert (await engine.snapshot()).observed_after_action_seq == 1
    with pytest.raises(SdkError):
        await engine.sdk_call(SdkMethod.FILL, {"ref": ref, "value": "Bob"}, 3)
    assert len(engine.ledger) == 1


async def test_https_post_runs_and_finalization_covers_actions(live_engine):
    engine = live_engine
    requests = []

    async def fixture_route(route):
        requests.append(route.request.method)
        await route.fulfill(status=200, headers={"Access-Control-Allow-Origin": "*"}, body="saved")

    await engine.page.route("https://example.com/write", fixture_route)
    await engine.page.evaluate("""() => fetch('https://example.com/write',
        {method:'POST', body:'fixture'}).catch(() => null)""")
    assert "POST" in requests and engine.blocked_requests == 0
    capture = await engine.capture("screenshot")
    assert capture["final_observation"]["blocked_requests"] == 0
    assert capture["observation_id"] == capture["final_observation"]["observation_id"]
    assert capture["observed_after_action_seq"] >= capture["last_confirmed_action_seq"]


async def test_navigation_succeeds_with_background_post(live_engine):
    engine = live_engine
    url = "https://example.com/fixture"
    page = PAGE + """<script>
    fetch('https://example.com/telemetry', {method:'POST', body:'fixture'}).catch(() => null);
    </script>"""

    async def fixture_route(route):
        await route.fulfill(status=200, content_type="text/html", body=page)

    requests = []

    async def telemetry(route):
        requests.append(route.request.method)
        await route.fulfill(status=200, body="ok")

    await engine.page.route("https://example.com/telemetry", telemetry)
    await engine.page.route(url, fixture_route)
    receipt = await engine.sdk_call(SdkMethod.NAVIGATE, {"url": url}, 1)
    assert receipt["status"] == "succeeded"
    assert engine.page.url == url
    assert requests == ["POST"] and engine.blocked_requests == 0
    assert not engine.outcome_unknown
    snapshot = await engine.snapshot()
    assert snapshot.blocked_requests == 0
    assert snapshot.title == "Fixture"


async def test_fill_triggers_remote_validation_and_confirms_value(live_engine):
    engine = live_engine
    requests = []

    async def telemetry(route):
        requests.append(route.request.method)
        await route.fulfill(status=200, headers={"Access-Control-Allow-Origin": "*"}, body="ok")

    await engine.page.route("https://example.com/telemetry", telemetry)
    await engine.page.locator("#name").evaluate("""node => {
        node.addEventListener('input', () => fetch('https://example.com/telemetry',
            {method:'POST', body:'fixture'}).catch(() => null));
    }""")
    field = (await find(engine, role="textbox", name="Name", exact=True))[0]
    receipt = await engine.sdk_call(SdkMethod.FILL, {"ref": field["ref"], "value": "Alice"}, 2)
    assert receipt["status"] == "succeeded"
    assert receipt["confirmed_state"] == {"value": "Alice"}
    assert "POST" in requests and engine.blocked_requests == 0
    assert not engine.outcome_unknown
    capture = await engine.capture("none")
    assert capture["final_observation"]["blocked_requests"] == 0
    assert capture["observed_after_action_seq"] == receipt["action_sequence"]


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000", "http://169.254.169.254", "http://10.0.0.1",
    "https://other.example/path",
])
async def test_navigation_does_not_apply_host_permission_rules(tmp_path, url):
    await PlaywrightRun(temp_dir=tmp_path).validate_url(url)


async def test_sdk_sequence_and_capability_are_bound_to_execution(tmp_path):
    worker = BrowserWorker(token="fixture", sdk_socket_dir=tmp_path)
    engine = PlaywrightRun(temp_dir=tmp_path)
    run = BrowserRun("run", "capability", engine, tmp_path, "instance", "owner")
    worker._run = run
    execution = ProgramExecution("digest", "sdk-ticket")
    run.programs["execution"] = execution
    request = {
        "version": SDK_VERSION, "run_id": "run", "execution_id": "execution",
        "sdk_capability": "sdk-ticket", "sequence": 1, "method": "fill",
        "arguments": {"ref": "missing", "value": "fixture"},
    }
    result = await worker.sdk_request(run, request)
    assert result["error"]["code"] == WorkerErrorCode.REF_NOT_FOUND
    assert result["error"]["sdk_call_index"] == 1
    assert await worker.sdk_request(run, request) == result
    assert run.sdk_count == 1
    for change in [{"arguments": {"ref": "other", "value": "fixture"}}, {"sequence": 3}, {"sdk_capability": "wrong"}]:
        with pytest.raises(SdkError):
            await worker.sdk_request(run, {**request, **change})
    execution.revoked = True
    with pytest.raises(SdkError):
        await worker.sdk_request(run, request)
    assert PROTOCOL_VERSION == 3

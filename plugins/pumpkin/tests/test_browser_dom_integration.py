"""Local Chromium regressions for generic DOM interaction and final evidence."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from browser.engine import PlaywrightRun
from browser.controller import BrowserAgentRunner
from browser.gate import BrowserResultGate
from browser.models import (
    BrowserArtifactMode, BrowserExecutionState, BrowserExpectedOutput,
    BrowserInteractionMode, BrowserTaskContract, BrowserTaskStatus,
)
from browser.sdk_protocol import SdkError, SdkMethod
from browser.safe_sdk import SafeBrowserSdk
from providers.public_url import PublicUrlGuard


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
    from playwright.async_api import async_playwright
    driver = await async_playwright().start()
    browser = await driver.chromium.launch(headless=True)
    run = PlaywrightRun(tmp_path, interaction_allowed=True, action_timeout_ms=500)
    run.browser = browser
    run.context = await browser.new_context()
    run.page = await run.context.new_page()
    await run.page.set_content(PAGE)
    try:
        yield run
    finally:
        await run.close()
        await driver.stop()


async def test_dom_scopes_disambiguate_repeated_custom_choices(engine):
    snapshot = await engine.snapshot()
    heading = next(e for e in snapshot.elements if e.local_text == "First question")
    assert heading.tag == "span"
    choices = await engine.sdk_call(
        SdkMethod.FIND, {"role": "link", "within": heading.parent_ref}, 1,
    )
    assert len(choices) == 1
    receipt = await engine.sdk_call(SdkMethod.CLICK, {"ref": choices[0]["ref"]}, 2)
    assert receipt["status"] == "succeeded"
    final = await engine.snapshot()
    assert next(e for e in final.elements if e.form_name == "first").checked
    assert not next(e for e in final.elements if e.form_name == "second").checked
    assert next(e for e in final.elements if e.local_text == "First question").ref == heading.ref


@pytest.mark.parametrize("href", [
    "javascript:document.title='changed'", "data:text/html,changed",
    "https://other.example/write",
])
async def test_executable_links_and_external_navigation_still_rejected(engine, href):
    await engine.page.locator("a").first.evaluate("(node, href) => node.href = href", href)
    ref = (await engine.sdk_call(SdkMethod.FIND, {"role": "link"}, 1))[0]["ref"]
    with pytest.raises(SdkError):
        await engine.sdk_call(SdkMethod.CLICK, {"ref": ref}, 2)
    assert not engine.ledger
    assert await engine.page.title() == "Choices"


async def test_completion_rejects_empty_form_title_and_omitted_groups(engine):
    class Worker:
        async def capture(self, *_args, **_kwargs):
            return await engine.capture("none")

    session = SimpleNamespace(run=object(), worker=Worker())
    state = BrowserExecutionState()
    contract = BrowserTaskContract(
        "Fill all fields", "https://example.com",
        BrowserExpectedOutput.SUMMARY, BrowserInteractionMode.PUBLIC_INTERACTION,
        BrowserArtifactMode.NONE,
    )
    gate = BrowserResultGate(PublicUrlGuard())
    arguments = {"status": "completed", "summary": "filled", "evidence": ["Choices"]}
    result = await gate.finish(session, state, contract, arguments)
    assert result.status is BrowserTaskStatus.PARTIALLY_COMPLETED
    name = (await engine.sdk_call(SdkMethod.FIND, {"role": "textbox"}, 1))[0]
    await engine.sdk_call(SdkMethod.FILL, {"ref": name["ref"], "value": "Test"}, 2)
    arguments["state_checks"] = [{"ref": name["ref"], "kind": "value", "expected": "Test"}]
    result = await gate.finish(session, state, contract, arguments)
    assert result.status is BrowserTaskStatus.PARTIALLY_COMPLETED
    assert result.error_code == "BROWSER_FORM_STATE_UNVERIFIED"


async def test_complete_form_passes_then_reset_and_wrong_expected_state_fail(engine):
    class Worker:
        async def capture(self, *_args, **_kwargs):
            return await engine.capture("none")

    state = BrowserExecutionState()
    gate = BrowserResultGate(PublicUrlGuard())
    session = SimpleNamespace(run=object(), worker=Worker())
    contract = BrowserTaskContract(
        "Fill all fields", "https://example.com", BrowserExpectedOutput.SUMMARY,
        BrowserInteractionMode.PUBLIC_INTERACTION, BrowserArtifactMode.NONE,
    )
    for link in await engine.sdk_call(SdkMethod.FIND, {"role": "link"}, 1):
        await engine.sdk_call(SdkMethod.CLICK, {"ref": link["ref"]}, 2)
    field = (await engine.sdk_call(SdkMethod.FIND, {"role": "textbox"}, 3))[0]
    await engine.sdk_call(SdkMethod.FILL, {"ref": field["ref"], "value": "Test"}, 4)
    snapshot = await engine.snapshot()
    checks = [
        {"ref": e.ref, "kind": "checked", "expected": True}
        for e in snapshot.elements if e.role == "radio"
    ] + [{"ref": field["ref"], "kind": "value", "expected": "Test"}]
    arguments = {"status": "completed", "summary": "filled", "evidence": ["Choices"]}
    state.last_program = {"return_value": {"state_checks": checks}}
    result = await gate.finish(session, state, contract, arguments)
    assert result.status is BrowserTaskStatus.COMPLETED
    await engine.page.locator("form").evaluate("form => form.reset()")
    result = await gate.finish(session, state, contract, arguments)
    assert result.error_code == "BROWSER_FINAL_STATE_MISMATCH"
    arguments["state_checks"] = [{"ref": field["ref"], "kind": [], "expected": ""}]
    result = await gate.finish(session, state, contract, arguments)
    assert result.error_code == "BROWSER_STATE_CHECK_INVALID"


async def test_form_submission_is_blocked_for_native_paths(engine):
    await engine.context.add_init_script(f"({engine.SUBMIT_GUARD})()")
    await engine.page.evaluate(engine.SUBMIT_GUARD)
    await engine.page.locator("form").evaluate("""form => {
        form.action = 'https://example.com/write';
        form.noValidate = true;
        form.insertAdjacentHTML('beforeend', '<button type=submit>Send</button>');
    }""")
    requests = []
    await engine.context.route("**/*", lambda route: requests.append(route.request.url) or route.abort())
    for method in ("submit", "requestSubmit"):
        with pytest.raises(Exception, match="Form submission is unavailable"):
            await engine.page.locator("form").evaluate(f"form => form.{method}()")
    await engine.page.get_by_role("button", name="Send").click()
    await engine.page.locator("input[name=person]").press("Enter")
    assert not requests
    assert engine.page.url == "about:blank"


@pytest.mark.parametrize("invalid", [None, "false", "paraphrase", "missing_group"])
async def test_typed_finish_checks_and_exact_evidence(engine, invalid):
    await engine.page.locator("form").evaluate("""form => {
        form.insertAdjacentHTML('beforeend',
            '<input type=checkbox name=extra value=a checked><input type=checkbox name=extra value=b>');
    }""")
    for link in await engine.sdk_call(SdkMethod.FIND, {"role": "link"}, 1):
        await engine.sdk_call(SdkMethod.CLICK, {"ref": link["ref"]}, 2)
    field = (await engine.sdk_call(SdkMethod.FIND, {"role": "textbox"}, 3))[0]
    await engine.sdk_call(SdkMethod.FILL, {"ref": field["ref"], "value": "Test"}, 4)
    snapshot = await engine.snapshot()

    class Worker:
        async def capture(self, *_args, **_kwargs):
            return await engine.capture("none")

    arguments = {
        "status": "completed", "summary": "All four fields filled",
        "evidence": ["Choices"],
        "value_checks": [{"ref": field["ref"], "expected": "Test"}],
        "checked_checks": [
            {"ref": e.ref, "expected": e.checked}
            for e in snapshot.elements if e.role in {"radio", "checkbox"}
        ],
    }
    if invalid == "false":
        arguments["checked_checks"][-1]["expected"] = "false"
    elif invalid == "paraphrase":
        arguments["facts"] = ["All four fields filled"]
    elif invalid == "missing_group":
        arguments["checked_checks"] = arguments["checked_checks"][:2]
    contract = BrowserTaskContract(
        "Fill all fields", "https://example.com", BrowserExpectedOutput.SUMMARY,
        BrowserInteractionMode.PUBLIC_INTERACTION, BrowserArtifactMode.NONE,
    )
    result = await BrowserResultGate(PublicUrlGuard()).finish(
        SimpleNamespace(run=object(), worker=Worker()), BrowserExecutionState(), contract, arguments,
    )
    if invalid is None:
        assert result.status is BrowserTaskStatus.COMPLETED
    else:
        assert result.status is BrowserTaskStatus.PARTIALLY_COMPLETED
        assert result.error_code == {
            "false": "BROWSER_STATE_CHECK_INVALID",
            "paraphrase": "BROWSER_COMPLETION_UNVERIFIED",
            "missing_group": "BROWSER_FORM_STATE_UNVERIFIED",
        }[invalid]


async def test_compact_observation_preserves_nodes_and_sdk_attribute_contract(engine):
    snapshot = (await engine.snapshot()).payload()
    compact = BrowserAgentRunner._model_observation(snapshot)
    reconstructed = [
        {**compact["element_defaults"], **element} for element in compact["elements"]
    ]
    assert reconstructed == json.loads(json.dumps(snapshot["elements"]))
    assert len(json.dumps(compact)) < len(json.dumps(snapshot))
    loop = asyncio.get_running_loop()

    def transport(request):
        result = asyncio.run_coroutine_threadsafe(
            engine.sdk_call(
                SdkMethod(request["method"]), request["arguments"], request["sequence"],
            ), loop,
        ).result(timeout=10)
        return {"ok": True, "result": result}

    def program():
        browser = SafeBrowserSdk(transport)
        observed = browser.snapshot()
        assert observed.title == "Choices"
        field = browser.find(role="textbox").one()
        assert isinstance(field.selected, tuple) and isinstance(field.options, tuple)
        browser.fill(field.ref, "SDK test")
        browser.assert_value(field.ref, "SDK test")
        return browser.get(field.ref).value

    assert await asyncio.to_thread(program) == "SDK test"

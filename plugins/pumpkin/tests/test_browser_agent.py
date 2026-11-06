from __future__ import annotations

import asyncio
import base64
import json
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from browser.worker import BrowserWorker, WorkerRequestError
from core.domain import ModelMessage, ModelResponse, ToolCall
from providers.browser_worker import BrowserWorkerClient, BrowserWorkerRun
from providers.public_url import PublicUrlError, PublicUrlGuard
from runtime.browser_agent import (
    BrowserAgentRunner,
    BrowserArtifactMode,
    BrowserExpectedOutput,
    BrowserInteractionMode,
    BrowserTaskStatus,
    _compact_detected_forms,
)


async def public_resolver(host: str, port: int):
    del host, port
    return ("93.184.216.34",)


class FakeWorker:
    def __init__(self) -> None:
        self.cancelled = False
        self.calls: list[tuple[str, dict]] = []
        self.tool_text: dict[str, str] = {}

    async def create_run(self) -> BrowserWorkerRun:
        return BrowserWorkerRun("run", "capability")

    async def cancel_run(self, run: BrowserWorkerRun) -> None:
        assert run.run_id == "run"
        self.cancelled = True

    async def mcp_request(self, run, message):
        del run
        method = message["method"]
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        if method == "notifications/initialized":
            return {"notification": True}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {"name": "browser_snapshot"},
                        {"name": "browser_detect_forms"},
                        {"name": "browser_fill_form"},
                        {"name": "browser_screenshot"},
                        {"name": "browser_evaluate"},
                    ]
                },
            }
        name = message["params"]["name"]
        arguments = message["params"]["arguments"]
        self.calls.append((name, arguments))
        if name == "browser_screenshot":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "content": [
                        {
                            "type": "image",
                            "mimeType": "image/png",
                            "data": base64.b64encode(
                                b"\x89PNG\r\n\x1a\nimage"
                            ).decode(),
                        }
                    ]
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": self.tool_text.get(name, "opened"),
                    }
                ]
            },
        }


class FakeProvider:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.tools: list[set[str]] = []
        self.messages = []

    async def stream(
        self,
        messages,
        tools,
        *,
        mode,
    ) -> AsyncIterator[ModelResponse]:
        assert mode == "browser"
        self.messages.append(messages)
        self.tools.append({tool.name for tool in tools})
        yield self.responses.pop(0)


def response(*calls: ToolCall) -> ModelResponse:
    return ModelResponse(
        message=ModelMessage(
            role="assistant",
            content="",
            tool_calls=tuple(calls),
        ),
        request_id="request",
        provider="fake",
        model="fake",
    )


@pytest.mark.asyncio
async def test_browser_worker_client_reads_large_protocol_frame():
    payload = "x" * (asyncio.streams._DEFAULT_LIMIT + 1)

    async def handle(reader, writer):
        request = json.loads(await reader.readline())
        writer.write(
            json.dumps(
                {
                    "version": 1,
                    "request_id": request["request_id"],
                    "ok": True,
                    "result": {"payload": payload},
                },
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    with tempfile.TemporaryDirectory(
        prefix="pumpkin-browser-",
        dir="/tmp",
    ) as directory:
        socket_path = Path(directory) / "worker.sock"
        server = await asyncio.start_unix_server(handle, path=socket_path)
        try:
            result = await BrowserWorkerClient(
                socket_path=socket_path,
                token="test-token",
            ).health()
        finally:
            server.close()
            await server.wait_closed()

    assert result == {"payload": payload}


@pytest.mark.asyncio
async def test_public_url_guard_rejects_mixed_private_dns():
    async def resolver(host: str, port: int):
        del host, port
        return ("93.184.216.34", "127.0.0.1")

    with pytest.raises(
        PublicUrlError,
        match="PUBLIC_URL_PRIVATE_ADDRESS",
    ):
        await PublicUrlGuard(resolver).validate("https://example.com")


@pytest.mark.asyncio
async def test_browser_runner_filters_tools_writes_media_and_cleans_run():
    worker = FakeWorker()
    worker.tool_text["browser_snapshot"] = "page loaded"
    provider = FakeProvider(
        [
            response(ToolCall("snapshot", "browser_snapshot", {})),
            response(ToolCall("shot", "browser_screenshot", {})),
            response(
                ToolCall(
                    "finish",
                    "finish_browser_task",
                    {
                        "status": "completed",
                        "summary": "captured",
                        "facts": ["page loaded"],
                        "links": ["https://example.com/result"],
                        "evidence": ["page loaded"],
                    },
                )
            ),
        ]
    )
    runner = BrowserAgentRunner(
        provider=provider,
        worker=worker,
        url_guard=PublicUrlGuard(public_resolver),
    )

    result = await runner.run(
        goal="capture the page",
        start_url="https://example.com",
        expected_output=BrowserExpectedOutput.SCREENSHOT,
        interaction_mode=BrowserInteractionMode.READ_ONLY,
        artifact_mode=BrowserArtifactMode.SCREENSHOT,
    )

    assert result.status is BrowserTaskStatus.COMPLETED
    assert result.artifacts[0].content.startswith(b"\x89PNG")
    assert result.links == ("https://example.com/result",)
    assert worker.cancelled is True
    assert "browser_evaluate" not in provider.tools[0]
    assert "browser_screenshot" in provider.tools[0]
    assert isinstance(provider.messages[0][1].content, str)
    assert worker.calls == [
        ("browser_navigate", {"url": "https://example.com"}),
        ("browser_snapshot", {}),
        ("browser_screenshot", {}),
    ]


@pytest.mark.asyncio
async def test_browser_runner_batches_form_fields_and_verifies_final_state():
    worker = FakeWorker()
    worker.tool_text["browser_detect_forms"] = "Q1 age; Q2 comment"
    worker.tool_text["browser_snapshot"] = "问卷提交成功，感谢参与"
    provider = FakeProvider(
        [
            response(ToolCall("forms", "browser_detect_forms", {})),
            response(
                ToolCall(
                    "fill",
                    "browser_fill_form",
                    {
                        "fields": [
                            {
                                "field_name": "q1",
                                "value": "3",
                                "type": "check",
                            },
                            {"ref": "e2", "value": "很好", "type": "text"},
                        ],
                        "submit_ref": "e3",
                    },
                )
            ),
            response(ToolCall("snapshot", "browser_snapshot", {})),
            response(ToolCall("shot", "browser_screenshot", {})),
            response(
                ToolCall(
                    "finish",
                    "finish_browser_task",
                    {
                        "status": "completed",
                        "summary": "submitted",
                        "facts": ["问卷提交成功"],
                        "evidence": ["问卷提交成功"],
                    },
                )
            ),
        ]
    )
    runner = BrowserAgentRunner(
        provider=provider,
        worker=worker,
        url_guard=PublicUrlGuard(public_resolver),
    )

    result = await runner.run(
        goal="complete the public questionnaire",
        start_url="https://example.com/form",
        expected_output=BrowserExpectedOutput.SCREENSHOT,
        interaction_mode=BrowserInteractionMode.PUBLIC_INTERACTION,
        artifact_mode=BrowserArtifactMode.SCREENSHOT,
    )

    assert result.status is BrowserTaskStatus.COMPLETED
    assert result.error_code is None
    assert (
        "browser_fill_form",
        {
            "fields": [
                {"field_name": "q1", "value": "3", "type": "check"},
                {"ref": "e2", "value": "很好", "type": "text"},
            ],
            "submit_ref": "e3",
        },
    ) in worker.calls
    assert "browser_fill_form" in provider.tools[0]


@pytest.mark.asyncio
async def test_browser_runner_preserves_form_evidence_across_scroll_snapshot():
    worker = FakeWorker()
    worker.tool_text["browser_detect_forms"] = '{"unfilled_field_names":[]}'
    worker.tool_text["browser_snapshot"] = "提交"
    provider = FakeProvider(
        [
            response(ToolCall("forms", "browser_detect_forms", {})),
            response(
                ToolCall(
                    "scroll",
                    "browser_scroll",
                    {"direction": "bottom", "amount": 2000},
                )
            ),
            response(ToolCall("snapshot", "browser_snapshot", {})),
            response(ToolCall("shot", "browser_screenshot", {})),
            response(
                ToolCall(
                    "finish",
                    "finish_browser_task",
                    {
                        "status": "completed",
                        "summary": "filled but not submitted",
                        "evidence": [
                            'browser_detect_forms: {"unfilled_field_names":[]}',
                            "browser_screenshot: image/png 100 bytes",
                        ],
                    },
                )
            ),
        ]
    )
    runner = BrowserAgentRunner(
        provider=provider,
        worker=worker,
        url_guard=PublicUrlGuard(public_resolver),
    )

    result = await runner.run(
        goal="fill but do not submit",
        start_url="https://example.com/form",
        expected_output=BrowserExpectedOutput.SCREENSHOT,
        interaction_mode=BrowserInteractionMode.PUBLIC_INTERACTION,
        artifact_mode=BrowserArtifactMode.SCREENSHOT,
    )

    assert result.status is BrowserTaskStatus.COMPLETED
    assert result.error_code is None


@pytest.mark.asyncio
async def test_browser_runner_rejects_unobserved_completion_claim():
    worker = FakeWorker()
    worker.tool_text["browser_snapshot"] = "问卷尚未提交"
    provider = FakeProvider(
        [
            response(ToolCall("snapshot", "browser_snapshot", {})),
            response(
                ToolCall(
                    "finish",
                    "finish_browser_task",
                    {
                        "status": "completed",
                        "summary": "submitted",
                        "evidence": ["提交成功"],
                    },
                )
            ),
        ]
    )
    runner = BrowserAgentRunner(
        provider=provider,
        worker=worker,
        url_guard=PublicUrlGuard(public_resolver),
    )

    result = await runner.run(
        goal="submit a public form",
        start_url="https://example.com/form",
        expected_output=BrowserExpectedOutput.SUMMARY,
        interaction_mode=BrowserInteractionMode.PUBLIC_INTERACTION,
        artifact_mode=BrowserArtifactMode.NONE,
    )

    assert result.status is BrowserTaskStatus.PARTIALLY_COMPLETED
    assert result.error_code == "BROWSER_COMPLETION_UNVERIFIED"


@pytest.mark.asyncio
async def test_worker_accepts_safe_form_locators_and_rejects_selectors():
    arguments = await BrowserWorker._validate_arguments(
        "browser_fill_form",
        {
            "fields": [
                {"ref": "field-1", "value": "answer", "type": "text"},
                {"field_name": "q1", "value": "3", "type": "check"},
                {
                    "field_name": "q12",
                    "value": "test answer",
                    "type": "text",
                },
            ],
            "submit_ref": "submit-1",
        },
    )
    assert arguments["submit_ref"] == "submit-1"
    assert arguments["fields"] == [
        {"ref": "field-1", "value": "answer", "type": "text"},
        {
            "selector": '[name="q1"][value="3"]',
            "value": "",
            "type": "check",
        },
        {
            "selector": '[name="q12"]',
            "value": "test answer",
            "type": "text",
        },
    ]

    with pytest.raises(WorkerRequestError, match="forbidden"):
        await BrowserWorker._validate_arguments(
            "browser_fill_form",
            {"fields": [{"selector": "#answer", "value": "answer", "type": "text"}]},
        )


def test_detected_forms_are_compacted_without_losing_late_fields():
    payload = json.dumps(
        [
            {
                "index": 0,
                "id": "form1",
                "action": "https://example.com/submit",
                "method": "post",
                "fields": [
                    {
                        "tag": "input",
                        "type": "radio",
                        "name": f"q{index}",
                        "value": str(option),
                        "checked": index == 22 and option == 3,
                        "required": False,
                        "label": "",
                        "ref": None,
                        "options": None,
                    }
                    for index in range(1, 23)
                    for option in range(1, 6)
                ],
            }
        ]
    )

    compacted = _compact_detected_forms(payload)
    forms = json.loads(compacted)

    assert len(compacted) < len(payload)
    assert forms[0]["fields"][-1]["name"] == "q22"
    assert "ref" not in forms[0]["fields"][-1]
    assert "action" not in forms[0]
    assert forms[0]["state"]["field_group_count"] == 22
    assert forms[0]["state"]["filled_field_names"] == ["q22"]
    assert "q22" not in forms[0]["state"]["unfilled_field_names"]


@pytest.mark.asyncio
async def test_browser_wait_for_text_uses_bounded_seconds():
    runner = BrowserAgentRunner(
        provider=FakeProvider([]),
        worker=FakeWorker(),
        url_guard=PublicUrlGuard(public_resolver),
    )

    default = await runner._sanitize(
        "browser_wait_for_text",
        {"text": "done"},
        BrowserInteractionMode.READ_ONLY,
    )
    bounded = await runner._sanitize(
        "browser_wait_for_text",
        {"text": "done", "timeout": 5000},
        BrowserInteractionMode.READ_ONLY,
    )

    assert default == {"text": "done", "timeout": 10}
    assert bounded == {"text": "done", "timeout": 60}


@pytest.mark.asyncio
async def test_worker_rejects_selector_and_private_navigation():
    with pytest.raises(WorkerRequestError, match="forbidden fields"):
        await BrowserWorker._validate_arguments(
            "browser_click",
            {"ref": "button-1", "selector": "#submit"},
        )

    with pytest.raises(WorkerRequestError, match="not public"):
        await BrowserWorker._validate_arguments(
            "browser_navigate",
            {"url": "http://127.0.0.1/private"},
        )

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import pytest

from browser.worker import BrowserWorker, WorkerRequestError
from core.domain import ModelMessage, ModelResponse, ToolCall
from providers.browser_worker import BrowserWorkerRun
from providers.public_url import PublicUrlError, PublicUrlGuard
from runtime.browser_agent import (
    BrowserAgentRunner,
    BrowserArtifactMode,
    BrowserExpectedOutput,
    BrowserInteractionMode,
    BrowserTaskStatus,
)


async def public_resolver(host: str, port: int):
    del host, port
    return ("93.184.216.34",)


class FakeWorker:
    def __init__(self) -> None:
        self.cancelled = False
        self.calls: list[tuple[str, dict]] = []

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
            "result": {"content": [{"type": "text", "text": "opened"}]},
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
    provider = FakeProvider(
        [
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
        ("browser_screenshot", {}),
    ]


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

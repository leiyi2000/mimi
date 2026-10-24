from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from core.agent_loop import (
    _TurnToolBudget,
    _budget_notice,
    _tool_content,
)
from core.config import PumpkinConfig
from core.domain import ToolCall, ToolResult
from core.tools import Tool, ToolBudgetKind, ToolBudgetLimits
from providers.web import WebProviderError, WebSearchProvider


def _tool(
    *,
    kind: ToolBudgetKind,
    deduplicate_argument: str | None = None,
) -> Tool:
    return Tool(
        name=f"tool_{kind.value}",
        description="test",
        input_schema={"type": "object"},
        handler=lambda context, arguments: ToolResult(content={}),
        budget_kind=kind,
        deduplicate_argument=deduplicate_argument,
    )


def _budget(
    *,
    search: int = 1,
    fetch: int = 2,
    image: int = 1,
) -> _TurnToolBudget:
    return _TurnToolBudget(
        ToolBudgetLimits(
            web_search=search,
            web_fetch=fetch,
            web_image=image,
        )
    )


def _provider(
    client: httpx.AsyncClient,
    *,
    trace=None,
    max_host_concurrency: int = 2,
) -> WebSearchProvider:
    return WebSearchProvider(
        api_url="https://search.example/api",
        api_key="test-key",
        timeout=1,
        max_results=3,
        max_fetch_bytes=100_000,
        max_image_bytes=100_000,
        max_fetch_chars=30_000,
        max_host_concurrency=max_host_concurrency,
        client=client,
        trace=trace,
    )


async def _allow_public_url(url: str) -> None:
    assert url.startswith("https://public.example/")


def test_task_budget_defaults_and_notices(monkeypatch):
    for name in (
        "PUMPKIN_TASK_MAX_STEPS",
        "PUMPKIN_TASK_FINALIZATION_STEPS",
        "PUMPKIN_TASK_WEB_SEARCH_LIMIT",
        "PUMPKIN_TASK_WEB_FETCH_LIMIT",
        "PUMPKIN_TASK_WEB_IMAGE_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)

    config = PumpkinConfig.from_env()

    assert config.task.budget.max_steps == 16
    assert config.task.budget.finalization_steps == 2
    assert config.task.budget.web_search_limit == 4
    assert config.task.budget.web_fetch_limit == 8
    assert config.task.budget.web_image_limit == 4
    assert "50%" in _budget_notice(8, 16, (0.5, 0.75))
    assert "75%" in _budget_notice(12, 16, (0.5, 0.75))


def test_turn_budget_reuses_normalized_url_after_non_retryable_failure():
    budget = _budget()
    tool = _tool(
        kind=ToolBudgetKind.WEB_FETCH,
        deduplicate_argument="url",
    )
    first_call = ToolCall(
        "first",
        tool.name,
        {"url": "HTTPS://Public.Example:443/page?b=2&a=1#section"},
    )
    duplicate_call = ToolCall(
        "duplicate",
        tool.name,
        {"url": "https://public.example/page?a=1&b=2"},
    )
    failure = ToolResult(
        content={"message": "not found"},
        is_error=True,
        error_code="FETCH_NOT_FOUND",
    )

    assert budget.prepare(tool, first_call) is None
    budget.finish(tool, first_call, failure)

    assert budget.prepare(tool, duplicate_call) is failure
    assert budget.counts[ToolBudgetKind.WEB_FETCH] == 1


def test_turn_budget_allows_retryable_url_and_rejects_exhausted_category():
    budget = _budget(fetch=2)
    tool = _tool(
        kind=ToolBudgetKind.WEB_FETCH,
        deduplicate_argument="url",
    )
    call = ToolCall("first", tool.name, {"url": "https://public.example/page"})
    retry = ToolResult(
        content={"message": "limited"},
        is_error=True,
        error_code="FETCH_RATE_LIMITED",
        retryable=True,
    )

    assert budget.prepare(tool, call) is None
    budget.finish(tool, call, retry)
    assert budget.prepare(tool, call) is None
    budget.finish(tool, call, retry)

    exhausted = budget.prepare(tool, call)
    assert exhausted is not None
    assert exhausted.error_code == "TOOL_BUDGET_EXHAUSTED"


def test_turn_budget_blocks_parallel_duplicate_url():
    budget = _budget()
    tool = _tool(
        kind=ToolBudgetKind.WEB_FETCH,
        deduplicate_argument="url",
    )
    first = ToolCall("first", tool.name, {"url": "https://public.example/page"})
    duplicate = ToolCall(
        "duplicate",
        tool.name,
        {"url": "https://PUBLIC.example/page#fragment"},
    )

    assert budget.prepare(tool, first) is None
    blocked = budget.prepare(tool, duplicate)

    assert blocked is not None
    assert blocked.error_code == "DUPLICATE_RESOURCE_IN_FLIGHT"
    assert budget.counts[ToolBudgetKind.WEB_FETCH] == 1


def test_web_model_content_is_bounded_without_mutating_full_result():
    full_text = "x" * 20_000
    result = ToolResult(
        content={
            "url": "https://public.example/page",
            "text": full_text,
            "links": [f"https://public.example/{index}" for index in range(100)],
        }
    )

    model_content = _tool_content(result, max_chars=8_000)
    payload = json.loads(model_content)

    assert len(model_content) <= 8_000
    assert payload["content"]["model_content_truncated"] is True
    assert len(payload["content"]["text"]) < len(full_text)
    assert result.content["text"] == full_text
    assert "model_content_truncated" not in result.content


@pytest.mark.parametrize(
    ("status_code", "error_code", "retryable"),
    (
        (400, "FETCH_HTTP_ERROR", False),
        (404, "FETCH_NOT_FOUND", False),
        (408, "FETCH_TIMEOUT", True),
        (429, "FETCH_RATE_LIMITED", True),
        (503, "FETCH_UPSTREAM_ERROR", True),
    ),
)
@pytest.mark.asyncio
async def test_fetch_http_status_classification(
    monkeypatch,
    status_code,
    error_code,
    retryable,
):
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"retry-after": "3"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = _provider(client)
        monkeypatch.setattr(provider, "_validate_public_url", _allow_public_url)

        with pytest.raises(WebProviderError) as captured:
            await provider.fetch("https://public.example/private-path?token=secret")

    assert captured.value.code == error_code
    assert captured.value.status_code == status_code
    assert captured.value.retryable is retryable
    assert captured.value.retry_after == "3"


@pytest.mark.asyncio
async def test_fetch_limits_concurrency_per_host(monkeypatch):
    active = 0
    max_active = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"ok",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = _provider(client, max_host_concurrency=2)
        monkeypatch.setattr(provider, "_validate_public_url", _allow_public_url)

        await asyncio.gather(
            *(
                provider.fetch(f"https://public.example/page-{index}")
                for index in range(6)
            )
        )

    assert max_active == 2


@pytest.mark.asyncio
async def test_http_trace_excludes_url_path_query_and_body(monkeypatch):
    traces: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "5"},
            content=b"sensitive-body",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = _provider(client, trace=traces.append)
        monkeypatch.setattr(provider, "_validate_public_url", _allow_public_url)

        with pytest.raises(WebProviderError):
            await provider.fetch(
                "https://public.example/private-path?token=secret-value"
            )

    assert len(traces) == 1
    assert traces[0].startswith("pumpkin_web_http ")
    assert "public.example" in traces[0]
    assert "private-path" not in traces[0]
    assert "secret-value" not in traces[0]
    assert "sensitive-body" not in traces[0]
    event = json.loads(traces[0].removeprefix("pumpkin_web_http "))
    assert event["status_code"] == 429
    assert event["retryable"] is True
    assert event["retry_after"] == "5"

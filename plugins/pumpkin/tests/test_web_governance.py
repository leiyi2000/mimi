from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import wreq

from core.agent_loop import (
    SessionAgentLoop,
    _TurnToolBudget,
    _budget_notice,
    _tool_content,
)
from core.config import PumpkinConfig
from core.domain import ToolCall, ToolResult
from core.tools import (
    Tool,
    ToolBudgetKind,
    ToolBudgetLimits,
    ToolExecutionPolicy,
    ToolRegistry,
)
from providers.web import (
    WebProviderError,
    WebSearchProvider,
    WebTransportError,
    WebTransportResponse,
    WreqWebTransport,
)


class FakeWebTransport:
    def __init__(
        self,
        respond: Callable[[str, Mapping[str, str], int], WebTransportResponse],
    ) -> None:
        self._respond = respond
        self.calls: list[tuple[str, Mapping[str, str], int]] = []

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        max_bytes: int,
    ) -> WebTransportResponse:
        self.calls.append((url, headers, max_bytes))
        return self._respond(url, headers, max_bytes)


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
    download: int = 1,
) -> _TurnToolBudget:
    return _TurnToolBudget(
        ToolBudgetLimits(
            web_search=search,
            web_fetch=fetch,
            web_download=download,
        )
    )


def _provider(
    transport: FakeWebTransport,
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
        max_download_bytes=100_000,
        max_fetch_chars=30_000,
        max_host_concurrency=max_host_concurrency,
        web_transport=transport,
        trace=trace,
    )


def test_task_budget_defaults_and_notices(monkeypatch):
    for name in (
        "PUMPKIN_TASK_MAX_STEPS",
        "PUMPKIN_TASK_FINALIZATION_STEPS",
        "PUMPKIN_TASK_WEB_SEARCH_LIMIT",
        "PUMPKIN_TASK_WEB_FETCH_LIMIT",
        "PUMPKIN_TASK_WEB_DOWNLOAD_LIMIT",
        "PUMPKIN_TASK_WEB_IMAGE_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)

    config = PumpkinConfig.from_env()

    assert config.task.budget.max_steps == 16
    assert config.task.budget.finalization_steps == 2
    assert config.task.budget.web_search_limit == 4
    assert config.task.budget.web_fetch_limit == 8
    assert config.task.budget.web_download_limit == 4
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


def test_turn_budget_uses_argument_specific_category():
    tool = Tool(
        name="fetch_url",
        description="test",
        input_schema={"type": "object"},
        handler=lambda context, arguments: ToolResult(content={}),
        deduplicate_argument="url",
        policy_resolver=lambda arguments: ToolExecutionPolicy(
            read_only=arguments.get("output") != "artifact",
            parallel_safe=True,
            idempotency_required=arguments.get("output") == "artifact",
            budget_kind=(
                ToolBudgetKind.WEB_DOWNLOAD
                if arguments.get("output") == "artifact"
                else ToolBudgetKind.WEB_FETCH
            ),
        ),
    )
    budget = _budget(fetch=1, download=1)
    text = ToolCall(
        "text",
        tool.name,
        {"url": "https://public.example/page"},
    )
    download = ToolCall(
        "download",
        tool.name,
        {
            "url": "https://public.example/file.zip",
            "output": "artifact",
        },
    )

    assert budget.prepare(tool, text) is None
    assert budget.prepare(tool, download) is None
    assert budget.counts == {
        ToolBudgetKind.WEB_FETCH: 1,
        ToolBudgetKind.WEB_DOWNLOAD: 1,
    }


def test_agent_loop_can_batch_parallel_downloads():
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="fetch_url",
            description="test",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(content={}),
            policy_resolver=lambda arguments: ToolExecutionPolicy(
                read_only=arguments.get("output") != "artifact",
                parallel_safe=True,
                idempotency_required=arguments.get("output") == "artifact",
                budget_kind=ToolBudgetKind.WEB_DOWNLOAD,
            ),
        )
    )
    loop = SimpleNamespace(tools=registry)
    calls = (
        ToolCall(
            "first",
            "fetch_url",
            {"url": "https://a.example/file.zip", "output": "artifact"},
        ),
        ToolCall(
            "second",
            "fetch_url",
            {"url": "https://b.example/file.pdf", "output": "artifact"},
        ),
    )

    assert SessionAgentLoop._can_run_in_parallel(loop, calls)


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


@pytest.mark.asyncio
async def test_search_uses_httpx_without_calling_web_transport():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "results": [
                        {
                            "title": "Result",
                            "url": "https://public.example/page",
                            "snippet": "Snippet",
                        }
                    ]
                }
            },
            request=request,
        )

    class RejectingTransport:
        async def get(self, url, *, headers, max_bytes):
            raise AssertionError("search must not call the public web transport")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as search_client:
        provider = WebSearchProvider(
            api_url="https://search.example/api",
            api_key="test-key",
            timeout=1,
            max_results=3,
            max_fetch_bytes=100_000,
            max_download_bytes=100_000,
            max_fetch_chars=30_000,
            search_client=search_client,
            web_transport=RejectingTransport(),
        )

        results = await provider.search("test")

    assert [result.url for result in results] == [
        "https://public.example/page"
    ]


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
    status_code,
    error_code,
    retryable,
):
    transport = FakeWebTransport(
        lambda url, headers, max_bytes: WebTransportResponse(
            status_code=status_code,
            url=url,
            headers={"retry-after": "3"},
            body=b"",
        )
    )
    provider = _provider(transport)

    with pytest.raises(WebProviderError) as captured:
        await provider.fetch("https://public.example/private-path?token=secret")

    assert captured.value.code == error_code
    assert captured.value.status_code == status_code
    assert captured.value.retryable is retryable
    assert captured.value.retry_after == "3"


@pytest.mark.asyncio
async def test_fetch_limits_concurrency_per_host():
    active = 0
    max_active = 0

    class ConcurrentTransport:
        async def get(self, url, *, headers, max_bytes):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return WebTransportResponse(
                200,
                url,
                {"content-type": "text/plain"},
                b"ok",
            )

    provider = WebSearchProvider(
        api_url="https://search.example/api",
        api_key="test-key",
        timeout=1,
        max_results=3,
        max_fetch_bytes=100_000,
        max_download_bytes=100_000,
        max_fetch_chars=30_000,
        max_host_concurrency=2,
        web_transport=ConcurrentTransport(),
    )

    await asyncio.gather(
        *(
            provider.fetch(f"https://public.example/page-{index}")
            for index in range(6)
        )
    )

    assert max_active == 2


@pytest.mark.asyncio
async def test_redirects_use_transport_for_each_target():
    responses = iter(
        (
            WebTransportResponse(
                302,
                "https://public.example/start",
                {"location": "https://cdn.example/final"},
                b"",
            ),
            WebTransportResponse(
                200,
                "https://cdn.example/final",
                {"content-type": "text/plain"},
                b"ok",
            ),
        )
    )
    transport = FakeWebTransport(
        lambda url, headers, max_bytes: next(responses)
    )
    provider = _provider(transport)

    page = await provider.fetch("https://public.example/start")

    assert page.url == "https://cdn.example/final"
    assert [call[0] for call in transport.calls] == [
        "https://public.example/start",
        "https://cdn.example/final",
    ]


@pytest.mark.asyncio
async def test_transport_timeout_is_retryable():
    class TimeoutTransport:
        async def get(self, url, *, headers, max_bytes):
            raise WebTransportError(retryable=True)

    provider = WebSearchProvider(
        api_url="https://search.example/api",
        api_key="test-key",
        timeout=1,
        max_results=3,
        max_fetch_bytes=100_000,
        max_download_bytes=100_000,
        max_fetch_chars=30_000,
        web_transport=TimeoutTransport(),
    )

    with pytest.raises(WebProviderError) as captured:
        await provider.fetch("https://public.example/page")

    assert captured.value.code == "FETCH_UNAVAILABLE"
    assert captured.value.retryable is True


class _FakeStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeResponse:
    def __init__(
        self,
        *,
        chunks: list[Any],
        headers: Mapping[str, bytes] | None = None,
    ) -> None:
        self.status = SimpleNamespace(as_int=lambda: 200)
        self.url = "https://public.example/page"
        self.headers = headers or {"content-type": b"text/plain"}
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    def stream(self):
        return _FakeStream(self._chunks)


class _FakeRequest:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None


class _FakeWreqClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.closed = False
        self.requests: list[tuple[str, Mapping[str, str]]] = []

    def get(self, url: str, *, headers: Mapping[str, str]):
        self.requests.append((url, headers))
        return _FakeRequest(self.response)

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_wreq_transport_pins_dns_and_streams_memoryviews():
    created: list[tuple[dict[str, Any], _FakeWreqClient]] = []

    def client_factory(**kwargs):
        client = _FakeWreqClient(
            _FakeResponse(chunks=[memoryview(b"hello"), memoryview(b" world")])
        )
        created.append((kwargs, client))
        return client

    async def resolver(host: str, port: int):
        assert (host, port) == ("public.example", 443)
        return ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")

    transport = WreqWebTransport(
        timeout=3,
        resolver=resolver,
        client_factory=client_factory,
    )

    response = await transport.get(
        "https://public.example/page",
        headers={"Accept": "text/plain"},
        max_bytes=100,
    )

    assert response.body == b"hello world"
    assert response.headers["content-type"] == "text/plain"
    assert len(created) == 1
    kwargs, client = created[0]
    assert kwargs["no_proxy"] is True
    assert kwargs["cookie_store"] is False
    assert kwargs["tls_verify"] is True
    assert kwargs["tls_verify_hostname"] is True
    assert kwargs["redirect"] is not None
    assert kwargs["dns_options"] is not None
    assert client.closed is True


def test_wreq_header_map_is_converted_without_mapping_items():
    headers = wreq.HeaderMap(
        {
            "content-type": "text/plain; charset=utf-8",
            "location": "/next",
        }
    )

    assert WreqWebTransport._headers(headers) == {
        "content-type": "text/plain; charset=utf-8",
        "location": "/next",
    }


@pytest.mark.asyncio
async def test_wreq_transport_rejects_private_dns_before_client_creation():
    created = False

    def client_factory(**kwargs):
        nonlocal created
        created = True
        raise AssertionError("client must not be created")

    async def resolver(host: str, port: int):
        return ("93.184.216.34", "127.0.0.1")

    transport = WreqWebTransport(
        timeout=1,
        resolver=resolver,
        client_factory=client_factory,
    )

    with pytest.raises(WebProviderError, match="FETCH_PRIVATE_ADDRESS"):
        await transport.get(
            "https://public.example/page",
            headers={},
            max_bytes=100,
        )

    assert created is False


@pytest.mark.asyncio
async def test_wreq_transport_enforces_stream_limit_and_closes_client():
    client = _FakeWreqClient(
        _FakeResponse(chunks=[memoryview(b"123"), memoryview(b"456")])
    )

    async def resolver(host: str, port: int):
        return ("93.184.216.34",)

    transport = WreqWebTransport(
        timeout=1,
        resolver=resolver,
        client_factory=lambda **kwargs: client,
    )

    with pytest.raises(WebProviderError, match="FETCH_TOO_LARGE"):
        await transport.get(
            "https://public.example/page",
            headers={},
            max_bytes=5,
        )

    assert client.closed is True


@pytest.mark.asyncio
async def test_wreq_transport_maps_client_builder_failure():
    async def resolver(host: str, port: int):
        return ("93.184.216.34",)

    def fail_client(**kwargs):
        raise wreq.BuilderError("invalid client configuration")

    transport = WreqWebTransport(
        timeout=1,
        resolver=resolver,
        client_factory=fail_client,
    )

    with pytest.raises(WebTransportError) as captured:
        await transport.get(
            "https://public.example/page",
            headers={},
            max_bytes=100,
        )

    assert captured.value.retryable is False


@pytest.mark.asyncio
async def test_http_trace_excludes_url_path_query_and_body():
    traces: list[str] = []

    transport = FakeWebTransport(
        lambda url, headers, max_bytes: WebTransportResponse(
            status_code=429,
            url=url,
            headers={"retry-after": "5"},
            body=b"sensitive-body",
        )
    )
    provider = _provider(transport, trace=traces.append)

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
    assert event["transport"] == "wreq"
    assert event["profile"] == "chrome149-linux"
    assert event["retryable"] is True
    assert event["retry_after"] == "5"

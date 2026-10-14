import json

import httpx
import pytest

from core.tools import ToolContext
from providers.web_search import (
    ANYSEARCH_URL,
    AnySearchProvider,
    UrlFetchProvider,
    WebSearchError,
    WebSearchTools,
)


class FakeSearchClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append((url, kwargs))
        return self.response


def response(
    status_code: int = 200,
    *,
    payload: object | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    request = httpx.Request("POST", ANYSEARCH_URL)
    if content is not None:
        return httpx.Response(status_code, content=content, request=request)
    return httpx.Response(status_code, json=payload, request=request)


async def test_provider_returns_structured_results_and_authenticates():
    client = FakeSearchClient(
        response(
            payload={
                "code": 200,
                "data": {
                    "results": [
                        {
                            "title": "目标视频",
                            "url": "https://www.bilibili.com/video/BV1VRHr6AEZ6/",
                            "content": "视频摘要",
                        },
                        {
                            "title": "bad",
                            "url": "javascript:alert(1)",
                            "snippet": "ignored",
                        },
                    ]
                },
            }
        )
    )
    provider = AnySearchProvider("secret-key", client)

    results = await provider.search("黄仁勋 世界巡演 AI MV大赛")

    assert len(results) == 1
    assert results[0].title == "目标视频"
    assert results[0].url.endswith("/BV1VRHr6AEZ6/")
    assert results[0].snippet == "视频摘要"
    url, request = client.calls[0]
    assert url == ANYSEARCH_URL
    assert request["headers"]["Authorization"] == "Bearer secret-key"
    assert request["json"] == {
        "query": "黄仁勋 世界巡演 AI MV大赛",
        "max_results": 8,
        "zone": "cn",
        "language": "zh-CN",
    }


@pytest.mark.parametrize(
    ("search_response", "error_code"),
    [
        (response(429, payload={}), "ANYSEARCH_RATE_LIMITED"),
        (response(503, payload={}), "ANYSEARCH_HTTP_ERROR"),
        (response(payload={"code": 500}), "ANYSEARCH_API_ERROR"),
        (response(content=b"not-json"), "ANYSEARCH_INVALID_RESPONSE"),
    ],
)
async def test_provider_rejects_search_failures(
    search_response: httpx.Response,
    error_code: str,
):
    provider = AnySearchProvider(
        "secret-key",
        FakeSearchClient(search_response),
    )

    with pytest.raises(WebSearchError, match=error_code):
        await provider.search("query")


async def test_tool_returns_explicit_retryable_error():
    tools = WebSearchTools(
        AnySearchProvider(
            "secret-key",
            FakeSearchClient(response(429, payload={})),
        )
    )

    result = await tools.search_web(
        ToolContext(1, 2, 3, "tester"),
        {"query": "query"},
    )

    assert result.is_error
    assert result.error_code == "ANYSEARCH_RATE_LIMITED"
    assert json.loads(result.content)["retryable"] is True


async def test_tool_preserves_empty_results_without_claiming_a_match():
    tools = WebSearchTools(
        AnySearchProvider(
            "secret-key",
            FakeSearchClient(
                response(payload={"code": 200, "data": {"results": []}})
            ),
        )
    )

    result = await tools.search_web(
        ToolContext(1, 2, 3, "tester"),
        {"query": "missing"},
    )
    payload = json.loads(result.content)

    assert not result.is_error
    assert payload["query"] == "missing"
    assert payload["results"] == []
    assert "低信任" in payload["notice"]


async def test_fetch_url_extracts_bounded_html_as_untrusted_content(
    monkeypatch,
):
    async def allow_public(url: str) -> None:
        assert url == "https://example.com/page"

    monkeypatch.setattr(
        UrlFetchProvider,
        "_validate_public_url",
        staticmethod(allow_public),
    )

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=(
                b"<html><head><title>Example</title>"
                b"<script>ignore()</script></head>"
                b"<body><h1>Heading</h1><p>Useful text</p></body></html>"
            ),
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        follow_redirects=False,
    ) as client:
        tools = WebSearchTools(
            AnySearchProvider(
                "secret-key",
                FakeSearchClient(response(payload={"results": []})),
            ),
            UrlFetchProvider(client),
        )
        result = await tools.fetch_url(
            ToolContext(1, 2, 3, "tester"),
            {"url": "https://example.com/page"},
        )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["final_url"] == "https://example.com/page"
    assert payload["title"] == "Example"
    assert "Heading" in payload["text"]
    assert "Useful text" in payload["text"]
    assert "ignore()" not in payload["text"]
    assert "低信任" in payload["notice"]


async def test_fetch_url_rejects_private_address_before_request():
    called = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        follow_redirects=False,
    ) as client:
        fetcher = UrlFetchProvider(client)
        with pytest.raises(WebSearchError, match="FETCH_PRIVATE_ADDRESS"):
            await fetcher.fetch("http://127.0.0.1/secret")

    assert not called


async def test_fetch_url_revalidates_redirect_target(monkeypatch):
    checked: list[str] = []

    async def validate(url: str) -> None:
        checked.append(url)
        if url.startswith("http://127.0.0.1"):
            raise WebSearchError("FETCH_PRIVATE_ADDRESS")

    monkeypatch.setattr(
        UrlFetchProvider,
        "_validate_public_url",
        staticmethod(validate),
    )

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/admin"},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        follow_redirects=False,
    ) as client:
        with pytest.raises(WebSearchError, match="FETCH_PRIVATE_ADDRESS"):
            await UrlFetchProvider(client).fetch("https://example.com/start")

    assert checked == [
        "https://example.com/start",
        "http://127.0.0.1/admin",
    ]

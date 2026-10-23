from __future__ import annotations

import httpx
import pytest

from providers.web import WebProviderError, WebSearchProvider
from sandbox.app import execute, validate_script


def test_code_runner_allows_standard_imports_and_file_operations():
    script = """
import json
import os
import urllib.parse

payload = {"cwd": os.getcwd(), "value": urllib.parse.quote("南瓜")}
with open("result.json", "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False)
print("ok")
"""
    validate_script(script)
    result = execute(script, 5)

    assert result["exit_code"] == 0
    assert result["stdout"] == "ok\n"
    assert result["stderr"] == ""
    assert [item["name"] for item in result["files"]] == ["result.json"]


@pytest.mark.asyncio
async def test_search_provider_requires_explicit_api_key():
    provider = WebSearchProvider(
        api_url="https://example.invalid/search",
        api_key=None,
        timeout=1,
        max_results=3,
        max_fetch_bytes=100_000,
        max_image_bytes=5 * 1024 * 1024,
        max_fetch_chars=1_000,
    )
    with pytest.raises(WebProviderError, match="SEARCH_NOT_CONFIGURED"):
        await provider.search("test")


@pytest.mark.asyncio
async def test_image_download_has_a_separate_size_limit(monkeypatch):
    body = b"x" * (2 * 1024 * 1024)

    def respond(request: httpx.Request) -> httpx.Response:
        content_type = (
            "text/plain"
            if request.url.path.endswith(".txt")
            else "image/jpeg"
        )
        return httpx.Response(
            200,
            headers={
                "content-type": content_type,
                "content-length": str(len(body)),
            },
            content=body,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = WebSearchProvider(
            api_url="https://example.invalid/search",
            api_key=None,
            timeout=1,
            max_results=3,
            max_fetch_bytes=1024 * 1024,
            max_image_bytes=5 * 1024 * 1024,
            max_fetch_chars=1_000,
            client=client,
        )

        async def allow_public_url(url: str) -> None:
            assert url in {
                "https://assets.example/image.jpg",
                "https://assets.example/page.txt",
            }

        monkeypatch.setattr(provider, "_validate_public_url", allow_public_url)

        _, content_type, content = await provider.download_image(
            "https://assets.example/image.jpg"
        )
        assert content_type == "image/jpeg"
        assert content == body

        with pytest.raises(WebProviderError, match="FETCH_TOO_LARGE"):
            await provider.fetch("https://assets.example/page.txt")

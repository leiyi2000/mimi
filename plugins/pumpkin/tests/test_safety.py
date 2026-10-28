from __future__ import annotations

import pytest

from providers.web import (
    WebProviderError,
    WebSearchProvider,
    WebTransportResponse,
)
from sandbox.app import MAX_FILE_BYTES, MAX_TOTAL_FILE_BYTES, execute, validate_script


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


def test_code_runner_collects_file_larger_than_previous_limit():
    script = """
with open("large.bin", "wb") as handle:
    handle.truncate(6 * 1024 * 1024)
"""

    result = execute(script, 5)

    assert MAX_FILE_BYTES == 20 * 1024 * 1024
    assert MAX_TOTAL_FILE_BYTES == 20 * 1024 * 1024
    assert result["truncated"] is False
    assert [item["name"] for item in result["files"]] == ["large.bin"]


@pytest.mark.asyncio
async def test_search_provider_requires_explicit_api_key():
    provider = WebSearchProvider(
        api_url="https://example.invalid/search",
        api_key=None,
        timeout=1,
        max_results=3,
        max_fetch_bytes=100_000,
        max_download_bytes=5 * 1024 * 1024,
        max_fetch_chars=1_000,
    )
    with pytest.raises(WebProviderError, match="SEARCH_NOT_CONFIGURED"):
        await provider.search("test")


@pytest.mark.asyncio
async def test_file_download_has_a_separate_size_limit():
    body = b"x" * (6 * 1024 * 1024)

    class Transport:
        async def get(self, url, *, headers, max_bytes):
            if len(body) > max_bytes:
                raise WebProviderError("FETCH_TOO_LARGE")
            if url.endswith(".txt"):
                content_type = "text/plain"
            elif url.endswith(".zip"):
                content_type = "application/zip"
            else:
                content_type = "image/jpeg"
            return WebTransportResponse(
                200,
                url,
                {
                    "content-type": content_type,
                    "content-length": str(len(body)),
                },
                body,
            )

    provider = WebSearchProvider(
        api_url="https://example.invalid/search",
        api_key=None,
        timeout=1,
        max_results=3,
        max_fetch_bytes=1024 * 1024,
        max_download_bytes=20 * 1024 * 1024,
        max_fetch_chars=1_000,
        web_transport=Transport(),
    )

    downloaded = await provider.download_file(
        "https://assets.example/image.jpg"
    )
    assert downloaded.content_type == "image/jpeg"
    assert downloaded.content == body

    archive = await provider.download_file(
        "https://assets.example/archive.zip"
    )
    assert archive.content_type == "application/zip"
    assert archive.content == body

    with pytest.raises(WebProviderError, match="FETCH_TOO_LARGE"):
        await provider.fetch("https://assets.example/page.txt")

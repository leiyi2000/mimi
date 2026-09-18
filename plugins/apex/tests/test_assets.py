import json

import httpx
import pytest

import cache_io
from features.rendering import assets


CONTENT = b"cached-image"
URL = "https://static.example/image.png"


async def test_asset_cache_honors_freshness_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "CACHE_DIR", tmp_path)
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            content=CONTENT,
            headers={"Cache-Control": "public, max-age=3600", "ETag": '"image-v1"'},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await assets.fetch_assets([URL], client) == {URL: CONTENT}
        assert await assets.fetch_assets([URL], client) == {URL: CONTENT}

    assert requests == 1
    metadata = json.loads(assets._metadata_path(URL).read_text(encoding="utf-8"))
    assert metadata["etag"] == '"image-v1"'


async def test_stale_asset_uses_conditional_request_and_304(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "CACHE_DIR", tmp_path)
    assets.atomic_write_bytes(assets._cache_path(URL), CONTENT)
    assets.atomic_write_json(
        assets._metadata_path(URL),
        {
            "url": URL,
            "etag": '"image-v1"',
            "last_modified": "Wed, 17 Sep 2026 10:00:00 GMT",
            "expires_at": 0,
        },
    )
    seen: httpx.Request | None = None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen = request
        return httpx.Response(
            304,
            headers={"Cache-Control": "max-age=600"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await assets.fetch_assets([URL], client) == {URL: CONTENT}

    assert seen is not None
    assert seen.headers["if-none-match"] == '"image-v1"'
    assert (
        seen.headers["if-modified-since"]
        == "Wed, 17 Sep 2026 10:00:00 GMT"
    )
    assert assets._read_cache(URL).fresh is True


async def test_stale_asset_survives_request_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "CACHE_DIR", tmp_path)
    assets.atomic_write_bytes(assets._cache_path(URL), CONTENT)
    assets.atomic_write_json(
        assets._metadata_path(URL),
        {"url": URL, "etag": None, "last_modified": None, "expires_at": 0},
    )

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        assert await assets.fetch_assets([URL], client) == {URL: CONTENT}


def test_atomic_write_failure_preserves_previous_file(tmp_path, monkeypatch):
    cache = tmp_path / "asset"
    cache.write_bytes(b"old")

    def fail_replace(source, destination):
        raise OSError("interrupted")

    monkeypatch.setattr(cache_io.os, "replace", fail_replace)

    with pytest.raises(OSError, match="interrupted"):
        cache_io.atomic_write_bytes(cache, b"new")

    assert cache.read_bytes() == b"old"
    assert list(tmp_path.glob(".asset.*")) == []

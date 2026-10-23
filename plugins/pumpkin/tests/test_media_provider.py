from __future__ import annotations

import pytest

from providers.media import MediaProviderError, MessageMediaProvider


class FakeNapCat:
    def __init__(self, response=None):
        self.response = response
        self.calls = []

    async def call_action(self, action, params):
        self.calls.append((action, params))
        return self.response


class FakeResponse:
    def __init__(self, content: bytes, *, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.headers = {
            "content-type": "application/octet-stream",
            "content-length": str(len(content)),
        }
        self.url = "https://cdn.test/image.png"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aiter_bytes(self):
        yield self.content


class FakeHttpClient:
    response: FakeResponse

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def stream(self, _method, _url, **_kwargs):
        return self.response


@pytest.mark.asyncio
async def test_media_provider_resolves_napcat_file_and_fetches_image(monkeypatch):
    http_response = FakeResponse(b"\x89PNG\r\n\x1a\nimage")
    FakeHttpClient.response = http_response
    monkeypatch.setattr("providers.media.httpx.AsyncClient", FakeHttpClient)
    napcat = FakeNapCat({"data": {"url": "https://cdn.test/image.png"}})
    provider = MessageMediaProvider(
        client=napcat,
        timeout=5,
        max_bytes=1024,
        trusted_hosts=("cdn.test",),
    )

    result = await provider.fetch("image", {"file": "image-id"})

    assert result.content == http_response.content
    assert result.content_type == "image/png"
    assert napcat.calls == [("get_image", {"file": "image-id"})]


@pytest.mark.asyncio
async def test_media_provider_rejects_expired_reference(monkeypatch):
    FakeHttpClient.response = FakeResponse(b"", status_code=410)
    monkeypatch.setattr("providers.media.httpx.AsyncClient", FakeHttpClient)
    provider = MessageMediaProvider(
        client=FakeNapCat(),
        timeout=5,
        max_bytes=1024,
        trusted_hosts=("cdn.test",),
    )

    with pytest.raises(MediaProviderError, match="MEDIA_EXPIRED"):
        await provider.fetch("image", {"url": "https://cdn.test/image.png"})


@pytest.mark.asyncio
async def test_media_provider_rejects_non_image_content(monkeypatch):
    FakeHttpClient.response = FakeResponse(b"plain text")
    monkeypatch.setattr("providers.media.httpx.AsyncClient", FakeHttpClient)
    provider = MessageMediaProvider(
        client=FakeNapCat(),
        timeout=5,
        max_bytes=1024,
        trusted_hosts=("cdn.test",),
    )

    with pytest.raises(MediaProviderError, match="MEDIA_CONTENT_UNSUPPORTED"):
        await provider.fetch("image", {"url": "https://cdn.test/image.png"})

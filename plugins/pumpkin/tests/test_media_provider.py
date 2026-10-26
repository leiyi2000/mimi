from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

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


def animated_gif_bytes(
    *,
    first_color: tuple[int, int, int, int] = (255, 0, 0, 255),
) -> bytes:
    first = Image.new("RGBA", (2, 2), first_color)
    second = Image.new("RGBA", (2, 2), (0, 0, 255, 255))
    output = BytesIO()
    first.save(
        output,
        format="GIF",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
        transparency=0 if first_color[3] == 0 else None,
    )
    first.close()
    second.close()
    return output.getvalue()


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
async def test_media_provider_converts_only_first_gif_frame_to_png(monkeypatch):
    FakeHttpClient.response = FakeResponse(animated_gif_bytes())
    monkeypatch.setattr("providers.media.httpx.AsyncClient", FakeHttpClient)
    provider = MessageMediaProvider(
        client=FakeNapCat(),
        timeout=5,
        max_bytes=1024,
        trusted_hosts=("cdn.test",),
    )

    result = await provider.fetch("mface", {"url": "https://cdn.test/image.gif"})

    assert result.content_type == "image/png"
    with Image.open(BytesIO(result.content)) as image:
        assert image.format == "PNG"
        assert getattr(image, "n_frames", 1) == 1
        assert image.convert("RGBA").getpixel((0, 0)) == (255, 0, 0, 255)


@pytest.mark.asyncio
async def test_media_provider_preserves_transparent_gif_first_frame(monkeypatch):
    FakeHttpClient.response = FakeResponse(
        animated_gif_bytes(first_color=(255, 0, 0, 0))
    )
    monkeypatch.setattr("providers.media.httpx.AsyncClient", FakeHttpClient)
    provider = MessageMediaProvider(
        client=FakeNapCat(),
        timeout=5,
        max_bytes=1024,
        trusted_hosts=("cdn.test",),
    )

    result = await provider.fetch("image", {"url": "https://cdn.test/image.gif"})

    with Image.open(BytesIO(result.content)) as image:
        assert image.convert("RGBA").getpixel((0, 0))[3] == 0


@pytest.mark.asyncio
async def test_media_provider_rejects_damaged_gif(monkeypatch):
    FakeHttpClient.response = FakeResponse(b"GIF89a-damaged")
    monkeypatch.setattr("providers.media.httpx.AsyncClient", FakeHttpClient)
    provider = MessageMediaProvider(
        client=FakeNapCat(),
        timeout=5,
        max_bytes=1024,
        trusted_hosts=("cdn.test",),
    )

    with pytest.raises(MediaProviderError, match="MEDIA_CONTENT_UNSUPPORTED"):
        await provider.fetch("image", {"url": "https://cdn.test/image.gif"})


@pytest.mark.asyncio
async def test_media_provider_rejects_gif_over_download_limit(monkeypatch):
    FakeHttpClient.response = FakeResponse(b"GIF89a" + b"x" * (64 * 1024))
    monkeypatch.setattr("providers.media.httpx.AsyncClient", FakeHttpClient)
    provider = MessageMediaProvider(
        client=FakeNapCat(),
        timeout=5,
        max_bytes=64 * 1024,
        trusted_hosts=("cdn.test",),
    )

    with pytest.raises(MediaProviderError, match="MEDIA_TOO_LARGE"):
        await provider.fetch("image", {"url": "https://cdn.test/image.gif"})


@pytest.mark.asyncio
async def test_media_provider_rejects_gif_over_pixel_limit(monkeypatch):
    FakeHttpClient.response = FakeResponse(animated_gif_bytes())
    monkeypatch.setattr("providers.media.httpx.AsyncClient", FakeHttpClient)
    monkeypatch.setattr(MessageMediaProvider, "_MAX_IMAGE_PIXELS", 3)
    provider = MessageMediaProvider(
        client=FakeNapCat(),
        timeout=5,
        max_bytes=1024,
        trusted_hosts=("cdn.test",),
    )

    with pytest.raises(MediaProviderError, match="MEDIA_PIXEL_LIMIT_EXCEEDED"):
        await provider.fetch("image", {"url": "https://cdn.test/image.gif"})


@pytest.mark.asyncio
async def test_media_provider_reports_gif_conversion_failure(monkeypatch):
    gif_content = animated_gif_bytes()
    FakeHttpClient.response = FakeResponse(gif_content)
    monkeypatch.setattr("providers.media.httpx.AsyncClient", FakeHttpClient)

    def fail_save(*_args, **_kwargs):
        raise OSError("encoding failed")

    monkeypatch.setattr(Image.Image, "save", fail_save)
    provider = MessageMediaProvider(
        client=FakeNapCat(),
        timeout=5,
        max_bytes=1024,
        trusted_hosts=("cdn.test",),
    )

    with pytest.raises(MediaProviderError, match="MEDIA_CONVERSION_FAILED"):
        await provider.fetch("image", {"url": "https://cdn.test/image.gif"})


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

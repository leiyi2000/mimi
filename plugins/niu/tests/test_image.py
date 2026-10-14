import io

from PIL import Image

import providers.image as image_module
from core.tools import ToolImage
from providers.image import ImageProvider
from providers.media import DownloadedMedia


class FakeDownloader:
    def __init__(self):
        self.urls = []

    async def download(self, url, allowed_mime_types):
        self.urls.append((url, allowed_mime_types))
        return DownloadedMedia(b"generated", "image/png")

    @staticmethod
    def validate_image(media):
        assert media.data == b"generated"


async def test_image_provider_sends_base64_subject_reference(monkeypatch):
    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "data": {"image_urls": ["https://example.com/result.png"]},
                "base_resp": {"status_code": 0},
            }

    class FakeAsyncClient:
        payload = None

        def __init__(self, *, timeout):
            assert timeout == 30

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, *, headers, json):
            assert url == "https://api.example/v1/image_generation"
            assert headers == {"Authorization": "Bearer token"}
            type(self).payload = json
            return FakeResponse()

    monkeypatch.setattr(image_module.httpx, "AsyncClient", FakeAsyncClient)
    downloader = FakeDownloader()
    provider = ImageProvider(
        api_base_url="https://api.example/v1",
        api_key="token",
        downloader=downloader,
        timeout=30,
    )

    artifact = await provider.generate(
        "保持人物形象，画他吃白米饭",
        "4:3",
        ToolImage(b"reference", "image/png", "人物"),
    )

    assert FakeAsyncClient.payload["subject_reference"] == [
        {
            "type": "character",
            "image_file": "data:image/png;base64,cmVmZXJlbmNl",
        }
    ]
    assert artifact.data == b"generated"
    assert downloader.urls == [
        (
            "https://example.com/result.png",
            {"image/jpeg", "image/png", "image/webp"},
        )
    ]


def test_image_provider_converts_webp_reference_to_jpeg():
    source = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(source, format="WEBP")

    data_uri = ImageProvider._reference_data_uri(
        ToolImage(source.getvalue(), "image/webp", "人物")
    )

    assert data_uri.startswith("data:image/jpeg;base64,")

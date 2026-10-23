from __future__ import annotations

from typing import ClassVar

import pytest

from providers.audio import AudioProvider, AudioTranscriptionProvider
from providers.image import ImageProvider


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.content = b""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    requests: ClassVar[list[tuple[str, dict]]] = []

    def __init__(self, **kwargs):
        del kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, *, headers, json=None, data=None, files=None):
        self.requests.append((url, json if json is not None else data))
        if url.endswith("/image_generation"):
            return FakeResponse(
                {"data": {"image_urls": ["https://cdn.test/image.png"]}}
            )
        if url.endswith("/speech_to_text"):
            return FakeResponse({"text": "这是语音转写结果", "duration": 1.2})
        return FakeResponse(
            {
                "data": {"audio": (b"ID3" + b"\x00" * 8).hex()},
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }
        )

    async def get(self, url):
        response = FakeResponse({})
        response.content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        return response


@pytest.mark.asyncio
async def test_image_provider_supports_reference_generation(monkeypatch):
    FakeClient.requests = []
    monkeypatch.setattr("providers.image.httpx.AsyncClient", FakeClient)
    provider = ImageProvider(
        api_base_url="https://api.test/v1",
        api_key="test-key",
        model="image-01",
        timeout=5,
        max_prompt_chars=1500,
    )

    images = await provider.generate(
        "portrait",
        "16:9",
        model="image-01",
        n=1,
        prompt_optimizer=True,
        subject_reference=[
            {"type": "character", "image_file": "https://cdn.test/reference.png"}
        ],
    )

    assert len(images) == 1
    assert images[0].extension == "png"
    payload = FakeClient.requests[0][1]
    assert payload["subject_reference"][0]["type"] == "character"
    assert payload["prompt_optimizer"] is True


@pytest.mark.asyncio
async def test_audio_provider_decodes_hex_audio(monkeypatch):
    FakeClient.requests = []
    monkeypatch.setattr("providers.audio.httpx.AsyncClient", FakeClient)
    provider = AudioProvider(
        api_base_url="https://api.test/v1",
        api_key="test-key",
        model="speech-2.8-hd",
        timeout=5,
        max_text_chars=10000,
    )

    audio = await provider.synthesize("测试")

    assert audio.extension == "mp3"
    assert audio.data.startswith(b"ID3")
    assert FakeClient.requests[0][1]["output_format"] == "hex"


@pytest.mark.asyncio
async def test_minimax_audio_transcription_uses_speech_to_text(monkeypatch):
    FakeClient.requests = []
    monkeypatch.setattr("providers.audio.httpx.AsyncClient", FakeClient)
    provider = AudioTranscriptionProvider(
        api_base_url="https://api.minimax.cn",
        api_key="test-key",
        model="asr-1.0",
        timeout=5,
    )

    text = await provider.transcribe(
        b"ID3voice",
        filename="voice.mp3",
        content_type="audio/mpeg",
    )

    assert text == "这是语音转写结果"
    assert FakeClient.requests[0][0] == "https://api.minimax.cn/v1/speech_to_text"
    assert FakeClient.requests[0][1]["model"] == "asr-1.0"

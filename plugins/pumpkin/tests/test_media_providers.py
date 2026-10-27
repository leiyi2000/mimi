from __future__ import annotations

from typing import ClassVar

import pytest

from providers.audio import (
    AudioProviderError,
    AudioSynthesisRequest,
    MinimaxAudioProvider,
    MinimaxAudioTranscriptionProvider,
    QwenLocalAudioProvider,
    QwenAudioLanguage,
    TimbreWeight,
    VoiceCloneRequest,
    VoiceEffect,
    VoiceModification,
)
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
    monkeypatch.setattr("providers.audio.minimax.httpx.AsyncClient", FakeClient)
    provider = MinimaxAudioProvider(
        api_base_url="https://api.test/v1",
        api_key="test-key",
        model="speech-2.8-hd",
        timeout=5,
        max_text_chars=10000,
    )

    audio = await provider.synthesize(AudioSynthesisRequest(text="测试"))

    assert audio.extension == "mp3"
    assert audio.data.startswith(b"ID3")
    assert FakeClient.requests[0][1]["output_format"] == "hex"


@pytest.mark.asyncio
async def test_audio_provider_supports_voice_modification_and_timbre_mix(monkeypatch):
    FakeClient.requests = []
    monkeypatch.setattr("providers.audio.minimax.httpx.AsyncClient", FakeClient)
    provider = MinimaxAudioProvider(
        api_base_url="https://api.test/v1",
        api_key="test-key",
        model="speech-2.8-hd",
        timeout=5,
        max_text_chars=10000,
    )

    await provider.synthesize(
        AudioSynthesisRequest(
            text="测试",
            voice_id=None,
            timbre_weights=(
                TimbreWeight("male-qn-qingse", 60),
                TimbreWeight("female-shaonv", 40),
            ),
            voice_modify=VoiceModification(
                pitch=-20,
                intensity=-10,
                timbre=25,
                sound_effects=VoiceEffect.SPACIOUS_ECHO,
            ),
        ),
    )

    payload = FakeClient.requests[0][1]
    assert payload["voice_setting"]["voice_id"] == ""
    assert payload["timbre_weights"] == [
        {"voice_id": "male-qn-qingse", "weight": 60},
        {"voice_id": "female-shaonv", "weight": 40},
    ]
    assert payload["voice_modify"] == {
        "pitch": -20,
        "intensity": -10,
        "timbre": 25,
        "sound_effects": "spacious_echo",
    }


@pytest.mark.asyncio
async def test_audio_provider_rejects_voice_selection_conflict(monkeypatch):
    FakeClient.requests = []
    monkeypatch.setattr("providers.audio.minimax.httpx.AsyncClient", FakeClient)
    provider = MinimaxAudioProvider(
        api_base_url="https://api.test/v1",
        api_key="test-key",
        model="speech-2.8-hd",
        timeout=5,
        max_text_chars=10000,
    )

    with pytest.raises(AudioProviderError) as exc_info:
        await provider.synthesize(
            AudioSynthesisRequest(
                text="测试",
                voice_id="male-qn-qingse",
                timbre_weights=(
                    TimbreWeight("male-qn-qingse", 60),
                    TimbreWeight("female-shaonv", 40),
                ),
            ),
        )

    assert exc_info.value.code == "AUDIO_VOICE_SELECTION_CONFLICT"
    assert FakeClient.requests == []


@pytest.mark.asyncio
async def test_audio_provider_preserves_minimax_business_failure(monkeypatch):
    class BusinessFailureClient(FakeClient):
        async def post(self, url, *, headers, json=None, data=None, files=None):
            del url, headers, json, data, files
            return FakeResponse(
                {
                    "data": None,
                    "base_resp": {
                        "status_code": 2061,
                        "status_msg": "plan not supported",
                    },
                }
            )

    monkeypatch.setattr(
        "providers.audio.minimax.httpx.AsyncClient",
        BusinessFailureClient,
    )
    provider = MinimaxAudioProvider(
        api_base_url="https://api.test/v1",
        api_key="test-key",
        model="speech-2.8-hd",
        timeout=5,
        max_text_chars=10000,
    )

    with pytest.raises(AudioProviderError) as exc_info:
        await provider.synthesize(AudioSynthesisRequest(text="测试"))

    assert exc_info.value.code == "AUDIO_SYNTHESIS_FAILED"


@pytest.mark.asyncio
async def test_minimax_audio_transcription_uses_speech_to_text(monkeypatch):
    FakeClient.requests = []
    monkeypatch.setattr("providers.audio.minimax.httpx.AsyncClient", FakeClient)
    provider = MinimaxAudioTranscriptionProvider(
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


class QwenResponse:
    def __init__(
        self,
        *,
        status_code=200,
        payload=None,
        content=b"",
        content_type="application/json",
    ):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = {"content-type": content_type}

    def json(self):
        return self._payload


class QwenClient:
    requests: ClassVar[list[tuple[str, dict]]] = []
    response = QwenResponse()

    def __init__(self, **kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, *, json):
        self.requests.append((url, json))
        return self.response

    async def get(self, url):
        self.requests.append((url, {}))
        return self.response


@pytest.mark.asyncio
async def test_qwen_audio_provider_synthesizes_wav(monkeypatch):
    wav = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 8
    QwenClient.requests = []
    QwenClient.response = QwenResponse(
        content=wav,
        content_type="audio/wav",
    )
    monkeypatch.setattr("providers.audio.qwen.httpx.AsyncClient", QwenClient)
    provider = QwenLocalAudioProvider(
        api_base_url="http://localhost:8765",
        timeout=5,
        max_text_chars=1000,
    )

    audio = await provider.synthesize(
        AudioSynthesisRequest(
            text="测试",
            voice_id="lillia_zh",
            audio_format="wav",
            language=QwenAudioLanguage.CHINESE,
        )
    )

    assert audio.data == wav
    assert audio.extension == "wav"
    assert QwenClient.requests == [
        (
            "http://localhost:8765/v1/audio/speech",
            {
                "input": "测试",
                "voice_id": "lillia_zh",
                "language": "Chinese",
            },
        )
    ]


@pytest.mark.asyncio
async def test_qwen_audio_provider_rejects_unsupported_unified_setting():
    provider = QwenLocalAudioProvider(
        api_base_url="http://localhost:8765",
        timeout=5,
        max_text_chars=1000,
    )

    with pytest.raises(AudioProviderError) as captured:
        await provider.synthesize(
            AudioSynthesisRequest(
                text="测试",
                voice_id="lillia_zh",
                audio_format="wav",
                speed=1.2,
                language=QwenAudioLanguage.CHINESE,
            )
        )

    assert captured.value.code == "AUDIO_SETTING_UNSUPPORTED"


@pytest.mark.asyncio
async def test_qwen_audio_provider_clones_authorized_voice(monkeypatch):
    QwenClient.requests = []
    QwenClient.response = QwenResponse(
        payload={
            "voice_id": "speaker_1",
            "created_at": "2026-10-27T00:00:00+00:00",
            "mode": "icl",
        }
    )
    monkeypatch.setattr("providers.audio.qwen.httpx.AsyncClient", QwenClient)
    provider = QwenLocalAudioProvider(
        api_base_url="http://localhost:8765",
        timeout=5,
        max_text_chars=1000,
    )

    summary = await provider.create_voice(
        VoiceCloneRequest(
            voice_id="speaker_1",
            reference_audio=b"RIFF" + b"\x00" * 300,
            reference_content_type="audio/wav",
            reference_text="参考文本",
            x_vector_only_mode=False,
            consent_confirmed=True,
        )
    )

    payload = QwenClient.requests[0][1]
    assert summary.voice_id == "speaker_1"
    assert payload["reference_audio_base64"].startswith("data:audio/wav;base64,")
    assert payload["reference_text"] == "参考文本"
    assert payload["consent_confirmed"] is True


@pytest.mark.asyncio
async def test_qwen_audio_provider_requires_clone_consent():
    provider = QwenLocalAudioProvider(
        api_base_url="http://localhost:8765",
        timeout=5,
        max_text_chars=1000,
    )

    with pytest.raises(AudioProviderError) as exc_info:
        await provider.create_voice(
            VoiceCloneRequest(
                voice_id="speaker_1",
                reference_audio=b"audio",
                reference_content_type="audio/wav",
                reference_text="参考文本",
                x_vector_only_mode=False,
                consent_confirmed=False,
            )
        )

    assert exc_info.value.code == "VOICE_CLONE_CONSENT_REQUIRED"

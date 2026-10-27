from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

import httpx

from core.config import AudioProviderKind, QwenAudioLanguage
from .contracts import (
    MAX_AUDIO_BYTES,
    AudioProviderCapabilities,
    AudioProviderError,
    AudioSynthesisRequest,
    GeneratedAudio,
    VoiceCloneRequest,
    VoiceSummary,
)


class QwenLocalAudioProvider:
    capabilities = AudioProviderCapabilities(
        provider_kind=AudioProviderKind.QWEN_LOCAL,
        formats=("wav",),
        supports_voice_clone=True,
    )

    def __init__(
        self,
        *,
        api_base_url: str,
        timeout: float,
        max_text_chars: int,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = timeout
        self.max_text_chars = max_text_chars

    async def synthesize(
        self,
        request: AudioSynthesisRequest,
    ) -> GeneratedAudio:
        text = request.text.strip()
        if not 1 <= len(text) <= self.max_text_chars:
            raise AudioProviderError("AUDIO_TEXT_INVALID")
        if not request.voice_id or not self._valid_voice_id(request.voice_id):
            raise AudioProviderError("AUDIO_VOICE_INVALID")
        if self._has_unsupported_synthesis_settings(request):
            raise AudioProviderError("AUDIO_SETTING_UNSUPPORTED")
        language = request.language or QwenAudioLanguage.CHINESE
        response = await self._post(
            "/v1/audio/speech",
            {
                "input": text,
                "voice_id": request.voice_id,
                "language": language.value,
            },
            operation="synthesis",
        )
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        data = response.content
        if (
            content_type != "audio/wav"
            or not self._is_wav(data)
            or len(data) > MAX_AUDIO_BYTES
        ):
            raise AudioProviderError("AUDIO_INVALID_RESPONSE")
        return GeneratedAudio(data=data, content_type="audio/wav", extension="wav")

    async def create_voice(self, request: VoiceCloneRequest) -> VoiceSummary:
        if not request.consent_confirmed:
            raise AudioProviderError("VOICE_CLONE_CONSENT_REQUIRED")
        if not self._valid_voice_id(request.voice_id):
            raise AudioProviderError("VOICE_CLONE_INPUT_INVALID")
        if not request.reference_audio:
            raise AudioProviderError("VOICE_CLONE_INPUT_INVALID")
        if not request.x_vector_only_mode and not (
            request.reference_text and request.reference_text.strip()
        ):
            raise AudioProviderError("VOICE_CLONE_REFERENCE_TEXT_REQUIRED")
        if len(request.reference_audio) > 25_000_000:
            raise AudioProviderError("VOICE_CLONE_INPUT_INVALID")
        encoded = base64.b64encode(request.reference_audio).decode("ascii")
        response = await self._post(
            "/v1/voices",
            {
                "voice_id": request.voice_id,
                "reference_audio_base64": (
                    f"data:{request.reference_content_type};base64,{encoded}"
                ),
                "reference_text": (
                    request.reference_text.strip() if request.reference_text else None
                ),
                "x_vector_only_mode": request.x_vector_only_mode,
                "consent_confirmed": True,
            },
            operation="clone",
        )
        return self._voice_summary(response)

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        operation: str,
    ) -> httpx.Response:
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{self.api_base_url}{path}",
                    json=payload,
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise AudioProviderError("AUDIO_UNAVAILABLE", retryable=True) from exc
        self._raise_for_status(response, operation=operation)
        return response

    def _raise_for_status(self, response: httpx.Response, *, operation: str) -> None:
        status = response.status_code
        if status < 400:
            return
        if status == 429:
            raise AudioProviderError("AUDIO_RATE_LIMITED", retryable=True)
        if status == 409 and operation == "clone":
            raise AudioProviderError("VOICE_PROFILE_EXISTS")
        if status == 404 and operation == "synthesis":
            raise AudioProviderError("AUDIO_VOICE_NOT_FOUND")
        if status in {400, 422}:
            code = (
                "VOICE_CLONE_INPUT_INVALID"
                if operation == "clone"
                else "AUDIO_SETTING_INVALID"
            )
            raise AudioProviderError(code)
        if status >= 500:
            code = "AUDIO_UNAVAILABLE" if status == 503 else "AUDIO_SYNTHESIS_FAILED"
            raise AudioProviderError(code, retryable=True)
        raise AudioProviderError("AUDIO_SYNTHESIS_FAILED")

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
        )

    @staticmethod
    def _voice_summary(response: httpx.Response) -> VoiceSummary:
        try:
            return QwenLocalAudioProvider._voice_summary_payload(response.json())
        except (TypeError, ValueError) as exc:
            raise AudioProviderError("AUDIO_INVALID_RESPONSE") from exc

    @staticmethod
    def _voice_summary_payload(payload: object) -> VoiceSummary:
        if not isinstance(payload, Mapping):
            raise TypeError("voice summary is not an object")
        voice_id = payload.get("voice_id")
        created_at = payload.get("created_at")
        mode = payload.get("mode")
        if not all(
            isinstance(item, str) and item for item in (voice_id, created_at, mode)
        ):
            raise ValueError("voice summary is incomplete")
        return VoiceSummary(
            voice_id=voice_id,
            created_at=created_at,
            mode=mode,
        )

    @staticmethod
    def _valid_voice_id(value: str) -> bool:
        return (
            1 <= len(value) <= 64
            and value[0].isalnum()
            and value.replace("_", "").replace("-", "").isalnum()
        )

    @staticmethod
    def _is_wav(data: bytes) -> bool:
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"

    @staticmethod
    def _has_unsupported_synthesis_settings(
        request: AudioSynthesisRequest,
    ) -> bool:
        return any(
            (
                request.model is not None,
                bool(request.timbre_weights),
                request.speed != 1.0,
                request.volume != 1.0,
                request.pitch != 0,
                request.emotion is not None,
                request.sample_rate != 32_000,
                request.bitrate != 128_000,
                request.audio_format not in {None, "wav"},
                request.channel != 1,
                request.language_boost is not None,
                request.voice_modify is not None,
            )
        )

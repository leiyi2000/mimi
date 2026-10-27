from __future__ import annotations

from typing import Any, ClassVar

import httpx

from core.config import AudioProviderKind
from .contracts import (
    MAX_AUDIO_BYTES,
    AudioProviderCapabilities,
    AudioProviderError,
    AudioSynthesisRequest,
    AudioTranscriptionError,
    GeneratedAudio,
    TimbreWeight,
)


class MinimaxAudioTranscriptionProvider:
    """MiniMax ASR adapter for inbound voice messages."""

    def __init__(
        self,
        *,
        api_base_url: str,
        api_key: str,
        model: str,
        timeout: float,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.model = model
        self.timeout = timeout

    async def transcribe(
        self,
        content: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> str:
        if not content:
            raise AudioTranscriptionError("AUDIO_EMPTY")
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    self._endpoint(),
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data={"model": self.model},
                    files={"file": (filename, content, content_type)},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.TimeoutException as exc:
            raise AudioTranscriptionError(
                "AUDIO_TRANSCRIPTION_TIMEOUT", retryable=True
            ) from exc
        except httpx.HTTPStatusError as exc:
            retryable = (
                exc.response.status_code == 429 or exc.response.status_code >= 500
            )
            raise AudioTranscriptionError(
                "AUDIO_TRANSCRIPTION_FAILED", retryable=retryable
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AudioTranscriptionError(
                "AUDIO_TRANSCRIPTION_FAILED", retryable=True
            ) from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise AudioTranscriptionError("AUDIO_TRANSCRIPTION_INVALID_RESPONSE")
        return text.strip()

    def _endpoint(self) -> str:
        if self.api_base_url.endswith("/v1"):
            return f"{self.api_base_url}/speech_to_text"
        return f"{self.api_base_url}/v1/speech_to_text"


class MinimaxAudioProvider:
    capabilities = AudioProviderCapabilities(
        provider_kind=AudioProviderKind.MINIMAX,
        formats=("mp3", "wav", "flac"),
    )
    _MODELS: ClassVar[frozenset[str]] = frozenset(
        {
            "speech-2.8-hd",
            "speech-2.8-turbo",
            "speech-2.6-hd",
            "speech-2.6-turbo",
            "speech-02-hd",
            "speech-02-turbo",
            "speech-01-hd",
            "speech-01-turbo",
        }
    )
    _FORMATS: ClassVar[dict[str, str]] = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "flac": "audio/flac",
    }

    def __init__(
        self,
        *,
        api_base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        max_text_chars: int,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.model = model
        self.timeout = timeout
        self.max_text_chars = max_text_chars

    async def synthesize(
        self,
        request: AudioSynthesisRequest,
    ) -> GeneratedAudio:
        text = request.text.strip()
        if not 1 <= len(text) <= self.max_text_chars:
            raise AudioProviderError("AUDIO_TEXT_INVALID")
        model = request.model or self.model
        if model not in self._MODELS:
            raise AudioProviderError("AUDIO_MODEL_INVALID")
        if request.language is not None:
            raise AudioProviderError("AUDIO_SETTING_UNSUPPORTED")
        if request.voice_id and request.timbre_weights:
            raise AudioProviderError("AUDIO_VOICE_SELECTION_CONFLICT")
        if not request.voice_id and not request.timbre_weights:
            raise AudioProviderError("AUDIO_VOICE_INVALID")
        if request.voice_id and (
            not request.voice_id.strip() or len(request.voice_id) > 256
        ):
            raise AudioProviderError("AUDIO_VOICE_INVALID")
        validated_weights = (
            TimbreWeight.from_values(
                [item.as_payload() for item in request.timbre_weights]
            )
            if request.timbre_weights
            else ()
        )
        if not 0.5 <= request.speed <= 2.0 or not 0 <= request.volume <= 10:
            raise AudioProviderError("AUDIO_VOICE_SETTING_INVALID")
        if not -12 <= request.pitch <= 12 or request.sample_rate not in {
            16_000,
            22_050,
            24_000,
            32_000,
            44_100,
        }:
            raise AudioProviderError("AUDIO_SETTING_INVALID")
        if request.bitrate <= 0 or request.channel not in {1, 2}:
            raise AudioProviderError("AUDIO_SETTING_INVALID")
        audio_format = request.audio_format or "mp3"
        if audio_format not in self._FORMATS:
            raise AudioProviderError("AUDIO_FORMAT_INVALID")

        voice_setting: dict[str, Any] = {
            "voice_id": request.voice_id or "",
            "speed": request.speed,
            "vol": request.volume,
            "pitch": request.pitch,
        }
        if request.emotion:
            voice_setting["emotion"] = request.emotion
        payload: dict[str, Any] = {
            "model": model,
            "text": text,
            "stream": False,
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": request.sample_rate,
                "bitrate": request.bitrate,
                "format": audio_format,
                "channel": request.channel,
            },
            "output_format": "hex",
        }
        if request.language_boost:
            payload["language_boost"] = request.language_boost
        if validated_weights:
            payload["timbre_weights"] = [
                item.as_payload() for item in validated_weights
            ]
        if request.voice_modify is not None:
            payload["voice_modify"] = request.voice_modify.as_payload()

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{self.api_base_url}/t2a_v2",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise AudioProviderError("AUDIO_UNAVAILABLE", retryable=True) from exc
        if response.status_code == 429:
            raise AudioProviderError("AUDIO_RATE_LIMITED", retryable=True)
        if response.status_code in {401, 403}:
            raise AudioProviderError("AUDIO_UNAUTHORIZED")
        if response.status_code >= 400:
            raise AudioProviderError(
                "AUDIO_SYNTHESIS_FAILED",
                retryable=response.status_code >= 500,
            )

        try:
            payload = response.json()
            base_resp = payload.get("base_resp") or {}
            if int(base_resp.get("status_code", 0)) != 0:
                raise AudioProviderError("AUDIO_SYNTHESIS_FAILED")
            encoded = (payload.get("data") or {}).get("audio")
            if not isinstance(encoded, str) or not encoded:
                raise ValueError("audio is empty")
            data = bytes.fromhex(encoded)
        except AudioProviderError:
            raise
        except (TypeError, ValueError, UnicodeError) as exc:
            raise AudioProviderError("AUDIO_INVALID_RESPONSE") from exc
        if not data or len(data) > MAX_AUDIO_BYTES:
            raise AudioProviderError("AUDIO_INVALID_RESPONSE")
        if not self._matches_format(data, audio_format):
            raise AudioProviderError("AUDIO_FORMAT_UNSUPPORTED")
        return GeneratedAudio(
            data=data,
            content_type=self._FORMATS[audio_format],
            extension=audio_format,
        )

    @staticmethod
    def _matches_format(data: bytes, audio_format: str) -> bool:
        if audio_format == "mp3":
            return data.startswith(b"ID3") or (
                len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
            )
        if audio_format == "wav":
            return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"
        if audio_format == "flac":
            return data.startswith(b"fLaC")
        return False

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

import httpx


DEFAULT_VOICE_ID = "male-qn-qingse"


class AudioProviderError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class AudioTranscriptionError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class GeneratedAudio:
    data: bytes
    content_type: str
    extension: str


class VoiceEffect(StrEnum):
    SPACIOUS_ECHO = "spacious_echo"
    AUDITORIUM_ECHO = "auditorium_echo"
    LOFI_TELEPHONE = "lofi_telephone"
    ROBOTIC = "robotic"


@dataclass(frozen=True, slots=True)
class VoiceModification:
    pitch: int = 0
    intensity: int = 0
    timbre: int = 0
    sound_effects: VoiceEffect | None = None

    @classmethod
    def from_value(cls, value: object) -> VoiceModification | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise AudioProviderError("AUDIO_VOICE_MODIFY_INVALID")
        try:
            pitch = int(value.get("pitch", 0))
            intensity = int(value.get("intensity", 0))
            timbre = int(value.get("timbre", 0))
        except (TypeError, ValueError) as exc:
            raise AudioProviderError("AUDIO_VOICE_MODIFY_INVALID") from exc
        if any(not -100 <= item <= 100 for item in (pitch, intensity, timbre)):
            raise AudioProviderError("AUDIO_VOICE_MODIFY_INVALID")
        raw_effect = value.get("sound_effects")
        try:
            effect = VoiceEffect(str(raw_effect)) if raw_effect else None
        except ValueError as exc:
            raise AudioProviderError("AUDIO_VOICE_EFFECT_INVALID") from exc
        return cls(
            pitch=pitch,
            intensity=intensity,
            timbre=timbre,
            sound_effects=effect,
        )

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pitch": self.pitch,
            "intensity": self.intensity,
            "timbre": self.timbre,
        }
        if self.sound_effects is not None:
            payload["sound_effects"] = self.sound_effects.value
        return payload


@dataclass(frozen=True, slots=True)
class TimbreWeight:
    voice_id: str
    weight: int

    @classmethod
    def from_values(cls, value: object) -> tuple[TimbreWeight, ...]:
        if value is None:
            return ()
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes, bytearray))
            or not 2 <= len(value) <= 4
        ):
            raise AudioProviderError("AUDIO_TIMBRE_WEIGHTS_INVALID")
        result: list[TimbreWeight] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, Mapping):
                raise AudioProviderError("AUDIO_TIMBRE_WEIGHTS_INVALID")
            voice_id = str(item.get("voice_id", "")).strip()
            try:
                weight = int(item.get("weight", 0))
            except (TypeError, ValueError) as exc:
                raise AudioProviderError("AUDIO_TIMBRE_WEIGHTS_INVALID") from exc
            if (
                not voice_id
                or len(voice_id) > 256
                or voice_id in seen
                or not 1 <= weight <= 100
            ):
                raise AudioProviderError("AUDIO_TIMBRE_WEIGHTS_INVALID")
            seen.add(voice_id)
            result.append(cls(voice_id=voice_id, weight=weight))
        return tuple(result)

    def as_payload(self) -> dict[str, Any]:
        return {"voice_id": self.voice_id, "weight": self.weight}


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    voice_id: str | None = DEFAULT_VOICE_ID
    timbre_weights: tuple[TimbreWeight, ...] = ()
    speed: float = 1.0
    volume: float = 1.0
    pitch: int = 0
    emotion: str | None = None
    language_boost: str | None = None
    voice_modify: VoiceModification | None = None

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        base: VoiceProfile | None = None,
    ) -> VoiceProfile:
        current = base or cls()
        has_voice_id = "voice_id" in value
        has_weights = "timbre_weights" in value
        voice_id = (
            _optional_string(value.get("voice_id"))
            if has_voice_id
            else current.voice_id
        )
        timbre_weights = (
            TimbreWeight.from_values(value.get("timbre_weights"))
            if has_weights
            else current.timbre_weights
        )
        if has_voice_id and voice_id and not has_weights:
            timbre_weights = ()
        if has_weights and timbre_weights and not has_voice_id:
            voice_id = None
        if has_voice_id and voice_id and has_weights and timbre_weights:
            raise AudioProviderError("AUDIO_VOICE_SELECTION_CONFLICT")
        if not voice_id and not timbre_weights:
            raise AudioProviderError("AUDIO_VOICE_INVALID")
        if voice_id and (not voice_id.strip() or len(voice_id) > 256):
            raise AudioProviderError("AUDIO_VOICE_INVALID")
        try:
            speed = float(value.get("speed", current.speed))
            volume = float(value.get("volume", current.volume))
            pitch = int(value.get("pitch", current.pitch))
        except (TypeError, ValueError) as exc:
            raise AudioProviderError("AUDIO_VOICE_SETTING_INVALID") from exc
        if not 0.5 <= speed <= 2.0 or not 0 <= volume <= 10:
            raise AudioProviderError("AUDIO_VOICE_SETTING_INVALID")
        if not -12 <= pitch <= 12:
            raise AudioProviderError("AUDIO_VOICE_SETTING_INVALID")
        emotion = (
            _optional_string(value.get("emotion"))
            if "emotion" in value
            else current.emotion
        )
        language_boost = (
            _optional_string(value.get("language_boost"))
            if "language_boost" in value
            else current.language_boost
        )
        voice_modify = (
            VoiceModification.from_value(value.get("voice_modify"))
            if "voice_modify" in value
            else current.voice_modify
        )
        return cls(
            voice_id=voice_id,
            timbre_weights=timbre_weights,
            speed=speed,
            volume=volume,
            pitch=pitch,
            emotion=emotion,
            language_boost=language_boost,
            voice_modify=voice_modify,
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "voice_id": self.voice_id,
            "timbre_weights": [item.as_payload() for item in self.timbre_weights],
            "speed": self.speed,
            "volume": self.volume,
            "pitch": self.pitch,
            "emotion": self.emotion,
            "language_boost": self.language_boost,
            "voice_modify": (
                self.voice_modify.as_payload() if self.voice_modify else None
            ),
        }
        return result


class AudioTranscriptionProvider:
    """MiniMax ASR adapter for inbound voice messages."""

    def __init__(
        self,
        *,
        api_base_url: str | None,
        api_key: str | None,
        model: str,
        timeout: float,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/") if api_base_url else None
        self.api_key = api_key.strip() if api_key else None
        self.model = model
        self.timeout = timeout

    async def transcribe(
        self,
        content: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> str:
        if not self.api_base_url or not self.api_key or not self.model:
            raise AudioTranscriptionError("AUDIO_TRANSCRIPTION_NOT_CONFIGURED")
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


class AudioProvider:
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
        api_base_url: str | None,
        api_key: str | None,
        model: str,
        timeout: float,
        max_text_chars: int,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/") if api_base_url else None
        self.api_key = api_key.strip() if api_key else None
        self.model = model
        self.timeout = timeout
        self.max_text_chars = max_text_chars

    async def synthesize(
        self,
        text: str,
        *,
        model: str | None = None,
        voice_id: str | None = DEFAULT_VOICE_ID,
        timbre_weights: tuple[TimbreWeight, ...] = (),
        speed: float = 1.0,
        volume: float = 1.0,
        pitch: int = 0,
        emotion: str | None = None,
        sample_rate: int = 32_000,
        bitrate: int = 128_000,
        audio_format: str = "mp3",
        channel: int = 1,
        language_boost: str | None = None,
        voice_modify: VoiceModification | None = None,
    ) -> GeneratedAudio:
        text = text.strip()
        if not self.api_base_url or not self.api_key:
            raise AudioProviderError("AUDIO_NOT_CONFIGURED")
        if not 1 <= len(text) <= self.max_text_chars:
            raise AudioProviderError("AUDIO_TEXT_INVALID")
        model = model or self.model
        if model not in self._MODELS:
            raise AudioProviderError("AUDIO_MODEL_INVALID")
        if voice_id and timbre_weights:
            raise AudioProviderError("AUDIO_VOICE_SELECTION_CONFLICT")
        if not voice_id and not timbre_weights:
            raise AudioProviderError("AUDIO_VOICE_INVALID")
        if voice_id and (not voice_id.strip() or len(voice_id) > 256):
            raise AudioProviderError("AUDIO_VOICE_INVALID")
        validated_weights = (
            TimbreWeight.from_values([item.as_payload() for item in timbre_weights])
            if timbre_weights
            else ()
        )
        if not 0.5 <= speed <= 2.0 or not 0 <= volume <= 10:
            raise AudioProviderError("AUDIO_VOICE_SETTING_INVALID")
        if not -12 <= pitch <= 12 or sample_rate not in {
            16_000,
            22_050,
            24_000,
            32_000,
            44_100,
        }:
            raise AudioProviderError("AUDIO_SETTING_INVALID")
        if bitrate <= 0 or channel not in {1, 2}:
            raise AudioProviderError("AUDIO_SETTING_INVALID")
        if audio_format not in self._FORMATS:
            raise AudioProviderError("AUDIO_FORMAT_INVALID")

        voice_setting: dict[str, Any] = {
            "voice_id": voice_id or "",
            "speed": speed,
            "vol": volume,
            "pitch": pitch,
        }
        if emotion:
            voice_setting["emotion"] = emotion
        payload: dict[str, Any] = {
            "model": model,
            "text": text,
            "stream": False,
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": sample_rate,
                "bitrate": bitrate,
                "format": audio_format,
                "channel": channel,
            },
            "output_format": "hex",
        }
        if language_boost:
            payload["language_boost"] = language_boost
        if validated_weights:
            payload["timbre_weights"] = [
                item.as_payload() for item in validated_weights
            ]
        if voice_modify is not None:
            payload["voice_modify"] = voice_modify.as_payload()

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
        if not data or len(data) > 20 * 1024 * 1024:
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


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from core.config import AudioProviderKind, QwenAudioLanguage


DEFAULT_VOICE_ID = "male-qn-qingse"
MAX_AUDIO_BYTES = 20 * 1024 * 1024


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


@dataclass(frozen=True, slots=True)
class AudioProviderCapabilities:
    provider_kind: AudioProviderKind
    formats: tuple[str, ...]
    supports_voice_clone: bool = False


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
        if value is None or value == [] or value == ():
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
        return {
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


@dataclass(frozen=True, slots=True)
class QwenVoiceProfile:
    voice_id: str
    language: QwenAudioLanguage = QwenAudioLanguage.CHINESE

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        base: QwenVoiceProfile,
    ) -> QwenVoiceProfile:
        voice_id = _optional_string(value.get("voice_id", base.voice_id))
        if (
            voice_id is None
            or len(voice_id) > 64
            or not voice_id.replace("_", "").replace("-", "").isalnum()
        ):
            raise AudioProviderError("AUDIO_VOICE_INVALID")
        try:
            language = QwenAudioLanguage(
                str(value.get("language", base.language.value))
            )
        except ValueError as exc:
            raise AudioProviderError("AUDIO_LANGUAGE_INVALID") from exc
        return cls(voice_id=voice_id, language=language)

    def as_dict(self) -> dict[str, Any]:
        return {
            "voice_id": self.voice_id,
            "language": self.language.value,
        }


AudioVoiceProfile = VoiceProfile | QwenVoiceProfile


@dataclass(frozen=True, slots=True)
class AudioSynthesisRequest:
    text: str
    model: str | None = None
    voice_id: str | None = DEFAULT_VOICE_ID
    timbre_weights: tuple[TimbreWeight, ...] = ()
    speed: float = 1.0
    volume: float = 1.0
    pitch: int = 0
    emotion: str | None = None
    sample_rate: int = 32_000
    bitrate: int = 128_000
    audio_format: str | None = None
    channel: int = 1
    language_boost: str | None = None
    voice_modify: VoiceModification | None = None
    language: QwenAudioLanguage | None = None


@dataclass(frozen=True, slots=True)
class VoiceCloneRequest:
    voice_id: str
    reference_audio: bytes
    reference_content_type: str
    reference_text: str | None
    x_vector_only_mode: bool
    consent_confirmed: bool


@dataclass(frozen=True, slots=True)
class VoiceSummary:
    voice_id: str
    created_at: str
    mode: str

    def as_dict(self) -> dict[str, str]:
        return {
            "voice_id": self.voice_id,
            "created_at": self.created_at,
            "mode": self.mode,
        }


class AudioSynthesisProvider(Protocol):
    capabilities: AudioProviderCapabilities

    async def synthesize(
        self,
        request: AudioSynthesisRequest,
    ) -> GeneratedAudio: ...


class AudioTranscriptionProvider(Protocol):
    async def transcribe(
        self,
        content: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> str: ...


class VoiceCloneProvider(Protocol):
    async def create_voice(self, request: VoiceCloneRequest) -> VoiceSummary: ...


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None

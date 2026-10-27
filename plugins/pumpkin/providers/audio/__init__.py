from core.config import QwenAudioLanguage

from .contracts import (
    MAX_AUDIO_BYTES,
    AudioProviderCapabilities,
    AudioProviderError,
    AudioSynthesisProvider,
    AudioSynthesisRequest,
    AudioTranscriptionError,
    AudioTranscriptionProvider,
    GeneratedAudio,
    QwenVoiceProfile,
    TimbreWeight,
    VoiceCloneProvider,
    VoiceCloneRequest,
    VoiceEffect,
    VoiceModification,
    VoiceProfile,
    VoiceSummary,
)
from .minimax import MinimaxAudioProvider, MinimaxAudioTranscriptionProvider
from .profile_store import VoiceProfileStore, VoiceProfileStoreError
from .qwen import QwenLocalAudioProvider

__all__ = [
    "MAX_AUDIO_BYTES",
    "AudioProviderCapabilities",
    "AudioProviderError",
    "AudioSynthesisProvider",
    "AudioSynthesisRequest",
    "AudioTranscriptionError",
    "AudioTranscriptionProvider",
    "GeneratedAudio",
    "MinimaxAudioProvider",
    "MinimaxAudioTranscriptionProvider",
    "QwenAudioLanguage",
    "QwenLocalAudioProvider",
    "QwenVoiceProfile",
    "TimbreWeight",
    "VoiceCloneProvider",
    "VoiceCloneRequest",
    "VoiceEffect",
    "VoiceModification",
    "VoiceProfile",
    "VoiceProfileStore",
    "VoiceProfileStoreError",
    "VoiceSummary",
]

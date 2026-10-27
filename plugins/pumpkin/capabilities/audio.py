from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, cast

from core.config import (
    AudioConfig,
    AudioProviderKind,
    PumpkinConfig,
    QwenAudioLanguage,
)
from core.domain import AgentRole, MessageSegmentType, ToolContext, ToolResult
from core.plugins import PluginContext, ServiceKey
from core.tools import Tool, ToolRegistry
from providers.audio import (
    AudioProviderError,
    AudioSynthesisProvider,
    AudioSynthesisRequest,
    AudioTranscriptionProvider,
    MinimaxAudioProvider,
    MinimaxAudioTranscriptionProvider,
    QwenVoiceProfile,
    QwenLocalAudioProvider,
    VoiceCloneProvider,
    VoiceCloneRequest,
    VoiceEffect,
    VoiceProfile,
    VoiceProfileStore,
    VoiceProfileStoreError,
)
from providers.media import MediaProviderError, MessageMediaProvider

from .base import CapabilityHost, error, result, task_error


log = logging.getLogger(__name__)

_CLONE_ARGUMENTS = frozenset(
    {
        "message_id",
        "segment_index",
        "segment_path",
        "voice_id",
        "reference_text",
        "x_vector_only_mode",
        "consent_confirmed",
        "idempotency_key",
    }
)


class AudioCapabilityPlugin:
    name = "audio"
    requires = frozenset(
        {
            ServiceKey.CONFIG,
            ServiceKey.TOOL_REGISTRY,
            ServiceKey.CAPABILITY_HOST,
            ServiceKey.MEDIA_PROVIDER,
        }
    )

    def __init__(self) -> None:
        self.provider: AudioSynthesisProvider | None = None
        self.transcriber: AudioTranscriptionProvider | None = None
        self.profile_store: VoiceProfileStore | None = None
        self.media_provider: MessageMediaProvider | None = None
        self.config: AudioConfig | None = None

    def setup(self, context: PluginContext) -> None:
        config = cast(PumpkinConfig, context.require(ServiceKey.CONFIG))
        registry = cast(ToolRegistry, context.require(ServiceKey.TOOL_REGISTRY))
        host = cast(
            CapabilityHost,
            context.require(ServiceKey.CAPABILITY_HOST),
        )
        self.config = config.audio
        self.provider = _build_provider(config.audio)
        self.transcriber = _build_transcriber(config.audio)
        self.media_provider = cast(
            MessageMediaProvider,
            context.require(ServiceKey.MEDIA_PROVIDER),
        )
        self.profile_store = VoiceProfileStore(
            Path(config.data_dir) / "voice_profile.json"
        )
        context.provide(ServiceKey.AUDIO_PROVIDER, self.provider)
        context.provide(ServiceKey.AUDIO_TRANSCRIBER, self.transcriber)
        context.effect(
            registry.register(
                Tool(
                    name="generate_audio",
                    description=(
                        "根据文本合成音频，并登记为当前任务的已验证 Audio Artifact。"
                    ),
                    input_schema=_generate_schema(),
                    handler=lambda tool_context, arguments: self.generate(
                        host,
                        tool_context,
                        arguments,
                    ),
                    skill_name="audio-render",
                    read_only=False,
                    timeout=135,
                    idempotency_required=True,
                    allowed_roles=frozenset({AgentRole.TASK}),
                )
            )
        )
        context.effect(
            registry.register(
                Tool(
                    name="clone_voice",
                    description=(
                        "使用当前任务源消息中的授权参考音频创建音色。"
                        "成功后自动将其设为后续合成使用的最新音色。"
                    ),
                    input_schema=_clone_voice_schema(),
                    handler=lambda tool_context, arguments: self.clone_voice(
                        host,
                        tool_context,
                        arguments,
                    ),
                    skill_name="audio-render",
                    read_only=False,
                    timeout=180,
                    idempotency_required=True,
                    allowed_roles=frozenset({AgentRole.TASK}),
                )
            )
        )

    async def generate(
        self,
        host: CapabilityHost,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        if self.provider is None or self.config is None:
            return error("音频生成未配置", "AUDIO_NOT_CONFIGURED")
        if self.profile_store is None:
            return error("默认音色存储未配置", "VOICE_PROFILE_NOT_CONFIGURED")
        if invalid := task_error(host, context):
            return invalid
        try:
            request = await self._synthesis_request(arguments)
        except AudioProviderError as exc:
            return error("音色参数无效。", exc.code)
        except VoiceProfileStoreError as exc:
            return error("默认音色配置无法读取。", exc.code)
        key = host.idempotency_key(
            context,
            "audio",
            str(arguments["idempotency_key"]),
        )
        reserved, cached = await host.store.idempotency_reserve(key)
        if not reserved and cached.get("_status") == "in_progress":
            return error(
                "音频生成结果未知，禁止自动重复调用",
                "AUDIO_OUTCOME_UNKNOWN",
            )
        if not reserved:
            artifacts = await host.store.get_artifacts(
                task_id=context.task_id or "",
                revision=context.revision or 0,
                scope_id=context.scope_id,
                artifact_ids=(str(cached["artifact_id"]),),
            )
            return result(cached, artifacts=artifacts, suppress_final_text=True)
        try:
            audio = await self.provider.synthesize(request)
            artifact = await host.write_artifact(
                context,
                filename=f"generated-{uuid.uuid4().hex[:12]}.{audio.extension}",
                kind="audio",
                content=audio.data,
                strategy_ref={
                    "tool": "generate_audio",
                    "provider": self.provider.capabilities.provider_kind.value,
                    "arguments": {
                        key: value
                        for key, value in arguments.items()
                        if key != "idempotency_key"
                    },
                },
            )
        except AudioProviderError as exc:
            await host.store.idempotency_release(key)
            return error(
                {
                    "message": "音频生成未配置或暂时失败，请稍后重试。",
                    "reason_code": exc.code,
                },
                exc.code,
                retryable=exc.retryable,
            )
        except (OSError, ValueError) as exc:
            await host.store.idempotency_release(key)
            return error(str(exc), "ARTIFACT_WRITE_FAILED")
        payload = {
            "artifact_id": artifact.artifact_id,
            "name": artifact.name,
            "size_bytes": artifact.size_bytes,
            "format": audio.extension,
        }
        await host.store.idempotency_put(key, payload)
        return result(payload, artifacts=(artifact,), suppress_final_text=True)

    async def clone_voice(
        self,
        host: CapabilityHost,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        if (
            self.provider is None
            or not self.provider.capabilities.supports_voice_clone
            or self.media_provider is None
            or self.profile_store is None
            or self.config is None
        ):
            return error(
                "当前语音 Provider 不支持音色克隆。", "VOICE_CLONE_UNSUPPORTED"
            )
        if set(arguments) - _CLONE_ARGUMENTS:
            return error("音色克隆参数无效。", "VOICE_CLONE_INPUT_INVALID")
        if not arguments.get("consent_confirmed"):
            return error(
                "未确认已获得说话人授权，不能克隆音色。",
                "VOICE_CLONE_CONSENT_REQUIRED",
            )
        try:
            handoff = host.capability_task(context)
        except ValueError as exc:
            return error(str(exc), "TASK_CONTEXT_MISSING")
        message_id = str(arguments.get("message_id") or "").strip()
        record = next(
            (
                item
                for item in handoff.source_messages
                if item.message.message_id == message_id
            ),
            None,
        )
        if record is None:
            return error(
                "当前任务没有权限读取这条参考音频。",
                "MEDIA_MESSAGE_NOT_AUTHORIZED",
            )
        try:
            segment_path = _segment_path(arguments)
            segment = MessageMediaProvider.resolve_segment(record, segment_path)
        except (MediaProviderError, ValueError) as exc:
            code = exc.code if isinstance(exc, MediaProviderError) else str(exc)
            return error("参考音频位置无效。", code)
        if segment.type not in {MessageSegmentType.RECORD, MessageSegmentType.FILE}:
            return error("指定消息段不是音频。", "MEDIA_SEGMENT_NOT_AUDIO")
        key = host.idempotency_key(
            context,
            "voice-clone",
            str(arguments["idempotency_key"]),
        )
        reserved, cached = await host.store.idempotency_reserve(key)
        if not reserved and cached.get("_status") == "in_progress":
            return error(
                "音色克隆结果未知，禁止自动重复创建。",
                "VOICE_CLONE_OUTCOME_UNKNOWN",
            )
        if not reserved:
            if cached.get("partial"):
                return result(
                    cached,
                    is_error=True,
                    error_code="VOICE_PROFILE_WRITE_FAILED",
                )
            return result(cached)
        try:
            fetched = await self.media_provider.fetch(
                segment.type.value,
                segment.data,
            )
        except MediaProviderError as exc:
            await host.store.idempotency_release(key)
            return error(
                "读取参考音频失败，请稍后重试。",
                exc.code,
                retryable=exc.retryable,
            )
        if not fetched.content_type.startswith("audio/"):
            await host.store.idempotency_release(key)
            return error("指定文件不是可识别的音频。", "MEDIA_SEGMENT_NOT_AUDIO")
        clone_provider = cast(VoiceCloneProvider, self.provider)
        try:
            summary = await clone_provider.create_voice(
                VoiceCloneRequest(
                    voice_id=str(arguments.get("voice_id") or "").strip(),
                    reference_audio=fetched.content,
                    reference_content_type=fetched.content_type,
                    reference_text=_optional_string(arguments.get("reference_text")),
                    x_vector_only_mode=bool(arguments.get("x_vector_only_mode", False)),
                    consent_confirmed=True,
                )
            )
        except AudioProviderError as exc:
            if not exc.retryable:
                await host.store.idempotency_release(key)
            return error(
                "音色克隆失败。",
                exc.code if not exc.retryable else "VOICE_CLONE_OUTCOME_UNKNOWN",
                retryable=False,
            )
        payload: dict[str, Any] = {
            "created": True,
            "voice": summary.as_dict(),
            "activated": False,
        }
        try:
            profile = QwenVoiceProfile(
                voice_id=summary.voice_id,
                language=self.config.language,
            )
            await self.profile_store.save(
                profile,
                AudioProviderKind.QWEN_LOCAL,
            )
            payload["activated"] = True
        except VoiceProfileStoreError:
            payload["partial"] = True
            payload["reason_code"] = "VOICE_PROFILE_WRITE_FAILED"
            await host.store.idempotency_put(key, payload)
            return result(
                payload,
                is_error=True,
                error_code="VOICE_PROFILE_WRITE_FAILED",
            )
        await host.store.idempotency_put(key, payload)
        return result(payload)

    async def _synthesis_request(
        self,
        arguments: dict[str, Any],
    ) -> AudioSynthesisRequest:
        assert self.config is not None
        assert self.profile_store is not None
        if self.config.provider is AudioProviderKind.QWEN_LOCAL:
            if not self.config.default_voice_id:
                raise AudioProviderError("AUDIO_VOICE_INVALID")
            default = QwenVoiceProfile(
                voice_id=self.config.default_voice_id,
                language=self.config.language,
            )
            qwen_profile = cast(
                QwenVoiceProfile,
                await self.profile_store.load(
                    AudioProviderKind.QWEN_LOCAL,
                    default=default,
                ),
            )
            qwen_profile = QwenVoiceProfile.from_mapping(
                arguments,
                base=qwen_profile,
            )
            base_voice_id = qwen_profile.voice_id
            language = qwen_profile.language
            default_format = "wav"
        else:
            base_voice_id = self.config.default_voice_id
            language = _optional_language(arguments.get("language"))
            default_format = "mp3"
        profile = VoiceProfile.from_mapping(
            arguments,
            base=VoiceProfile(voice_id=base_voice_id or VoiceProfile().voice_id),
        )
        return AudioSynthesisRequest(
            text=str(arguments["text"]),
            model=_optional_string(arguments.get("model")),
            voice_id=profile.voice_id,
            timbre_weights=profile.timbre_weights,
            speed=profile.speed,
            volume=profile.volume,
            pitch=profile.pitch,
            emotion=profile.emotion,
            sample_rate=int(arguments.get("sample_rate", 32_000)),
            bitrate=int(arguments.get("bitrate", 128_000)),
            audio_format=str(arguments.get("format", default_format)),
            channel=int(arguments.get("channel", 1)),
            language_boost=profile.language_boost,
            voice_modify=profile.voice_modify,
            language=language,
        )


def _build_provider(config: AudioConfig) -> AudioSynthesisProvider | None:
    if not config.is_configured or config.provider is None:
        return None
    if config.provider is AudioProviderKind.MINIMAX:
        assert config.api_base_url and config.api_key and config.model
        return MinimaxAudioProvider(
            api_base_url=config.api_base_url,
            api_key=config.api_key,
            model=config.model,
            timeout=config.timeout_seconds,
            max_text_chars=config.max_text_chars,
        )
    if config.provider is AudioProviderKind.QWEN_LOCAL:
        assert config.api_base_url
        return QwenLocalAudioProvider(
            api_base_url=config.api_base_url,
            timeout=config.timeout_seconds,
            max_text_chars=config.max_text_chars,
        )
    log.warning("audio provider is not supported: %s", config.provider)
    return None


def _build_transcriber(config: AudioConfig) -> AudioTranscriptionProvider | None:
    if not config.transcription_is_configured:
        return None
    if config.transcription_provider != "minimax":
        log.warning(
            "audio transcription provider is not supported: %s",
            config.transcription_provider,
        )
        return None
    assert config.transcription_base_url
    assert config.transcription_api_key
    assert config.transcription_model
    return MinimaxAudioTranscriptionProvider(
        api_base_url=config.transcription_base_url,
        api_key=config.transcription_api_key,
        model=config.transcription_model,
        timeout=config.timeout_seconds,
    )


def _voice_properties() -> dict[str, Any]:
    return {
        "voice_id": {"type": ["string", "null"]},
        "timbre_weights": {
            "type": "array",
            "minItems": 2,
            "maxItems": 4,
            "items": {
                "type": "object",
                "required": ["voice_id", "weight"],
                "properties": {
                    "voice_id": {"type": "string"},
                    "weight": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "additionalProperties": False,
            },
        },
        "speed": {"type": "number", "minimum": 0.5, "maximum": 2.0},
        "volume": {"type": "number", "minimum": 0, "maximum": 10},
        "pitch": {"type": "integer", "minimum": -12, "maximum": 12},
        "emotion": {"type": ["string", "null"]},
        "language_boost": {"type": ["string", "null"]},
        "voice_modify": {
            "type": ["object", "null"],
            "properties": {
                "pitch": {"type": "integer", "minimum": -100, "maximum": 100},
                "intensity": {"type": "integer", "minimum": -100, "maximum": 100},
                "timbre": {"type": "integer", "minimum": -100, "maximum": 100},
                "sound_effects": {
                    "type": "string",
                    "enum": [item.value for item in VoiceEffect],
                },
            },
            "additionalProperties": False,
        },
    }


def _generate_schema() -> dict[str, Any]:
    properties = {
        "text": {"type": "string"},
        "idempotency_key": {"type": "string"},
        "model": {"type": "string"},
        "sample_rate": {"type": "integer"},
        "bitrate": {"type": "integer"},
        "format": {"type": "string"},
        "channel": {"type": "integer"},
        "language": {
            "type": "string",
            "enum": [item.value for item in QwenAudioLanguage],
        },
        **_voice_properties(),
    }
    return {
        "type": "object",
        "required": ["text", "idempotency_key"],
        "properties": properties,
        "additionalProperties": False,
    }


def _clone_voice_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "message_id",
            "voice_id",
            "consent_confirmed",
            "idempotency_key",
        ],
        "properties": {
            "message_id": {"type": "string"},
            "segment_index": {"type": "integer"},
            "segment_path": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
            },
            "voice_id": {"type": "string"},
            "reference_text": {"type": "string"},
            "x_vector_only_mode": {"type": "boolean"},
            "consent_confirmed": {"type": "boolean"},
            "idempotency_key": {"type": "string"},
        },
        "additionalProperties": False,
    }


def _segment_path(arguments: dict[str, Any]) -> tuple[int, ...]:
    raw_path = arguments.get("segment_path")
    segment_index = arguments.get("segment_index")
    if raw_path is None:
        if isinstance(segment_index, bool) or not isinstance(segment_index, int):
            raise ValueError("MEDIA_ARGUMENT_INVALID")
        return (segment_index,)
    if (
        not isinstance(raw_path, list)
        or not raw_path
        or any(isinstance(item, bool) or not isinstance(item, int) for item in raw_path)
        or (segment_index is not None and segment_index != raw_path[0])
        or (len(raw_path) - 1) % 2 != 0
    ):
        raise ValueError("MEDIA_ARGUMENT_INVALID")
    return tuple(raw_path)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _optional_language(value: object) -> QwenAudioLanguage | None:
    if value is None:
        return None
    try:
        return QwenAudioLanguage(str(value))
    except ValueError as exc:
        raise AudioProviderError("AUDIO_LANGUAGE_INVALID") from exc

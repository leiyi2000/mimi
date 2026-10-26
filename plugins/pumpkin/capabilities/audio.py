from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, cast

from core.config import AudioConfig, PumpkinConfig
from core.domain import AgentRole, ToolContext, ToolResult
from core.plugins import PluginContext, ServiceKey
from core.tools import Tool, ToolRegistry
from providers.audio import (
    AudioProvider,
    AudioProviderError,
    AudioTranscriptionProvider,
    VoiceProfile,
)
from providers.voice_profile import VoiceProfileStore, VoiceProfileStoreError

from .base import CapabilityHost, error, result, task_error


log = logging.getLogger(__name__)


class AudioCapabilityPlugin:
    name = "audio"
    requires = frozenset(
        {
            ServiceKey.CONFIG,
            ServiceKey.TOOL_REGISTRY,
            ServiceKey.CAPABILITY_HOST,
        }
    )

    def __init__(self) -> None:
        self.provider: AudioProvider | None = None
        self.transcriber: AudioTranscriptionProvider | None = None
        self.profile_store: VoiceProfileStore | None = None

    def setup(self, context: PluginContext) -> None:
        config = cast(PumpkinConfig, context.require(ServiceKey.CONFIG))
        registry = cast(
            ToolRegistry,
            context.require(ServiceKey.TOOL_REGISTRY),
        )
        host = cast(
            CapabilityHost,
            context.require(ServiceKey.CAPABILITY_HOST),
        )
        self.provider = _build_provider(config.audio)
        self.transcriber = _build_transcriber(config.audio)
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
                    name="save_default_voice",
                    description=(
                        "将一套完整音色参数原子保存为全局默认音色。"
                        "仅在用户明确要求以后默认使用该音色时调用。"
                    ),
                    input_schema=_save_profile_schema(),
                    handler=lambda tool_context, arguments: self.save_default(
                        host,
                        tool_context,
                        arguments,
                    ),
                    skill_name="audio-render",
                    read_only=False,
                    timeout=30,
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
        if self.provider is None:
            return error("音频生成未配置", "AUDIO_NOT_CONFIGURED")
        if self.profile_store is None:
            return error("默认音色存储未配置", "VOICE_PROFILE_NOT_CONFIGURED")
        if invalid := task_error(host, context):
            return invalid
        try:
            profile = VoiceProfile.from_mapping(
                arguments,
                base=await self.profile_store.load(),
            )
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
            return result(
                cached,
                artifacts=artifacts,
                suppress_final_text=True,
            )
        try:
            audio = await self.provider.synthesize(
                str(arguments["text"]),
                model=arguments.get("model"),
                voice_id=profile.voice_id,
                timbre_weights=profile.timbre_weights,
                speed=profile.speed,
                volume=profile.volume,
                pitch=profile.pitch,
                emotion=profile.emotion,
                sample_rate=int(arguments.get("sample_rate", 32_000)),
                bitrate=int(arguments.get("bitrate", 128_000)),
                audio_format=str(arguments.get("format", "mp3")),
                channel=int(arguments.get("channel", 1)),
                language_boost=profile.language_boost,
                voice_modify=profile.voice_modify,
            )
            artifact = await host.write_artifact(
                context,
                filename=f"generated-{uuid.uuid4().hex[:12]}.{audio.extension}",
                kind="audio",
                content=audio.data,
                strategy_ref={
                    "tool": "generate_audio",
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
        return result(
            payload,
            artifacts=(artifact,),
            suppress_final_text=True,
        )

    async def save_default(
        self,
        host: CapabilityHost,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        if self.profile_store is None:
            return error("默认音色存储未配置", "VOICE_PROFILE_NOT_CONFIGURED")
        if invalid := task_error(host, context):
            return invalid
        key = host.idempotency_key(
            context,
            "voice-profile",
            str(arguments["idempotency_key"]),
        )
        reserved, cached = await host.store.idempotency_reserve(key)
        if not reserved and cached.get("_status") == "in_progress":
            return error(
                "默认音色保存结果未知，禁止自动重复调用",
                "VOICE_PROFILE_OUTCOME_UNKNOWN",
            )
        if not reserved:
            return result(cached)
        try:
            current = await self.profile_store.load()
            profile = VoiceProfile.from_mapping(arguments, base=current)
            await self.profile_store.save(profile)
        except AudioProviderError as exc:
            await host.store.idempotency_release(key)
            return error("音色参数无效。", exc.code)
        except VoiceProfileStoreError as exc:
            await host.store.idempotency_release(key)
            return error("默认音色保存失败。", exc.code)
        payload = {"saved": True, "profile": profile.as_dict()}
        await host.store.idempotency_put(key, payload)
        return result(payload)


def _build_provider(config: AudioConfig) -> AudioProvider | None:
    if not config.is_configured:
        return None
    if config.provider != "minimax":
        log.warning("audio provider is not supported: %s", config.provider)
        return None
    return AudioProvider(
        api_base_url=config.api_base_url,
        api_key=config.api_key,
        model=config.model,
        timeout=config.timeout_seconds,
        max_text_chars=config.max_text_chars,
    )


def _build_transcriber(
    config: AudioConfig,
) -> AudioTranscriptionProvider | None:
    if not config.transcription_is_configured:
        return None
    if config.transcription_provider != "minimax":
        log.warning(
            "audio transcription provider is not supported: %s",
            config.transcription_provider,
        )
        return None
    return AudioTranscriptionProvider(
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
                "intensity": {
                    "type": "integer",
                    "minimum": -100,
                    "maximum": 100,
                },
                "timbre": {"type": "integer", "minimum": -100, "maximum": 100},
                "sound_effects": {
                    "type": "string",
                    "enum": [
                        "spacious_echo",
                        "auditorium_echo",
                        "lofi_telephone",
                        "robotic",
                    ],
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
        **_voice_properties(),
    }
    return {
        "type": "object",
        "required": ["text", "idempotency_key"],
        "properties": properties,
    }


def _save_profile_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["idempotency_key"],
        "properties": {
            "idempotency_key": {"type": "string"},
            **_voice_properties(),
        },
    }

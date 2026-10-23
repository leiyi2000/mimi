from __future__ import annotations

import logging
import uuid
from typing import Any, cast

from core.config import AudioConfig, PumpkinConfig
from core.domain import AgentRole, ToolContext, ToolResult
from core.plugins import PluginContext, ServiceKey
from core.tools import Tool, ToolRegistry
from providers.audio import (
    AudioProvider,
    AudioProviderError,
    AudioTranscriptionProvider,
)

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
        context.provide(ServiceKey.AUDIO_PROVIDER, self.provider)
        context.provide(ServiceKey.AUDIO_TRANSCRIBER, self.transcriber)
        context.effect(
            registry.register(
                Tool(
                    name="generate_audio",
                    description=(
                        "根据文本合成音频，"
                        "并登记为当前任务的已验证 Audio Artifact。"
                    ),
                    input_schema={
                        "type": "object",
                        "required": ["text", "idempotency_key"],
                        "properties": {
                            "text": {"type": "string"},
                            "idempotency_key": {"type": "string"},
                            "model": {"type": "string"},
                            "voice_id": {"type": "string"},
                            "speed": {"type": "number"},
                            "volume": {"type": "number"},
                            "pitch": {"type": "integer"},
                            "emotion": {"type": "string"},
                            "sample_rate": {"type": "integer"},
                            "bitrate": {"type": "integer"},
                            "format": {"type": "string"},
                            "channel": {"type": "integer"},
                            "language_boost": {"type": "string"},
                        },
                    },
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

    async def generate(
        self,
        host: CapabilityHost,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        if self.provider is None:
            return error("音频生成未配置", "AUDIO_NOT_CONFIGURED")
        if invalid := task_error(host, context):
            return invalid
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
                voice_id=str(arguments.get("voice_id", "male-qn-qingse")),
                speed=float(arguments.get("speed", 1.0)),
                volume=float(arguments.get("volume", 1.0)),
                pitch=int(arguments.get("pitch", 0)),
                emotion=arguments.get("emotion"),
                sample_rate=int(arguments.get("sample_rate", 32_000)),
                bitrate=int(arguments.get("bitrate", 128_000)),
                audio_format=str(arguments.get("format", "mp3")),
                channel=int(arguments.get("channel", 1)),
                language_boost=arguments.get("language_boost"),
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

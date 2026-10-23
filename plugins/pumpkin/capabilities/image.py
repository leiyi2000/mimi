from __future__ import annotations

import logging
import uuid
from typing import Any, cast

from core.config import ImageConfig, PumpkinConfig
from core.domain import AgentRole, ToolContext, ToolResult
from core.plugins import PluginContext, ServiceKey
from core.tools import Tool, ToolRegistry
from providers.image import ImageProvider, ImageProviderError

from .base import CapabilityHost, error, result, task_error


log = logging.getLogger(__name__)


class ImageCapabilityPlugin:
    name = "image"
    requires = frozenset(
        {
            ServiceKey.CONFIG,
            ServiceKey.TOOL_REGISTRY,
            ServiceKey.CAPABILITY_HOST,
        }
    )

    def __init__(self) -> None:
        self.provider: ImageProvider | None = None

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
        self.provider = _build_provider(config.image)
        context.provide(ServiceKey.IMAGE_PROVIDER, self.provider)
        context.effect(
            registry.register(
                Tool(
                    name="generate_image",
                    description=(
                        "根据文字描述或人物主体参考图生成图片，"
                        "并登记为当前任务的已验证 Artifact。"
                    ),
                    input_schema={
                        "type": "object",
                        "required": ["prompt", "idempotency_key"],
                        "properties": {
                            "prompt": {"type": "string"},
                            "idempotency_key": {"type": "string"},
                            "aspect_ratio": {"type": "string"},
                            "model": {"type": "string"},
                            "width": {"type": "integer"},
                            "height": {"type": "integer"},
                            "n": {"type": "integer"},
                            "prompt_optimizer": {"type": "boolean"},
                            "aigc_watermark": {"type": "boolean"},
                            "style": {"type": "object"},
                            "subject_reference": {"type": "array"},
                            "seed": {"type": "integer"},
                        },
                    },
                    handler=lambda tool_context, arguments: self.generate(
                        host,
                        tool_context,
                        arguments,
                    ),
                    skill_name="image-render",
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
            return error("图片生成未配置", "IMAGE_NOT_CONFIGURED")
        if invalid := task_error(host, context):
            return invalid
        key = host.idempotency_key(
            context,
            "image",
            str(arguments["idempotency_key"]),
        )
        reserved, cached = await host.store.idempotency_reserve(key)
        if not reserved and cached.get("_status") == "in_progress":
            return error(
                "图片生成结果未知，禁止自动重复调用",
                "IMAGE_OUTCOME_UNKNOWN",
            )
        if not reserved:
            artifacts = await host.store.get_artifacts(
                task_id=context.task_id or "",
                revision=context.revision or 0,
                scope_id=context.scope_id,
                artifact_ids=tuple(
                    str(item["artifact_id"])
                    for item in cached.get("artifacts", [])
                ),
            )
            return result(
                cached,
                artifacts=artifacts,
                suppress_final_text=True,
            )
        try:
            images = await self.provider.generate(
                str(arguments["prompt"]),
                arguments.get("aspect_ratio"),
                model=arguments.get("model"),
                width=arguments.get("width"),
                height=arguments.get("height"),
                n=int(arguments.get("n", 1)),
                prompt_optimizer=bool(arguments.get("prompt_optimizer", False)),
                aigc_watermark=bool(arguments.get("aigc_watermark", False)),
                style=arguments.get("style"),
                subject_reference=arguments.get("subject_reference"),
                seed=arguments.get("seed"),
            )
            artifacts = tuple(
                [
                    await host.write_artifact(
                        context,
                        filename=(
                            f"generated-{uuid.uuid4().hex[:12]}.{image.extension}"
                        ),
                        kind="image",
                        content=image.data,
                        strategy_ref={
                            "tool": "generate_image",
                            "arguments": {
                                key: value
                                for key, value in arguments.items()
                                if key != "idempotency_key"
                            },
                        },
                    )
                    for image in images
                ]
            )
        except ImageProviderError as exc:
            await host.store.idempotency_release(key)
            return error(
                {
                    "message": "图片生成未配置或暂时失败，请稍后重试。",
                    "reason_code": exc.code,
                },
                exc.code,
                retryable=exc.retryable,
            )
        except (OSError, ValueError) as exc:
            await host.store.idempotency_release(key)
            return error(str(exc), "ARTIFACT_WRITE_FAILED")
        artifact_payload = [
            {
                "artifact_id": artifact.artifact_id,
                "name": artifact.name,
                "size_bytes": artifact.size_bytes,
            }
            for artifact in artifacts
        ]
        payload = {
            "artifact_id": artifact_payload[0]["artifact_id"],
            "name": artifact_payload[0]["name"],
            "size_bytes": artifact_payload[0]["size_bytes"],
            "artifacts": artifact_payload,
        }
        await host.store.idempotency_put(key, payload)
        return result(
            payload,
            artifacts=artifacts,
            suppress_final_text=True,
        )


def _build_provider(config: ImageConfig) -> ImageProvider | None:
    if not config.is_configured:
        return None
    if config.provider != "minimax":
        log.warning("image provider is not supported: %s", config.provider)
        return None
    return ImageProvider(
        api_base_url=config.api_base_url,
        api_key=config.api_key,
        model=config.model,
        timeout=config.timeout_seconds,
        max_prompt_chars=config.max_prompt_chars,
    )

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

from core.domain import AgentRole, ToolContext, ToolResult
from core.plugins import PluginContext, ServiceKey
from core.tools import Tool, ToolRegistry
from providers.html import (
    DEFAULT_HTML_RENDER_DEVICE_PIXEL_RATIO,
    DEFAULT_HTML_RENDER_WIDTH,
    HtmlRenderError,
    HtmlRenderer,
)

from .base import CapabilityHost, error, result, task_error


class HtmlCapabilityPlugin:
    name = "html"
    requires = frozenset(
        {
            ServiceKey.CONFIG,
            ServiceKey.TOOL_REGISTRY,
            ServiceKey.CAPABILITY_HOST,
        }
    )

    def __init__(self, renderer: HtmlRenderer | None = None) -> None:
        self.renderer = renderer or HtmlRenderer()

    def setup(self, context: PluginContext) -> None:
        registry = cast(
            ToolRegistry,
            context.require(ServiceKey.TOOL_REGISTRY),
        )
        host = cast(
            CapabilityHost,
            context.require(ServiceKey.CAPABILITY_HOST),
        )
        context.require(ServiceKey.CONFIG)
        context.effect(
            registry.register(
                Tool(
                    name="render_html",
                    description=(
                        "将安全的自包含 HTML/CSS 渲染成当前任务的 PNG Artifact，"
                        "可引用当前 revision 的图片 Artifact。"
                    ),
                    input_schema={
                        "type": "object",
                        "required": ["html", "name", "idempotency_key"],
                        "properties": {
                            "html": {"type": "string"},
                            "name": {"type": "string"},
                            "width": {"type": "integer"},
                            "device_pixel_ratio": {"type": "integer"},
                            "idempotency_key": {"type": "string"},
                        },
                    },
                    handler=lambda tool_context, arguments: self.render(
                        host,
                        tool_context,
                        arguments,
                    ),
                    skill_name="html-render",
                    read_only=False,
                    timeout=120,
                    idempotency_required=True,
                    allowed_roles=frozenset({AgentRole.TASK}),
                )
            )
        )

    async def render(
        self,
        host: CapabilityHost,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        if invalid := task_error(host, context):
            return invalid
        key = host.idempotency_key(
            context,
            "html",
            str(arguments["idempotency_key"]),
        )
        cached = await host.store.idempotency_get(key)
        if cached:
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

        html = str(arguments["html"])
        name = Path(str(arguments["name"])).name
        if not name.lower().endswith(".png"):
            name = f"{name}.png"
        width = int(arguments.get("width", DEFAULT_HTML_RENDER_WIDTH))
        device_pixel_ratio = int(
            arguments.get(
                "device_pixel_ratio",
                DEFAULT_HTML_RENDER_DEVICE_PIXEL_RATIO,
            )
        )
        try:
            artifact_ids = self.renderer.artifact_ids(html)
            source_artifacts = await host.store.get_artifacts(
                task_id=context.task_id or "",
                revision=context.revision or 0,
                scope_id=context.scope_id,
                artifact_ids=artifact_ids,
            )
            images: dict[str, bytes] = {}
            data_root = Path(host.config.data_dir).resolve()
            for artifact in source_artifacts:
                if artifact.kind != "image" or not artifact.path:
                    return error(
                        "HTML 只能引用图片 Artifact",
                        "HTML_ASSET_INVALID",
                    )
                path = Path(artifact.path).resolve()
                path.relative_to(data_root)
                images[f"artifact://{artifact.artifact_id}"] = path.read_bytes()
            content = self.renderer.render(
                html,
                images=images,
                width=width,
                device_pixel_ratio=device_pixel_ratio,
            )
            artifact = await host.write_artifact(
                context,
                filename=name,
                kind="image",
                content=content,
                source_ref={"artifact_ids": list(artifact_ids)},
                strategy_ref={
                    "tool": "render_html",
                    "html_sha256": hashlib.sha256(
                        html.encode("utf-8")
                    ).hexdigest(),
                    "width": width,
                    "device_pixel_ratio": device_pixel_ratio,
                },
            )
        except HtmlRenderError as exc:
            return error(str(exc), "HTML_RENDER_INVALID")
        except (OSError, ValueError) as exc:
            return error(str(exc), "HTML_RENDER_FAILED")
        payload = {
            "artifact_id": artifact.artifact_id,
            "name": artifact.name,
            "size_bytes": artifact.size_bytes,
            "format": "png",
        }
        await host.store.idempotency_put(key, payload)
        return result(
            payload,
            artifacts=(artifact,),
            suppress_final_text=True,
        )

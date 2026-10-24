from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

from core.domain import AgentRole, ToolContext, ToolResult
from core.plugins import PluginContext, ServiceKey
from core.tools import Tool, ToolRegistry
from providers.markdown import (
    DEFAULT_MARKDOWN_RENDER_DEVICE_PIXEL_RATIO,
    DEFAULT_MARKDOWN_RENDER_WIDTH,
    MarkdownRenderError,
    MarkdownRenderer,
    MarkdownTheme,
)

from .base import CapabilityHost, error, result, task_error


class MarkdownCapabilityPlugin:
    name = "markdown"
    requires = frozenset(
        {
            ServiceKey.CONFIG,
            ServiceKey.TOOL_REGISTRY,
            ServiceKey.CAPABILITY_HOST,
        }
    )

    def __init__(self, renderer: MarkdownRenderer | None = None) -> None:
        self.renderer = renderer or MarkdownRenderer()

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
                    name="render_markdown",
                    description=(
                        "将 Markdown 渲染成当前任务的 PNG Artifact；支持标题、"
                        "列表、引用、表格、代码块和当前 revision 的图片 Artifact。"
                    ),
                    input_schema={
                        "type": "object",
                        "required": [
                            "markdown",
                            "name",
                            "idempotency_key",
                        ],
                        "properties": {
                            "markdown": {"type": "string"},
                            "name": {"type": "string"},
                            "theme": {
                                "type": "string",
                                "enum": [item.value for item in MarkdownTheme],
                            },
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
                    skill_name="markdown-render",
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
            "markdown",
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

        markdown = str(arguments["markdown"])
        name = Path(str(arguments["name"])).name
        if not name.lower().endswith(".png"):
            name = f"{name}.png"
        width = int(
            arguments.get("width", DEFAULT_MARKDOWN_RENDER_WIDTH)
        )
        device_pixel_ratio = int(
            arguments.get(
                "device_pixel_ratio",
                DEFAULT_MARKDOWN_RENDER_DEVICE_PIXEL_RATIO,
            )
        )
        try:
            theme = MarkdownTheme(
                str(arguments.get("theme", MarkdownTheme.DARK.value))
            )
            artifact_ids = self.renderer.artifact_ids(markdown)
        except (MarkdownRenderError, ValueError) as exc:
            return error(str(exc), "MARKDOWN_RENDER_INVALID")
        try:
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
                        "Markdown 只能引用图片 Artifact",
                        "MARKDOWN_ASSET_INVALID",
                    )
                path = Path(artifact.path).resolve()
                path.relative_to(data_root)
                images[f"artifact://{artifact.artifact_id}"] = path.read_bytes()
        except (OSError, ValueError) as exc:
            return error(str(exc), "MARKDOWN_ASSET_INVALID")
        try:
            content = self.renderer.render(
                markdown,
                images=images,
                width=width,
                device_pixel_ratio=device_pixel_ratio,
                theme=theme,
            )
            artifact = await host.write_artifact(
                context,
                filename=name,
                kind="image",
                content=content,
                source_ref={"artifact_ids": list(artifact_ids)},
                strategy_ref={
                    "tool": "render_markdown",
                    "markdown_sha256": hashlib.sha256(
                        markdown.encode("utf-8")
                    ).hexdigest(),
                    "theme": theme.value,
                    "width": width,
                    "device_pixel_ratio": device_pixel_ratio,
                },
            )
        except MarkdownRenderError as exc:
            return error(str(exc), "MARKDOWN_RENDER_INVALID")
        except (OSError, ValueError) as exc:
            return error(str(exc), "MARKDOWN_RENDER_FAILED")
        payload = {
            "artifact_id": artifact.artifact_id,
            "name": artifact.name,
            "size_bytes": artifact.size_bytes,
            "format": "png",
            "theme": theme.value,
        }
        await host.store.idempotency_put(key, payload)
        return result(
            payload,
            artifacts=(artifact,),
            suppress_final_text=True,
        )

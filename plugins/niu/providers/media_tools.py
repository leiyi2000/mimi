from __future__ import annotations

from typing import Any

from core.tools import Tool, ToolContext, ToolRegistry, ToolResult
from providers.image import ImageProvider
from providers.media import MediaError
from providers.music import MusicProvider


class MediaTools:
    def __init__(
        self,
        image_provider: ImageProvider | None,
        music_provider: MusicProvider | None,
    ) -> None:
        self.image_provider = image_provider
        self.music_provider = music_provider

    def register(self, registry: ToolRegistry) -> None:
        if self.image_provider is not None:
            registry.register(
                Tool(
                    name="generate_image",
                    description=(
                        "Generate one image from a detailed prompt, optionally "
                        "preserving a person from one input image."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "aspect_ratio": {
                                "enum": ["1:1", "16:9", "4:3", "3:4", "9:16"]
                            },
                            "reference_image_index": {
                                "type": "integer",
                                "minimum": 0,
                                "description": (
                                    "Optional image index from the current and "
                                    "quoted message, in displayed order."
                                ),
                            },
                        },
                        "required": ["prompt"],
                    },
                    handler=self.generate_image,
                    timeout=120,
                    read_only=False,
                    skill_name="image",
                    completion_required=True,
                )
            )
        if self.music_provider is not None:
            registry.register(
                Tool(
                    name="generate_music",
                    description="Generate one song or instrumental audio.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "lyrics": {"type": "string"},
                            "is_instrumental": {"type": "boolean"},
                        },
                        "required": ["prompt"],
                    },
                    handler=self.generate_music,
                    timeout=180,
                    read_only=False,
                    skill_name="music",
                    completion_required=True,
                )
            )

    async def generate_image(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        assert self.image_provider is not None
        reference_image = None
        raw_index = arguments.get("reference_image_index")
        if raw_index is not None:
            if not isinstance(raw_index, int) or isinstance(raw_index, bool):
                raise ValueError("invalid reference image index")
            available_images = context.images + context.quoted_images
            if not 0 <= raw_index < len(available_images):
                return ToolResult.error(
                    "REFERENCE_IMAGE_NOT_FOUND",
                    "指定的参考图片不存在，请使用消息中显示的图片序号。",
                    retryable=True,
                    safe_details={
                        "available_image_count": str(len(available_images))
                    },
                )
            reference_image = available_images[raw_index]
        try:
            artifact = await self.image_provider.generate(
                str(arguments["prompt"]),
                str(arguments.get("aspect_ratio", "1:1")),
                reference_image,
            )
        except MediaError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(
            "IMAGE_GENERATED",
            artifacts=(artifact,),
            suppress_final_text=True,
        )

    async def generate_music(
        self, _context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        assert self.music_provider is not None
        try:
            artifact = await self.music_provider.generate(
                str(arguments["prompt"]),
                str(arguments.get("lyrics", "")),
                bool(arguments.get("is_instrumental", False)),
            )
        except MediaError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult("MUSIC_GENERATED", artifacts=(artifact,))

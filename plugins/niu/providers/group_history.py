from __future__ import annotations

import base64
import binascii
import json
from typing import Any

import httpx

from core.tools import Tool, ToolContext, ToolImage, ToolRegistry, ToolResult


_ALLOWED_IMAGE_MIME_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
_MAX_RESPONSE_IMAGES = 10
_MAX_RESPONSE_IMAGE_BYTES = 20 * 1024 * 1024


class GroupHistoryProvider:
    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        max_items: int,
        max_chars: int,
        max_images: int,
        max_image_bytes: int,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.max_items = max_items
        self.max_chars = max_chars
        self.max_images = max_images
        self.max_image_bytes = max_image_bytes

    async def resolve_member(self, group_id: int, query: str) -> dict[str, Any]:
        return await self._post(
            "/v1/agent/members/resolve",
            {"group_id": group_id, "query": query[:100]},
        )

    async def search(
        self, group_id: int, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {
            "group_id": group_id,
            "user_id": arguments.get("target_user_id"),
            "keywords": list(arguments.get("keywords") or [])[:5],
            "after": arguments.get("after"),
            "before": arguments.get("before"),
            "limit": min(int(arguments.get("limit", 30)), self.max_items),
            "cursor": arguments.get("cursor"),
            "max_chars": self.max_chars,
            "max_images": self.max_images,
            "max_image_bytes": self.max_image_bytes,
            "max_total_image_bytes": min(
                self.max_images * self.max_image_bytes,
                _MAX_RESPONSE_IMAGE_BYTES,
            ),
        }
        return await self._post("/v1/agent/messages/search", payload)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}{path}",
                json=payload,
            )
        response.raise_for_status()
        return response.json()


class GroupHistoryTools:
    def __init__(self, provider: GroupHistoryProvider) -> None:
        self.provider = provider
        self._resolved: dict[tuple[int, int], set[int]] = {}

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            Tool(
                name="resolve_group_member",
                description="Resolve a nickname to a unique member in the current group.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                handler=self.resolve,
                skill_name="group-history",
            )
        )
        registry.register(
            Tool(
                name="search_group_messages",
                description=(
                    "Search archived text and images only in the current group."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "target_user_id": {"type": ["integer", "null"]},
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 5,
                        },
                        "after": {"type": ["integer", "null"]},
                        "before": {"type": ["integer", "null"]},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        "cursor": {"type": ["string", "null"]},
                    },
                },
                handler=self.search,
                skill_name="group-history",
                completion_required=True,
            )
        )

    async def resolve(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        try:
            result = await self.provider.resolve_member(
                context.group_id, str(arguments["query"])
            )
        except httpx.HTTPError:
            return ToolResult("GROUP_HISTORY_UNAVAILABLE", is_error=True)
        if result.get("status") == "unique":
            matches = result.get("matches") or []
            if matches:
                key = (context.group_id, context.message_id)
                self._resolved.setdefault(key, set()).add(
                    int(matches[0]["user_id"])
                )
        return ToolResult(json.dumps(result, ensure_ascii=False))

    async def search(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        target = arguments.get("target_user_id")
        allowed = self._resolved.get(
            (context.group_id, context.message_id), set()
        ) | {context.user_id}
        if target is not None and int(target) not in allowed:
            return ToolResult("GROUP_MEMBER_NOT_RESOLVED", is_error=True)
        try:
            result = await self.provider.search(context.group_id, arguments)
        except httpx.HTTPError:
            return ToolResult("GROUP_HISTORY_UNAVAILABLE", is_error=True)
        try:
            return self._tool_result(result)
        except (KeyError, TypeError, ValueError, binascii.Error):
            return ToolResult.error(
                "GROUP_HISTORY_INVALID_RESPONSE",
                "群历史服务返回了无效图片数据，请基于可用上下文回答。",
                retryable=False,
            )

    @staticmethod
    def _tool_result(result: dict[str, Any]) -> ToolResult:
        messages = result.get("data")
        if not isinstance(messages, list):
            raise TypeError("group history data must be a list")
        images: list[ToolImage] = []
        total_bytes = 0
        for message in messages:
            if not isinstance(message, dict):
                raise TypeError("group history message must be an object")
            raw_images = message.get("images") or []
            if not isinstance(raw_images, list):
                raise TypeError("group history images must be a list")
            image_metadata = []
            for message_image_index, raw_image in enumerate(raw_images, start=1):
                if len(images) >= _MAX_RESPONSE_IMAGES:
                    raise ValueError("too many group history images")
                if not isinstance(raw_image, dict):
                    raise TypeError("group history image must be an object")
                mime_type = str(raw_image["mime_type"])
                if mime_type not in _ALLOWED_IMAGE_MIME_TYPES:
                    raise ValueError("unsupported group history image type")
                data = base64.b64decode(
                    str(raw_image["data_base64"]),
                    validate=True,
                )
                byte_size = int(raw_image["byte_size"])
                if byte_size != len(data) or byte_size <= 0:
                    raise ValueError("invalid group history image size")
                total_bytes += byte_size
                if total_bytes > _MAX_RESPONSE_IMAGE_BYTES:
                    raise ValueError("group history images are too large")
                image_index = len(images)
                label = (
                    f"群历史消息 {message.get('message_id')} 图片 {message_image_index}"
                )
                images.append(ToolImage(data, mime_type, label))
                image_metadata.append(
                    {
                        "image_index": image_index,
                        "mime_type": mime_type,
                        "byte_size": byte_size,
                        "label": label,
                    }
                )
            message["images"] = image_metadata
        return ToolResult(
            json.dumps(result, ensure_ascii=False),
            images=tuple(images),
        )

from __future__ import annotations

import json
from typing import Any

import httpx

from core.tools import Tool, ToolContext, ToolRegistry, ToolResult


class GroupHistoryProvider:
    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        max_items: int,
        max_chars: int,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.max_items = max_items
        self.max_chars = max_chars

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
                description="Search archived text messages only in the current group.",
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
        return ToolResult(json.dumps(result, ensure_ascii=False))

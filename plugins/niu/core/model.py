from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"

    def as_message_block(self) -> dict[str, Any]:
        return {"type": self.type, "text": self.text}


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"

    def as_message_block(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }


ModelBlock = TextBlock | ToolUseBlock


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: tuple[ModelBlock, ...]
    stop_reason: str | None = None
    usage: ModelUsage = ModelUsage()


@dataclass(frozen=True, slots=True)
class ModelRequest:
    model: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    max_tokens: int
    reasoning_effort: str
    timeout: float
    exchange_id: str = ""
    run_id: str = ""
    round_index: int = -1


class ModelProviderError(RuntimeError):
    pass


class ModelAuthenticationError(ModelProviderError):
    pass


class ModelRateLimitError(ModelProviderError):
    pass


class ModelConnectionError(ModelProviderError):
    pass


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...

    async def close(self) -> None: ...

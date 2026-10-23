from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol

import anthropic
import httpx

from core.domain import ModelMessage, ModelResponse, ToolCall
from core.tools import Tool


class ModelProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        code: str,
        error_type: str | None = None,
        status_code: int | None = None,
        detail: str | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.error_type = error_type
        self.status_code = status_code
        self.detail = detail


class ModelProvider(Protocol):
    async def stream(
        self,
        messages: tuple[ModelMessage, ...],
        tools: tuple[Tool, ...],
        *,
        mode: str,
    ) -> AsyncIterator[ModelResponse]: ...


def tool_schema(tool: Tool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str | None,
        timeout: float = 60.0,
        max_tokens: int = 4096,
    ):
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens

    async def stream(
        self,
        messages: tuple[ModelMessage, ...],
        tools: tuple[Tool, ...],
        *,
        mode: str,
    ) -> AsyncIterator[ModelResponse]:
        payload = {
            "model": self.model,
            "messages": [self._message(item) for item in messages],
            "tools": [tool_schema(item) for item in tools],
            "stream": False,
            "max_tokens": self.max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise ModelProviderError(
                "model request timed out",
                retryable=False,
                code="MODEL_TIMEOUT",
                error_type=type(exc).__name__,
            ) from exc
        except httpx.HTTPStatusError as exc:
            retryable = (
                exc.response.status_code == 429 or exc.response.status_code >= 500
            )
            raise ModelProviderError(
                "model provider returned an error",
                retryable=retryable,
                code="MODEL_HTTP_ERROR",
                error_type=type(exc).__name__,
                status_code=exc.response.status_code,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelProviderError(
                "model provider request failed",
                retryable=True,
                code="MODEL_REQUEST_FAILED",
                error_type=type(exc).__name__,
            ) from exc
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelProviderError(
                "invalid model response",
                retryable=False,
                code="MODEL_RESPONSE_INVALID",
                error_type=type(exc).__name__,
            ) from exc
        tool_calls = tuple(
            ToolCall(
                tool_use_id=str(call.get("id") or uuid.uuid4().hex),
                name=str(call["function"]["name"]),
                arguments=_json_object(call["function"].get("arguments", "{}")),
            )
            for call in message.get("tool_calls", [])
        )
        content = message.get("content")
        if content is None and not tool_calls:
            raise ModelProviderError(
                "model returned an empty response",
                retryable=True,
                code="MODEL_EMPTY_RESPONSE",
            )
        yield ModelResponse(
            message=ModelMessage(
                role="assistant",
                content=content or "",
                tool_calls=tool_calls,
            ),
            request_id=str(data.get("id") or uuid.uuid4().hex),
            provider=self.provider,
            model=self.model,
            usage={
                str(key): int(value)
                for key, value in (data.get("usage") or {}).items()
                if isinstance(value, int)
            },
            finish_reason=choice.get("finish_reason"),
        )

    @staticmethod
    def _message(message: ModelMessage) -> dict[str, Any]:
        value: dict[str, Any] = {
            "role": message.role,
            "content": _openai_content(message.content),
        }
        if message.tool_call_id:
            value["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            value["tool_calls"] = [
                {
                    "id": call.tool_use_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments, ensure_ascii=False, separators=(",", ":")
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        return value


class AnthropicCompatibleProvider:
    """Provider adapter for endpoints implementing the Anthropic Messages API."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str | None,
        timeout: float = 60.0,
        reasoning_effort: str | None = None,
        max_tokens: int = 4096,
    ):
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key or "",
            base_url=self.base_url,
            timeout=timeout,
            max_retries=0,
        )

    async def stream(
        self,
        messages: tuple[ModelMessage, ...],
        tools: tuple[Tool, ...],
        *,
        mode: str,
    ) -> AsyncIterator[ModelResponse]:
        del mode
        system, converted_messages = self._messages(messages)
        try:
            payload: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": converted_messages,
                "timeout": self.timeout,
            }
            if system:
                payload["system"] = system
            if tools:
                payload["tools"] = [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    }
                    for tool in tools
                ]
            if self.reasoning_effort:
                payload["output_config"] = {"effort": self.reasoning_effort}
            response = await self.client.messages.create(**payload)
        except anthropic.AuthenticationError as exc:
            raise ModelProviderError(
                "model authentication failed",
                retryable=False,
                code="MODEL_AUTHENTICATION",
                error_type=type(exc).__name__,
            ) from exc
        except anthropic.RateLimitError as exc:
            raise ModelProviderError(
                "model provider rate limited the request",
                retryable=True,
                code="MODEL_RATE_LIMITED",
                error_type=type(exc).__name__,
            ) from exc
        except (anthropic.APITimeoutError, TimeoutError) as exc:
            raise ModelProviderError(
                "model request timed out",
                retryable=False,
                code="MODEL_TIMEOUT",
                error_type=type(exc).__name__,
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ModelProviderError(
                "model provider connection failed",
                retryable=True,
                code="MODEL_CONNECTION_FAILED",
                error_type=type(exc).__name__,
            ) from exc
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code == 429 or exc.status_code >= 500
            raise ModelProviderError(
                f"model provider returned status {exc.status_code}",
                retryable=retryable,
                code="MODEL_HTTP_ERROR",
                error_type=type(exc).__name__,
                status_code=exc.status_code,
            ) from exc
        except (TypeError, ValueError) as exc:
            raise ModelProviderError(
                "model request was invalid",
                retryable=False,
                code="MODEL_REQUEST_INVALID",
                error_type=type(exc).__name__,
            ) from exc

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        provider_content = tuple(
            _anthropic_response_block(block) for block in response.content
        )
        for block in response.content:
            if block.type == "text" and block.text:
                text_parts.append(str(block.text))
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        tool_use_id=str(block.id),
                        name=str(block.name),
                        arguments=dict(block.input),
                    )
                )
        text = "\n".join(text_parts)
        if not text and not tool_calls:
            raise ModelProviderError(
                "model returned an empty response",
                retryable=True,
                code="MODEL_EMPTY_RESPONSE",
            )
        usage = response.usage
        yield ModelResponse(
            message=ModelMessage(
                role="assistant",
                content=text,
                tool_calls=tuple(tool_calls),
                provider_content=provider_content,
            ),
            request_id=str(response.id or uuid.uuid4().hex),
            provider=self.provider,
            model=self.model,
            usage={
                "input_tokens": int(usage.input_tokens or 0),
                "output_tokens": int(usage.output_tokens or 0),
            },
            finish_reason=response.stop_reason,
        )

    async def close(self) -> None:
        await self.client.close()

    @classmethod
    def _messages(
        cls, messages: tuple[ModelMessage, ...]
    ) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                system_parts.append(str(message.content or ""))
                continue
            if message.role == "tool":
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": _anthropic_content(message.content),
                }
                if (
                    converted
                    and converted[-1]["role"] == "user"
                    and isinstance(converted[-1]["content"], list)
                ):
                    converted[-1]["content"].append(tool_result)
                else:
                    converted.append(
                        {
                            "role": "user",
                            "content": [tool_result],
                        }
                    )
                continue
            if message.role == "assistant" and message.provider_content:
                converted.append(
                    {
                        "role": "assistant",
                        "content": [dict(block) for block in message.provider_content],
                    }
                )
                continue
            if message.role == "assistant" and message.tool_calls:
                content: list[dict[str, Any]] = []
                if message.content:
                    content.append({"type": "text", "text": str(message.content)})
                content.extend(
                    {
                        "type": "tool_use",
                        "id": call.tool_use_id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                    for call in message.tool_calls
                )
                converted.append({"role": "assistant", "content": content})
                continue
            converted.append(
                {
                    "role": message.role,
                    "content": _anthropic_content(message.content),
                }
            )
        return "\n\n".join(item for item in system_parts if item), converted


class NullModelProvider:
    """Explicitly disabled provider used when no model credentials are configured."""

    async def stream(
        self,
        messages: tuple[ModelMessage, ...],
        tools: tuple[Tool, ...],
        *,
        mode: str,
    ) -> AsyncIterator[ModelResponse]:
        raise ModelProviderError(
            "model provider is not configured",
            retryable=False,
            code="MODEL_NOT_CONFIGURED",
        )
        yield  # pragma: no cover


class ResilientModelProvider:
    def __init__(
        self,
        providers: tuple[ModelProvider, ...],
        *,
        max_retries: int = 2,
        empty_response_retries: int = 2,
        backoff_seconds: float = 0.2,
    ):
        if not providers:
            raise ValueError("at least one model provider is required")
        self.providers = providers
        self.max_retries = max_retries
        self.empty_response_retries = empty_response_retries
        self.backoff_seconds = backoff_seconds

    async def close(self) -> None:
        closed: set[int] = set()
        for provider in self.providers:
            close = getattr(provider, "close", None)
            if close is None or id(provider) in closed:
                continue
            closed.add(id(provider))
            await close()

    async def stream(
        self,
        messages: tuple[ModelMessage, ...],
        tools: tuple[Tool, ...],
        *,
        mode: str,
    ) -> AsyncIterator[ModelResponse]:
        last_error: ModelProviderError | None = None
        for provider in self.providers:
            retries = 0
            empty_retries = 0
            while True:
                try:
                    yielded = False
                    async for response in provider.stream(messages, tools, mode=mode):
                        yielded = True
                        yield response
                    if yielded:
                        return
                    empty_retries += 1
                    if empty_retries > self.empty_response_retries:
                        raise ModelProviderError(
                            "model returned no response",
                            retryable=True,
                            code="MODEL_EMPTY_RESPONSE",
                        )
                except ModelProviderError as exc:
                    last_error = exc
                    can_retry = exc.retryable and (
                        retries < self.max_retries
                        or exc.code == "MODEL_EMPTY_RESPONSE"
                        and empty_retries <= self.empty_response_retries
                    )
                    if not can_retry:
                        break
                    retries += 1
                    await asyncio.sleep(self.backoff_seconds * (2 ** (retries - 1)))
        if last_error is not None:
            raise last_error
        raise ModelProviderError(
            "all model providers failed", retryable=False, code="MODEL_UNAVAILABLE"
        )


def _openai_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    converted: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            converted.append({"type": "text", "text": str(block)})
            continue
        if block.get("type") == "image":
            media_type = str(block.get("media_type") or "image/jpeg")
            data = str(block.get("data") or "")
            converted.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{data}",
                    },
                }
            )
        elif block.get("type") == "text":
            converted.append({"type": "text", "text": str(block.get("text") or "")})
        else:
            converted.append({"type": "text", "text": str(block)})
    return converted


def _anthropic_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    converted: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            converted.append({"type": "text", "text": str(block)})
            continue
        if block.get("type") == "image":
            converted.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": str(block.get("media_type") or "image/jpeg"),
                        "data": str(block.get("data") or ""),
                    },
                }
            )
        elif block.get("type") == "text":
            converted.append({"type": "text", "text": str(block.get("text") or "")})
        else:
            converted.append({"type": "text", "text": str(block)})
    return converted


def _anthropic_response_block(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return json.loads(json.dumps(block, ensure_ascii=False, default=str))
    model_dump = getattr(block, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json", exclude_none=True)
        if isinstance(dumped, dict):
            return dumped
    block_type = str(getattr(block, "type", ""))
    if block_type == "thinking":
        value = {
            "type": "thinking",
            "thinking": str(getattr(block, "thinking", "")),
        }
        signature = getattr(block, "signature", None)
        if signature:
            value["signature"] = str(signature)
        return value
    if block_type == "text":
        return {
            "type": "text",
            "text": str(getattr(block, "text", "")),
        }
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": str(getattr(block, "id", "")),
            "name": str(getattr(block, "name", "")),
            "input": dict(getattr(block, "input", {})),
        }
    raw = {
        key: value
        for key, value in vars(block).items()
        if not key.startswith("_")
    }
    raw.setdefault("type", block_type or "unknown")
    return json.loads(json.dumps(raw, ensure_ascii=False, default=str))


def _json_object(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise TypeError("tool arguments must be an object")
    return parsed

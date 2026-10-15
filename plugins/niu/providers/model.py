from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx

from core.model import (
    ModelAuthenticationError,
    ModelConnectionError,
    ModelProvider,
    ModelProviderError,
    ModelRateLimitError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    TextBlock,
    ToolUseBlock,
)


class AnthropicModelProvider(ModelProvider):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        supports_reasoning_effort: bool = False,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
        )
        self._supports_reasoning_effort = supports_reasoning_effort

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": request.messages,
            "timeout": request.timeout,
        }
        if request.tools:
            payload["tools"] = request.tools
        if self._supports_reasoning_effort:
            payload["output_config"] = {"effort": request.reasoning_effort}
        try:
            response = await self._client.messages.create(**payload)
        except anthropic.AuthenticationError as exc:
            raise ModelAuthenticationError() from exc
        except anthropic.RateLimitError as exc:
            raise ModelRateLimitError() from exc
        except (
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
            TimeoutError,
        ) as exc:
            raise ModelConnectionError() from exc
        except anthropic.APIStatusError as exc:
            raise ModelProviderError(
                f"model API returned status {exc.status_code}"
            ) from exc

        blocks = []
        for block in response.content:
            if block.type == "text" and block.text:
                blocks.append(TextBlock(str(block.text)))
            elif block.type == "tool_use":
                blocks.append(
                    ToolUseBlock(
                        id=str(block.id),
                        name=str(block.name),
                        input=dict(block.input),
                    )
                )
        usage = response.usage
        return ModelResponse(
            content=tuple(blocks),
            stop_reason=response.stop_reason,
            usage=ModelUsage(
                input_tokens=int(usage.input_tokens or 0),
                output_tokens=int(usage.output_tokens or 0),
            ),
        )

    async def close(self) -> None:
        await self._client.close()


class OpenAIModelProvider(ModelProvider):
    def __init__(self, *, api_key: str, base_url: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system},
                *self._messages(request.messages),
            ],
        }
        reasoning_model = request.model.startswith(("gpt-5", "o1", "o3", "o4"))
        payload["max_completion_tokens" if reasoning_model else "max_tokens"] = (
            request.max_tokens
        )
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool["input_schema"],
                    },
                }
                for tool in request.tools
            ]
        if reasoning_model:
            payload["reasoning_effort"] = request.reasoning_effort
        try:
            response = await self._client.post(
                "chat/completions",
                json=payload,
                timeout=request.timeout,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ModelConnectionError() from exc
        if response.status_code in {401, 403}:
            raise ModelAuthenticationError()
        if response.status_code == 429:
            raise ModelRateLimitError()
        if response.is_error:
            raise ModelProviderError(
                f"model API returned status {response.status_code}"
            )
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            blocks = []
            if content := str(message.get("content") or "").strip():
                blocks.append(TextBlock(content))
            for call in message.get("tool_calls") or []:
                function = call["function"]
                arguments = json.loads(function.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise TypeError("tool arguments must be an object")
                blocks.append(
                    ToolUseBlock(
                        id=str(call["id"]),
                        name=str(function["name"]),
                        input=arguments,
                    )
                )
            usage = data.get("usage") or {}
        except (
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise ModelProviderError("invalid model response") from exc
        return ModelResponse(
            content=tuple(blocks),
            stop_reason=(
                "max_tokens"
                if choice.get("finish_reason") == "length"
                else choice.get("finish_reason")
            ),
            usage=ModelUsage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
        )

    async def close(self) -> None:
        await self._client.aclose()

    @classmethod
    def _messages(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = str(message["role"])
            content = message["content"]
            if role == "assistant" and isinstance(content, list):
                text_parts = [
                    str(block.get("text") or "")
                    for block in content
                    if block.get("type") == "text"
                ]
                calls = [
                    {
                        "id": str(block["id"]),
                        "type": "function",
                        "function": {
                            "name": str(block["name"]),
                            "arguments": json.dumps(
                                block.get("input") or {},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                    for block in content
                    if block.get("type") == "tool_use"
                ]
                converted.append(
                    {
                        "role": "assistant",
                        "content": "\n".join(part for part in text_parts if part)
                        or None,
                        "tool_calls": calls,
                    }
                )
                continue
            if (
                role == "user"
                and isinstance(content, list)
                and all(block.get("type") == "tool_result" for block in content)
            ):
                tool_messages = []
                tool_images = []
                for block in content:
                    result_content = block.get("content") or ""
                    if isinstance(result_content, list):
                        text_parts = [
                            str(part.get("text") or "")
                            for part in result_content
                            if part.get("type") == "text"
                        ]
                        result_text = "\n".join(part for part in text_parts if part)
                        tool_images.extend(
                            part
                            for part in result_content[1:]
                            if part.get("type") in {"text", "image"}
                        )
                    else:
                        result_text = str(result_content)
                    tool_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(block["tool_use_id"]),
                            "content": result_text,
                        }
                    )
                converted.extend(tool_messages)
                if tool_images:
                    converted.append(
                        {
                            "role": "user",
                            "content": cls._content(
                                [
                                    {
                                        "type": "text",
                                        "text": (
                                            "以下图片来自上方工具结果，"
                                            "按工具图片标签与消息对应。"
                                        ),
                                    },
                                    *tool_images,
                                ]
                            ),
                        }
                    )
                continue
            converted.append(
                {
                    "role": role,
                    "content": cls._content(content),
                }
            )
        return converted

    @staticmethod
    def _content(content: Any) -> Any:
        if not isinstance(content, list):
            return content
        converted = []
        for block in content:
            block_type = block.get("type")
            if block_type == "text":
                converted.append({"type": "text", "text": str(block.get("text") or "")})
            elif block_type == "image":
                source = block["source"]
                converted.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": (
                                f"data:{source['media_type']};base64,{source['data']}"
                            )
                        },
                    }
                )
        return converted


def create_model_provider(
    *,
    provider: str,
    api_key: str,
    base_url: str,
) -> ModelProvider:
    if provider == "minimax":
        return AnthropicModelProvider(
            api_key=api_key,
            base_url=base_url,
            supports_reasoning_effort=True,
        )
    if provider == "anthropic":
        return AnthropicModelProvider(
            api_key=api_key,
            base_url=base_url,
        )
    if provider == "openai":
        return OpenAIModelProvider(
            api_key=api_key,
            base_url=base_url,
        )
    raise ValueError(f"unsupported model provider: {provider}")

import json
import logging

import httpx

from core.model import ModelRequest, TextBlock, ToolUseBlock
from providers.model import OpenAIModelProvider, create_model_provider


def request(messages, tools=None):
    return ModelRequest(
        model="gpt-5-mini",
        system="system",
        messages=messages,
        tools=tools or [],
        max_tokens=512,
        reasoning_effort="low",
        timeout=5,
    )


async def test_openai_provider_converts_tools_images_and_usage(caplog):
    captured = {}

    async def handler(http_request):
        captured.update(json.loads(http_request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "先查询",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"query":"Niu"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                },
            },
        )

    provider = OpenAIModelProvider(
        api_key="test-key",
        base_url="https://api.openai.com/v1",
    )
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(
        base_url="https://api.openai.com/v1/",
        transport=httpx.MockTransport(handler),
    )
    with caplog.at_level(logging.DEBUG, logger="providers.model"):
        response = await provider.complete(
            request(
                [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "aW1hZ2U=",
                                },
                            },
                            {"type": "text", "text": "这是什么"},
                        ],
                    }
                ],
                [
                    {
                        "name": "search",
                        "description": "search",
                        "input_schema": {"type": "object"},
                    }
                ],
            )
        )
    await provider.close()

    assert response.content == (
        TextBlock("先查询"),
        ToolUseBlock("call-1", "search", {"query": "Niu"}),
    )
    assert response.usage.input_tokens == 12
    assert captured["messages"][1]["content"][0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert captured["tools"][0]["function"]["parameters"] == {"type": "object"}
    assert captured["max_completion_tokens"] == 512
    assert "max_tokens" not in captured
    assert captured["reasoning_effort"] == "low"
    assert "event=model.provider_response protocol=openai" in caplog.text
    assert '"finish_reason":"tool_calls"' in caplog.text
    assert '"arguments":"{\\"query\\":\\"Niu\\"}"' in caplog.text
    assert all("\n" not in record.getMessage() for record in caplog.records)


def test_openai_provider_converts_tool_roundtrip_messages():
    messages = OpenAIModelProvider._messages(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "查询"},
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "search",
                        "input": {"query": "Niu"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": "result",
                        "is_error": False,
                    }
                ],
            },
        ]
    )

    assert messages[0]["tool_calls"][0]["function"]["arguments"] == ('{"query":"Niu"}')
    assert messages[1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "result",
    }


def test_openai_provider_places_tool_images_after_all_tool_results():
    messages = OpenAIModelProvider._messages(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": [
                            {"type": "text", "text": '{"data": []}'},
                            {"type": "text", "text": "[工具图片：历史图片]"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "cG5n",
                                },
                            },
                        ],
                        "is_error": False,
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-2",
                        "content": "done",
                        "is_error": False,
                    },
                ],
            }
        ]
    )

    assert [message["role"] for message in messages] == ["tool", "tool", "user"]
    assert messages[0]["content"].startswith('{"data": []}')
    assert messages[1]["content"] == "done"
    assert messages[2]["content"][-1]["image_url"]["url"] == (
        "data:image/png;base64,cG5n"
    )


async def test_factory_creates_supported_providers():
    providers = [
        create_model_provider(
            provider=name,
            api_key="key",
            base_url="https://example.com",
        )
        for name in ("minimax", "anthropic", "openai")
    ]

    for provider in providers:
        await provider.close()

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import pytest

from core.config import PumpkinConfig, ReasoningEffort
from core.domain import ModelMessage, ModelResponse, ToolCall
from providers.code import CodeProviderError, CodeRunnerProvider
from providers.model import (
    AnthropicCompatibleProvider,
    ModelProviderError,
    OpenAICompatibleProvider,
    ResilientModelProvider,
)


def test_assistant_name_is_also_the_group_wake_prefix(monkeypatch):
    monkeypatch.setenv("PUMPKIN_ASSISTANT_NAME", "南瓜")
    monkeypatch.setenv("PUMPKIN_MODEL", "test-model")
    monkeypatch.setenv("PUMPKIN_MODEL_API_KEY", "model-test")
    monkeypatch.setenv("PUMPKIN_MODEL_BASE_URL", "`https://model.test/v1`")
    monkeypatch.setenv("PUMPKIN_SEARCH_API_KEY", "search-test")

    config = PumpkinConfig.from_env()

    assert config.assistant.name == "南瓜"
    assert config.activation.prefixes == ("南瓜",)
    assert config.model_chat.model == "test-model"
    assert config.model_chat.base_url == "https://model.test/v1"
    assert config.web.api_key == "search-test"
    assert config.image.api_base_url is None
    assert config.audio.api_base_url is None


def test_time_defaults_are_bounded_for_interactive_runtime(monkeypatch):
    for name in (
        "PUMPKIN_SESSION_TTL_SECONDS",
        "PUMPKIN_TOPIC_MAX_LIFETIME_SECONDS",
        "PUMPKIN_DIRECT_FOLLOW_UP_WINDOW_SECONDS",
        "PUMPKIN_FOLLOW_UP_TURN_BUDGET",
        "PUMPKIN_CHAT_TIMEOUT_SECONDS",
        "PUMPKIN_TASK_TIMEOUT_SECONDS",
        "PUMPKIN_SINGLE_TASK_TIMEOUT_SECONDS",
        "PUMPKIN_SEARCH_TIMEOUT_SECONDS",
        "PUMPKIN_CODE_TIMEOUT_SECONDS",
        "PUMPKIN_IMAGE_TIMEOUT_SECONDS",
        "PUMPKIN_AUDIO_TIMEOUT_SECONDS",
        "PUMPKIN_MEDIA_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    config = PumpkinConfig.from_env()

    assert config.activation.session_ttl_seconds == 900
    assert config.activation.topic_max_lifetime_seconds == 900
    assert config.activation.direct_follow_up_window_seconds == 300
    assert config.activation.follow_up_turn_budget == 6
    assert config.model_chat.request_timeout_seconds == 60
    assert config.model_task.request_timeout_seconds == 90
    assert config.model_chat.reasoning_effort is ReasoningEffort.LOW
    assert config.model_task.reasoning_effort is ReasoningEffort.MAX
    assert config.model_chat.max_tokens == 4096
    assert config.model_task.max_tokens == 8192
    assert config.task.single_task_timeout_seconds == 900
    assert config.web.timeout_seconds == 10
    assert config.web.max_fetch_bytes == 1024 * 1024
    assert config.web.max_image_bytes == 20 * 1024 * 1024
    assert config.code.timeout_seconds == 20
    assert config.image.timeout_seconds == 90
    assert config.audio.timeout_seconds == 90
    assert config.media.timeout_seconds == 10


@pytest.mark.parametrize("value", [item.value for item in ReasoningEffort])
def test_reasoning_effort_accepts_all_supported_values(monkeypatch, value):
    monkeypatch.setenv("PUMPKIN_CHAT_REASONING_EFFORT", value)
    monkeypatch.setenv("PUMPKIN_TASK_REASONING_EFFORT", value)

    config = PumpkinConfig.from_env()

    assert config.model_chat.reasoning_effort is ReasoningEffort(value)
    assert config.model_task.reasoning_effort is ReasoningEffort(value)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (
            "PUMPKIN_CHAT_REASONING_EFFORT",
            "none",
            "PUMPKIN_CHAT_REASONING_EFFORT must be one of",
        ),
        (
            "PUMPKIN_TASK_REASONING_EFFORT",
            "extreme",
            "PUMPKIN_TASK_REASONING_EFFORT must be one of",
        ),
        (
            "PUMPKIN_CHAT_MAX_TOKENS",
            "0",
            "PUMPKIN_CHAT_MAX_TOKENS must be a positive integer",
        ),
        (
            "PUMPKIN_TASK_MAX_TOKENS",
            "many",
            "PUMPKIN_TASK_MAX_TOKENS must be a positive integer",
        ),
    ],
)
def test_invalid_model_budget_configuration_fails_fast(
    monkeypatch, name, value, message
):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        PumpkinConfig.from_env()


def test_global_reasoning_effort_does_not_override_role_configuration(monkeypatch):
    monkeypatch.setenv("PUMPKIN_REASONING_EFFORT", "medium")
    monkeypatch.delenv("PUMPKIN_CHAT_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("PUMPKIN_TASK_REASONING_EFFORT", raising=False)

    config = PumpkinConfig.from_env()

    assert config.model_chat.reasoning_effort is ReasoningEffort.LOW
    assert config.model_task.reasoning_effort is ReasoningEffort.MAX


def test_legacy_niu_and_sandbox_variables_are_ignored(monkeypatch):
    for name in (
        "PUMPKIN_MODEL",
        "PUMPKIN_MODEL_API_KEY",
        "PUMPKIN_MODEL_BASE_URL",
        "PUMPKIN_ASSISTANT_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NIU_MODEL", "legacy-model")
    monkeypatch.setenv("NIU_MODEL_API_KEY", "legacy-model-key")
    monkeypatch.setenv("NIU_MODEL_BASE_URL", "https://legacy-model.test")
    monkeypatch.setenv("NIU_TRIGGER_PREFIX", "legacy-prefix")
    monkeypatch.setenv("SANDBOX_URL", "http://legacy-sandbox:8080")
    monkeypatch.setenv("SANDBOX_TOKEN", "legacy-sandbox-token")

    config = PumpkinConfig.from_env()

    assert config.model_chat.model == ""
    assert config.model_chat.base_url is None
    assert config.model_chat.api_key is None
    assert config.code.base_url == "http://code-runner:8080"
    assert config.code.token is None
    assert config.activation.prefixes == ("configured-assistant",)


def test_anysearch_variables_are_not_pumpkin_aliases(monkeypatch):
    monkeypatch.delenv("PUMPKIN_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("PUMPKIN_SEARCH_API_URL", raising=False)
    monkeypatch.setenv("ANYSEARCH_API_KEY", "legacy-search-key")
    monkeypatch.setenv("ANYSEARCH_API_URL", "https://legacy-search.test")

    config = PumpkinConfig.from_env()

    assert config.web.api_key is None
    assert config.web.api_url == "https://api.anysearch.com/v1/search"


def test_anthropic_messages_are_converted_for_tool_loop():
    messages = (
        ModelMessage(role="system", content="system"),
        ModelMessage(role="user", content="question"),
        ModelMessage(
            role="assistant",
            content="",
            tool_calls=(
                ToolCall(
                    tool_use_id="tool-1",
                    name="search_web",
                    arguments={"query": "test"},
                ),
            ),
        ),
        ModelMessage(
            role="tool",
            tool_call_id="tool-1",
            content='{"results":[]}',
        ),
    )
    system, converted = AnthropicCompatibleProvider._messages(messages)

    assert system == "system"
    assert converted[1]["content"][0]["type"] == "tool_use"
    assert converted[2]["content"][0]["type"] == "tool_result"


def test_multimodal_tool_results_are_converted_for_both_providers():
    content = [
        {"type": "text", "text": '{"media_type":"image/png"}'},
        {"type": "image", "media_type": "image/png", "data": "cG5n"},
    ]
    messages = (ModelMessage(role="tool", tool_call_id="tool-1", content=content),)

    _, anthropic_messages = AnthropicCompatibleProvider._messages(messages)
    tool_content = anthropic_messages[0]["content"][0]["content"]
    assert tool_content[1]["type"] == "image"
    assert tool_content[1]["source"]["media_type"] == "image/png"
    assert tool_content[1]["source"]["data"] == "cG5n"

    openai_message = OpenAICompatibleProvider._message(messages[0])
    assert openai_message["content"][1]["type"] == "image_url"
    assert (
        openai_message["content"][1]["image_url"]["url"] == "data:image/png;base64,cG5n"
    )


def test_legacy_sandbox_file_fields_are_supported():
    provider = CodeRunnerProvider(
        base_url="http://sandbox:8080",
        token="test",
        timeout=5,
        max_output_chars=100,
    )
    result = provider._decode_result(
        {
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "truncated": False,
            "files": [
                {
                    "path": "result.txt",
                    "mime_type": "text/plain",
                    "data": "b2s=",
                }
            ],
        }
    )
    assert result.files[0].name == "result.txt"
    assert result.files[0].content_type == "text/plain"


@pytest.mark.asyncio
async def test_code_runner_retries_one_transient_failure():
    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class Client:
        def __init__(self):
            self.calls = 0

        async def post(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            if self.calls == 1:
                return Response(503, {})
            return Response(
                200,
                {
                    "exit_code": 0,
                    "stdout": "sandbox-ok\n",
                    "stderr": "",
                    "timed_out": False,
                    "truncated": False,
                    "files": [],
                },
            )

    client = Client()
    provider = CodeRunnerProvider(
        base_url="http://sandbox:8080",
        token=None,
        timeout=5,
        max_output_chars=100,
        client=client,
    )

    result = await provider.execute("print('sandbox-ok')", 5)

    assert client.calls == 2
    assert result.stdout == "sandbox-ok\n"


@pytest.mark.asyncio
async def test_code_runner_preserves_structured_request_error():
    class Response:
        status_code = 400

        @staticmethod
        def json():
            return {"error": {"code": "SCRIPT_SYNTAX_ERROR"}}

    class Client:
        async def post(self, *args, **kwargs):
            del args, kwargs
            return Response()

    provider = CodeRunnerProvider(
        base_url="http://sandbox:8080",
        token=None,
        timeout=5,
        max_output_chars=100,
        client=Client(),
    )

    with pytest.raises(CodeProviderError, match="SCRIPT_SYNTAX_ERROR"):
        await provider.execute("not valid python", 5)


@pytest.mark.asyncio
async def test_anthropic_sdk_provider_maps_text_and_tool_calls(monkeypatch):
    class Messages:
        async def create(self, **payload):
            assert payload["model"] == "test-model"
            assert payload["max_tokens"] == 8192
            assert payload["output_config"] == {"effort": "max"}
            assert payload["tools"][0]["name"] == "search_web"
            return SimpleNamespace(
                id="response-1",
                content=[
                    SimpleNamespace(
                        type="thinking",
                        thinking="先调用搜索工具",
                        signature="signed-thinking",
                    ),
                    SimpleNamespace(type="text", text="回答"),
                    SimpleNamespace(
                        type="tool_use",
                        id="tool-1",
                        name="search_web",
                        input={"query": "test"},
                    ),
                ],
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=3, output_tokens=4),
            )

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0
            self.messages = Messages()

        async def close(self):
            return None

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Client)
    provider = AnthropicCompatibleProvider(
        provider="minimax",
        model="test-model",
        base_url="https://example.test/anthropic",
        api_key="test-key",
        max_tokens=8192,
        reasoning_effort=ReasoningEffort.MAX,
    )
    responses = [
        response
        async for response in provider.stream(
            (ModelMessage(role="user", content="你好"),),
            (
                SimpleNamespace(
                    name="search_web",
                    description="search",
                    input_schema={"type": "object"},
                ),
            ),
            mode="chat",
        )
    ]

    assert responses[0].message.content == "回答"
    assert responses[0].message.tool_calls[0].name == "search_web"
    assert responses[0].message.provider_content[0] == {
        "type": "thinking",
        "thinking": "先调用搜索工具",
        "signature": "signed-thinking",
    }
    assert responses[0].usage == {"input_tokens": 3, "output_tokens": 4}
    _, continued = AnthropicCompatibleProvider._messages((responses[0].message,))
    assert continued[0]["content"] == list(
        responses[0].message.provider_content
    )
    await provider.close()


@pytest.mark.asyncio
async def test_anthropic_sdk_provider_does_not_retry_timeout(monkeypatch):
    class Messages:
        calls = 0

        async def create(self, **payload):
            del payload
            self.calls += 1
            raise TimeoutError

    messages = Messages()

    class Client:
        def __init__(self, **kwargs):
            del kwargs
            self.messages = messages

        async def close(self):
            return None

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Client)
    provider = AnthropicCompatibleProvider(
        provider="minimax",
        model="test-model",
        base_url="https://example.test/anthropic",
        api_key="test-key",
    )
    resilient = ResilientModelProvider(
        (provider,),
        max_retries=2,
        backoff_seconds=0,
    )

    with pytest.raises(ModelProviderError) as exc_info:
        _ = [
            response
            async for response in resilient.stream((), (), mode="chat")
        ]

    assert exc_info.value.code == "MODEL_TIMEOUT"
    assert not exc_info.value.retryable
    assert messages.calls == 1
    await resilient.close()


@pytest.mark.asyncio
async def test_resilient_provider_retries_failures_and_empty_responses():
    class Provider:
        def __init__(self, *, empty_first=False):
            self.calls = 0
            self.empty_first = empty_first

        async def stream(self, messages, tools, *, mode):
            self.calls += 1
            if self.calls == 1 and self.empty_first:
                return
            if self.calls < 3:
                raise ModelProviderError(
                    "temporary failure",
                    retryable=True,
                    code="MODEL_CONNECTION_FAILED",
                )
            yield ModelResponse(
                message=ModelMessage(role="assistant", content="ok"),
                request_id="response",
                provider="test",
                model="test",
            )

    for provider in (Provider(), Provider(empty_first=True)):
        resilient = ResilientModelProvider(
            (provider,),
            max_retries=2,
            empty_response_retries=2,
            backoff_seconds=0,
        )
        responses = [
            response async for response in resilient.stream((), (), mode="chat")
        ]
        assert responses[0].message.content == "ok"
        assert provider.calls == 3

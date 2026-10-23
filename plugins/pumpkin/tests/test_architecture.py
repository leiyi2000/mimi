from __future__ import annotations

from core.config import PumpkinConfig
from core.domain import AgentRole, ToolContext, ToolResult, TurnCapability
from core.plugins import ServiceKey
from core.tools import Tool, ToolRegistry
from runtime.app import PumpkinRuntime
from runtime.reliability import ReliablePumpkinRuntime
from runtime.tools import build_registry


class ToolHost:
    def __getattr__(self, name):
        async def handler(context, arguments):
            del context, arguments
            return ToolResult(content={"handler": name})

        return handler


def test_runtime_core_services_are_owned_by_plugin_definitions():
    runtime = PumpkinRuntime(PumpkinConfig.from_env(), client=object())

    expected = {
        ServiceKey.STORE: runtime.store,
        ServiceKey.TASK_SERVICE: runtime.task_service,
        ServiceKey.SESSION_SERVICE: runtime.sessions,
        ServiceKey.PROVIDER_CHAT: runtime.provider_chat,
        ServiceKey.PROVIDER_TASK: runtime.provider_task,
        ServiceKey.TOOL_REGISTRY: runtime.registry,
        ServiceKey.SKILL_REGISTRY: runtime.skills,
        ServiceKey.AGENT_POOL: runtime._conversation_agents,
        ServiceKey.CONVERSATION_DRIVER: runtime.conversation_driver,
        ServiceKey.TASK_RUNTIME: runtime.task_runtime,
        ServiceKey.TASK_EVENT_NOTIFIER: runtime.task_event_notifier,
        ServiceKey.ARTIFACT_SERVICE: runtime.artifact_service,
        ServiceKey.NAPCAT_SENDER: runtime.sender,
        ServiceKey.NAPCAT_ADAPTER: runtime.adapter,
        ServiceKey.MEDIA_PROVIDER: runtime.media_provider,
        ServiceKey.TEXT_RENDERER: runtime.text_renderer,
        ServiceKey.OUTBOX: runtime.outbox,
        ServiceKey.REPLY_LEDGER: runtime.reply_ledger,
    }

    for key, service in expected.items():
        assert runtime.plugin_manager.service(key) is service


def test_reliability_driver_is_decorated_inside_service_definition():
    runtime = ReliablePumpkinRuntime(PumpkinConfig.from_env(), client=object())

    assert (
        runtime.plugin_manager.service(ServiceKey.CONVERSATION_DRIVER)
        is runtime.conversation_driver
    )


def test_conversation_reply_tools_are_builtin_runtime_protocols():
    registry = build_registry(ToolHost())

    assert registry.get("send_napcat_reply").skill_name is None
    assert registry.get("send_napcat_replies").skill_name is None


async def test_tool_registry_enforces_required_turn_capabilities():
    called = False

    async def handler(context, arguments):
        nonlocal called
        del context, arguments
        called = True
        return ToolResult(content={"ok": True})

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="deliver",
            description="test",
            input_schema={"type": "object"},
            handler=handler,
            required_capabilities=frozenset({TurnCapability.TASK_DELIVERY}),
        )
    )

    result = await registry.execute(
        "deliver",
        ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="message-1",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="run-1",
        ),
        {},
    )

    assert result.is_error
    assert result.error_code == "TOOL_CAPABILITY_NOT_AUTHORIZED"
    assert not called


async def test_task_delivery_rejects_direct_execution_without_event_capability():
    registry = build_registry(ToolHost())

    result = await registry.execute(
        "send_task_delivery",
        ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="message-1",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="run-1",
        ),
        {},
    )

    assert result.is_error
    assert result.error_code == "TOOL_CAPABILITY_NOT_AUTHORIZED"


def test_task_event_notifier_is_shared_without_runtime_callback_bridge():
    runtime = PumpkinRuntime(PumpkinConfig.from_env(), client=object())

    assert runtime.artifact_service.task_event_notifier is runtime.task_event_notifier
    assert runtime.task_commands.task_event_notifier is runtime.task_event_notifier
    assert runtime.task_runtime.task_event_notifier is runtime.task_event_notifier
    assert not hasattr(runtime, "_notify_task_event")
    assert not hasattr(runtime.task_runtime, "notify_task_event")

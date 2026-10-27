from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from adapters.napcat import NapCatMessageAdapter, NapCatSender
from capabilities import (
    AudioCapabilityPlugin,
    CodeCapabilityPlugin,
    HtmlCapabilityPlugin,
    ImageCapabilityPlugin,
    MarkdownCapabilityPlugin,
    WebCapabilityPlugin,
)
from capabilities.base import CapabilityServices
from core.agent import Agent, AgentDriver
from core.agent_loop import AgentLoopProfile, SessionAgentLoop
from core.config import ModelConfig, PumpkinConfig
from core.conversation import ChatSessionService
from core.domain import (
    AgentInboxSource,
    AgentProfile,
    AgentRole,
    AgentStatus,
    ArtifactRef,
    TaskHandoff,
    ToolContext,
)
from core.plugins import PluginManager, ServiceKey
from core.prompts import CHAT_PERSONA, static_prompt
from core.services import ServiceDefinition, ServiceProvider
from core.skills import SkillRegistry
from core.tasking import TaskService, TaskStateError
from core.tools import ToolRegistry
from persistence.store import PumpkinStore
from providers.html import HtmlRenderer
from providers.markdown import MarkdownRenderer
from providers.media import MessageMediaProvider
from providers.model import (
    AnthropicCompatibleProvider,
    ModelProvider,
    NullModelProvider,
    OpenAICompatibleProvider,
    ResilientModelProvider,
)
from providers.stickers import ModelStickerClassifier, StickerClassifier
from providers.text_image import TextImageRenderer
from runtime.artifacts import ArtifactService
from runtime.chat_tools import ChatToolService
from runtime.delivery import DeliveryService
from runtime.ingress import MessageIngress
from runtime.outbox import OutboxSender
from runtime.payloads import (
    correction_payload,
    event_payload,
    handoff_payload,
    record_payload,
    snapshot_payload,
    task_requester_id,
    task_source_message_id,
)
from runtime.reply_ledger import TurnReplyLedger
from runtime.task_commands import TaskCommandService
from runtime.task_events import TaskEventNotifier
from runtime.task_runtime import TaskRuntime
from runtime.stickers import StickerService
from runtime.tools import build_registry
from runtime.recovery import RuntimeRecovery
from runtime.turn_policy import ConversationTurnPolicy


log = logging.getLogger(__name__)


class PumpkinRuntime:
    """Composition root for the Harness-style Pumpkin runtime."""

    def __init__(
        self,
        config: PumpkinConfig,
        *,
        client: Any,
        provider_chat: ModelProvider | None = None,
        provider_task: ModelProvider | None = None,
        text_renderer: TextImageRenderer | None = None,
        html_renderer: HtmlRenderer | None = None,
        markdown_renderer: MarkdownRenderer | None = None,
        sticker_classifier: StickerClassifier | None = None,
        trace: Callable[[str], None] | None = None,
        conversation_driver_decorator: Callable[
            [AgentDriver], AgentDriver
        ] | None = None,
    ):
        self.config = config
        self.client = client
        self.trace = trace or (lambda *_args: None)
        self._conversation_agents: dict[str, Agent] = {}
        self._closing = False
        self.plugin_manager = PluginManager({ServiceKey.CONFIG: config})
        self.plugin_manager.mount_all(
            (
                ServiceDefinition(
                    name="persistence-sqlite",
                    requires=frozenset({ServiceKey.CONFIG}),
                    providers=(
                        ServiceProvider(
                            ServiceKey.STORE,
                            lambda _context: PumpkinStore(),
                        ),
                    ),
                ),
                ServiceDefinition(
                    name="session",
                    requires=frozenset(
                        {ServiceKey.CONFIG, ServiceKey.STORE}
                    ),
                    providers=(
                        ServiceProvider(
                            ServiceKey.TASK_SERVICE,
                            lambda context: TaskService(
                                context.require(ServiceKey.STORE)
                            ),
                        ),
                        ServiceProvider(
                            ServiceKey.SESSION_SERVICE,
                            lambda context: ChatSessionService(
                                context.require(ServiceKey.STORE),
                                config.activation,
                            ),
                        ),
                    ),
                ),
                ServiceDefinition(
                    name="llm",
                    requires=frozenset({ServiceKey.CONFIG}),
                    providers=(
                        ServiceProvider(
                            ServiceKey.PROVIDER_CHAT,
                            lambda _context: provider_chat
                            or _provider(config.model_chat),
                        ),
                        ServiceProvider(
                            ServiceKey.PROVIDER_TASK,
                            lambda _context: provider_task
                            or _provider(config.model_task),
                        ),
                    ),
                ),
                ServiceDefinition(
                    name="tools",
                    requires=frozenset(),
                    providers=(
                        ServiceProvider(
                            ServiceKey.TOOL_REGISTRY,
                            lambda _context: ToolRegistry(trace=self.trace),
                        ),
                    ),
                ),
                ServiceDefinition(
                    name="napcat",
                    requires=frozenset({ServiceKey.CONFIG}),
                    providers=(
                        ServiceProvider(
                            ServiceKey.NAPCAT_SENDER,
                            lambda _context: NapCatSender(
                                client,
                                allowed_path_root=config.data_dir,
                            ),
                        ),
                        ServiceProvider(
                            ServiceKey.NAPCAT_ADAPTER,
                            lambda _context: NapCatMessageAdapter(),
                        ),
                        ServiceProvider(
                            ServiceKey.MEDIA_PROVIDER,
                            lambda _context: MessageMediaProvider(
                                client=client,
                                timeout=config.media.timeout_seconds,
                                max_bytes=config.media.max_bytes,
                                trusted_hosts=(config.napcat_host,),
                            ),
                        ),
                        ServiceProvider(
                            ServiceKey.TEXT_RENDERER,
                            lambda _context: text_renderer
                            or TextImageRenderer(),
                        ),
                    ),
                ),
                ServiceDefinition(
                    name="agent",
                    requires=frozenset(),
                    providers=(
                        ServiceProvider(
                            ServiceKey.AGENT_POOL,
                            lambda _context: self._conversation_agents,
                        ),
                    ),
                ),
            )
        )
        self.store = self.plugin_manager.service(ServiceKey.STORE)
        self.task_service = self.plugin_manager.service(ServiceKey.TASK_SERVICE)
        self.sessions = self.plugin_manager.service(ServiceKey.SESSION_SERVICE)
        self.provider_chat = self.plugin_manager.service(ServiceKey.PROVIDER_CHAT)
        self.provider_task = self.plugin_manager.service(ServiceKey.PROVIDER_TASK)
        self.registry = self.plugin_manager.service(ServiceKey.TOOL_REGISTRY)
        self.sender = self.plugin_manager.service(ServiceKey.NAPCAT_SENDER)
        self.adapter = self.plugin_manager.service(ServiceKey.NAPCAT_ADAPTER)
        self.media_provider = self.plugin_manager.service(ServiceKey.MEDIA_PROVIDER)
        self.text_renderer = self.plugin_manager.service(ServiceKey.TEXT_RENDERER)
        self.plugin_manager.mount_all(
            (
                ServiceDefinition(
                    name="outbox",
                    requires=frozenset(
                        {
                            ServiceKey.CONFIG,
                            ServiceKey.STORE,
                            ServiceKey.NAPCAT_SENDER,
                            ServiceKey.TEXT_RENDERER,
                        }
                    ),
                    providers=(
                        ServiceProvider(
                            ServiceKey.OUTBOX,
                            lambda context: OutboxSender(
                                store=context.require(ServiceKey.STORE),
                                sender=context.require(ServiceKey.NAPCAT_SENDER),
                                text_renderer=context.require(
                                    ServiceKey.TEXT_RENDERER
                                ),
                                data_dir=config.data_dir,
                                assistant_name=config.assistant.name,
                            ),
                        ),
                        ServiceProvider(
                            ServiceKey.REPLY_LEDGER,
                            lambda _context: TurnReplyLedger(),
                        ),
                    ),
                ),
                ServiceDefinition(
                    name="task-event-notifier",
                    requires=frozenset({ServiceKey.STORE}),
                    providers=(
                        ServiceProvider(
                            ServiceKey.TASK_EVENT_NOTIFIER,
                            lambda context: TaskEventNotifier(
                                store=context.require(ServiceKey.STORE),
                                assistant_context=self._assistant_context_payload,
                                conversation_agent=self._conversation_agent,
                                event_payload=event_payload,
                                snapshot_payload=snapshot_payload,
                            ),
                        ),
                    ),
                ),
                ServiceDefinition(
                    name="artifacts",
                    requires=frozenset(
                        {
                            ServiceKey.CONFIG,
                            ServiceKey.STORE,
                            ServiceKey.TASK_SERVICE,
                            ServiceKey.TASK_EVENT_NOTIFIER,
                        }
                    ),
                    providers=(
                        ServiceProvider(
                            ServiceKey.ARTIFACT_SERVICE,
                            lambda context: ArtifactService(
                                config,
                                store=context.require(ServiceKey.STORE),
                                task_service=context.require(
                                    ServiceKey.TASK_SERVICE
                                ),
                                task_lock=lambda task_id: self.task_runtime.task_lock(
                                    task_id
                                ),
                                task_event_notifier=context.require(
                                    ServiceKey.TASK_EVENT_NOTIFIER
                                ),
                            ),
                        ),
                    ),
                ),
            )
        )
        self.outbox = self.plugin_manager.service(ServiceKey.OUTBOX)
        self.reply_ledger = self.plugin_manager.service(ServiceKey.REPLY_LEDGER)
        self.task_event_notifier = self.plugin_manager.service(
            ServiceKey.TASK_EVENT_NOTIFIER
        )
        self.artifact_service = self.plugin_manager.service(
            ServiceKey.ARTIFACT_SERVICE
        )
        self.stickers = StickerService(
            store=self.store,
            media_provider=self.media_provider,
            classifier=sticker_classifier
            or ModelStickerClassifier(self.provider_chat),
            outbox=self.outbox,
            reply_ledger=self.reply_ledger,
            data_dir=config.data_dir,
            idempotency_key=self.idempotency_key,
        )
        self.capability_services = CapabilityServices(
            config=config,
            store=self.store,
            _task_context=self.capability_task,
            _idempotency_key=self.idempotency_key,
            _write_artifact=self.artifact_service.write,
        )
        self.plugin_manager.mount_all(
            (
                ServiceDefinition(
                    name="capability-host",
                    requires=frozenset({ServiceKey.ARTIFACT_SERVICE}),
                    providers=(
                        ServiceProvider(
                            ServiceKey.CAPABILITY_HOST,
                            lambda _context: self.capability_services,
                        ),
                    ),
                ),
            )
        )
        self.web_plugin = WebCapabilityPlugin(trace=self.trace)
        self.code_plugin = CodeCapabilityPlugin()
        self.image_plugin = ImageCapabilityPlugin()
        self.audio_plugin = AudioCapabilityPlugin()
        self.html_plugin = HtmlCapabilityPlugin(html_renderer)
        self.markdown_plugin = MarkdownCapabilityPlugin(markdown_renderer)
        self.plugin_manager.mount_all(
            (
                self.web_plugin,
                self.code_plugin,
                self.image_plugin,
                self.audio_plugin,
                self.html_plugin,
                self.markdown_plugin,
            )
        )
        self.web_provider = self.web_plugin.provider
        self.code_provider = self.code_plugin.provider
        self.image_provider = self.image_plugin.provider
        self.audio_provider = self.audio_plugin.provider
        self.audio_transcriber = self.audio_plugin.transcriber

        disabled_skills = {
            skill_name
            for skill_name, provider in (
                ("image-render", self.image_provider),
                ("audio-render", self.audio_provider),
            )
            if provider is None
        }
        self.plugin_manager.mount_all(
            (
                ServiceDefinition(
                    name="skills",
                    requires=frozenset(
                        {ServiceKey.CONFIG, ServiceKey.TOOL_REGISTRY}
                    ),
                    providers=(
                        ServiceProvider(
                            ServiceKey.SKILL_REGISTRY,
                            lambda context: SkillRegistry(
                                config.skills_dir,
                                context.require(ServiceKey.TOOL_REGISTRY),
                                disabled_skills=frozenset(disabled_skills),
                            ),
                        ),
                    ),
                ),
                ServiceDefinition(
                    name="agent-loop",
                    requires=frozenset(
                        {
                            ServiceKey.PROVIDER_CHAT,
                            ServiceKey.TOOL_REGISTRY,
                            ServiceKey.SKILL_REGISTRY,
                        }
                    ),
                    providers=(
                        ServiceProvider(
                            ServiceKey.CONVERSATION_DRIVER,
                            lambda context: _decorate_driver(
                                SessionAgentLoop(
                                    AgentLoopProfile(
                                        profile=AgentProfile.CONVERSATION,
                                        tool_role=AgentRole.CHAT,
                                        system_prompt=static_prompt(AgentRole.CHAT),
                                        persona=CHAT_PERSONA,
                                        completion_tools=frozenset(
                                            {"chat_turn_complete", "create_task"}
                                        ),
                                        max_steps=config.max_steps,
                                        source_step_limits=(
                                            (AgentInboxSource.TASK_EVENT, 6),
                                        ),
                                        turn_policy=ConversationTurnPolicy(),
                                    ),
                                    context.require(ServiceKey.PROVIDER_CHAT),
                                    context.require(ServiceKey.TOOL_REGISTRY),
                                    context.require(ServiceKey.SKILL_REGISTRY),
                                    trace=self.trace,
                                ),
                                conversation_driver_decorator,
                            ),
                        ),
                    ),
                ),
                ServiceDefinition(
                    name="task-jobs",
                    requires=frozenset(
                        {
                            ServiceKey.CONFIG,
                            ServiceKey.STORE,
                            ServiceKey.TASK_SERVICE,
                            ServiceKey.PROVIDER_TASK,
                            ServiceKey.TOOL_REGISTRY,
                            ServiceKey.SKILL_REGISTRY,
                            ServiceKey.TASK_EVENT_NOTIFIER,
                        }
                    ),
                    providers=(
                        ServiceProvider(
                            ServiceKey.TASK_RUNTIME,
                            lambda context: TaskRuntime(
                                config,
                                store=context.require(ServiceKey.STORE),
                                task_service=context.require(
                                    ServiceKey.TASK_SERVICE
                                ),
                                provider=context.require(
                                    ServiceKey.PROVIDER_TASK
                                ),
                                registry=context.require(
                                    ServiceKey.TOOL_REGISTRY
                                ),
                                skills=context.require(
                                    ServiceKey.SKILL_REGISTRY
                                ),
                                assistant_context=self._assistant_context_payload,
                                task_event_notifier=context.require(
                                    ServiceKey.TASK_EVENT_NOTIFIER
                                ),
                                handoff_payload=handoff_payload,
                                correction_payload=correction_payload,
                                record_payload=record_payload,
                                schedule_task=self.task_runtime_schedule,
                                trace=self.trace,
                                is_closing=lambda: self._closing,
                            ),
                        ),
                    ),
                ),
            )
        )
        self.skills = self.plugin_manager.service(ServiceKey.SKILL_REGISTRY)
        self.conversation_driver = self.plugin_manager.service(
            ServiceKey.CONVERSATION_DRIVER
        )
        self.task_runtime = self.plugin_manager.service(ServiceKey.TASK_RUNTIME)
        self.delivery_service = DeliveryService(
            store=self.store,
            task_service=self.task_service,
            artifacts=self.artifact_service,
            outbox=self.outbox,
            idempotency_key=self.idempotency_key,
            requester_id=task_requester_id,
            source_message_id=task_source_message_id,
            reply_ledger=self.reply_ledger,
        )
        self.chat_tools = ChatToolService(
            store=self.store,
            sessions=self.sessions,
            outbox=self.outbox,
            media_provider=self.media_provider,
            audio_transcriber=self.audio_transcriber,
            task_runtime=self.task_runtime,
            idempotency_key=self.idempotency_key,
            reply_ledger=self.reply_ledger,
        )
        self.task_commands = TaskCommandService(
            config,
            store=self.store,
            task_service=self.task_service,
            task_runtime=self.task_runtime,
            artifact_service=self.artifact_service,
            delivery_service=self.delivery_service,
            outbox=self.outbox,
            sessions=self.sessions,
            skills=self.skills,
            idempotency_key=self.idempotency_key,
            task_event_notifier=self.task_event_notifier,
            schedule_queued_task=self.task_runtime.schedule_queued_from_row,
        )
        build_registry(
            self.chat_tools,
            self.task_commands,
            sticker=self.stickers,
            trace=self.trace,
            registry=self.registry,
        )
        self.ingress = MessageIngress(
            config=config,
            client=client,
            adapter=self.adapter,
            store=self.store,
            sessions=self.sessions,
            outbox=self.outbox,
            restore_task_sources=self.task_runtime.restore_task_sources,
            assistant_message_ids=self._assistant_message_ids,
            task_source_message_ids=self.task_runtime.source_message_ids_for_scope,
            assistant_context=self._assistant_context_payload,
            record_payload=record_payload,
            snapshot_payload=snapshot_payload,
            task_agent_skill_catalog=self._task_agent_skill_catalog,
            conversation_agent=self._conversation_agent,
            collect_stickers=self.stickers.enqueue,
            trace=self.trace,
        )

    def task_runtime_schedule(self, handoff: TaskHandoff) -> None:
        self.task_runtime.schedule(handoff)

    async def start(self) -> None:
        self._closing = False
        Path(self.config.data_dir).mkdir(parents=True, exist_ok=True)
        await self.store.initialize(self.config.db_url)
        self.skills.discover()
        await self.outbox.start()
        await self.stickers.start()
        recovery = RuntimeRecovery(
            inbox=self.store,
            tasks=self.store,
        )
        await recovery.recover_claimed_agent_inputs()
        pending = await self.store.list_pending_agent_inputs()
        for item in pending:
            if item.agent_id.startswith("conversation:"):
                await self._conversation_agent(item.scope_id).wake()
        queued_tasks, interrupted_events = await recovery.recover_tasks()
        pending_task_events = await recovery.recover_task_event_notifications()
        events_to_notify = {
            event.event_id: event
            for event in (*interrupted_events, *pending_task_events)
        }
        for event in events_to_notify.values():
            await self.task_event_notifier.notify(event)
        for row in queued_tasks:
            await self.task_runtime.schedule_queued_from_row(
                row,
                run_id="runtime-recovery",
            )

    async def wait_for_idle(self, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            active = tuple(
                task
                for task in self.task_runtime.execution_tasks()
                if not task.done()
            )
            running_agents = tuple(
                agent
                for agent in (
                    *self._conversation_agents.values(),
                    *self.task_runtime.lifecycle_agents(),
                )
                if agent.status is AgentStatus.RUNNING
            )
            if not active and not running_agents:
                await self.outbox.when_idle()
                await self.stickers.when_idle()
                self.stickers.raise_background_failure()
                self.task_runtime.raise_background_failures()
                if self._conversation_agents or self.task_runtime.lifecycle_agents():
                    await asyncio.gather(
                        *(
                            agent.when_idle()
                            for agent in (
                                *self._conversation_agents.values(),
                                *self.task_runtime.lifecycle_agents(),
                            )
                        )
                    )
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Pumpkin runtime did not become idle")
            if active:
                await asyncio.wait(active, timeout=min(remaining, 0.05))
            else:
                await asyncio.sleep(min(remaining, 0.01))

    async def close(self) -> None:
        self._closing = True
        await self.stickers.close()
        await self.outbox.close()
        agents = (
            *self._conversation_agents.values(),
            *self.task_runtime.lifecycle_agents(),
        )
        if agents:
            await asyncio.gather(
                *(
                    agent.cancel("runtime shutdown", keep_inbox=True)
                    for agent in agents
                ),
                return_exceptions=True,
            )
        tasks = self.task_runtime.execution_tasks()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        closed: set[int] = set()
        for provider in (self.provider_chat, self.provider_task):
            close = getattr(provider, "close", None)
            if close is None or id(provider) in closed:
                continue
            closed.add(id(provider))
            await close()
        await self.plugin_manager.close()
        await self.store.close()

    async def handle_event(self, event: Any) -> None:
        await self.ingress.handle_event(event)

    def _conversation_agent(self, scope_id: str) -> Agent:
        agent = self._conversation_agents.get(scope_id)
        if agent is not None:
            return agent
        agent_id = f"conversation:{scope_id}"
        agent = Agent.create(
            store=self.store,
            driver=self.conversation_driver,
            agent_id=agent_id,
            scope_id=scope_id,
            profile=AgentProfile.CONVERSATION,
            session_id=f"{agent_id}:session",
        )
        self._conversation_agents[scope_id] = agent
        return agent

    async def _assistant_context_payload(self, scope_id: str) -> dict[str, Any]:
        recent = await self.store.recent_outbound_messages(scope_id)
        return {
            "assistant_identity": {
                "role": "assistant",
                "name": self.config.assistant.name,
                "user_id": str(self.client.self_id),
            },
            "recent_outbound_messages": [
                {
                    "outbound_id": item.outbound_id,
                    "message_ids": list(item.message_ids),
                    "text": item.text[:1200],
                    "segments": list(item.segments),
                    "source": item.source,
                    "sent_at": item.sent_at.isoformat(),
                }
                for item in recent
            ],
        }

    async def _assistant_message_ids(self, scope_id: str) -> set[str]:
        recent = await self.store.recent_outbound_messages(scope_id)
        return {
            message_id
            for item in recent
            for message_id in item.message_ids
        }

    def _task_agent_skill_catalog(self) -> list[dict[str, Any]]:
        authorized = (
            set(self.config.task.allowed_skills)
            if self.config.task.allowed_skills
            else None
        )
        return list(self.skills.catalog(AgentRole.TASK, authorized))

    def capability_task(self, context: ToolContext) -> TaskHandoff:
        if context.task_id is None or context.revision is None:
            raise TaskStateError("task context is missing")
        handoff = self.task_runtime.state_for_task(context.task_id)
        if (
            handoff.scope_id != context.scope_id
            or handoff.revision != context.revision
        ):
            raise TaskStateError("task scope or revision does not match")
        return handoff

    def idempotency_key(
        self,
        context: ToolContext,
        namespace: str,
        value: str,
    ) -> str:
        identity = "\x1f".join(
            (
                context.scope_id,
                context.task_id or "",
                str(context.revision or ""),
                value,
            )
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"{namespace}:{digest}"

    async def write_artifact(
        self,
        context: ToolContext,
        *,
        filename: str,
        kind: str,
        content: bytes,
        source_ref: Any | None = None,
        strategy_ref: Any | None = None,
    ) -> ArtifactRef:
        return await self.artifact_service.write(
            context,
            filename=filename,
            kind=kind,
            content=content,
            source_ref=source_ref,
            strategy_ref=strategy_ref,
        )


def _decorate_driver(
    driver: AgentDriver,
    decorator: Callable[[AgentDriver], AgentDriver] | None,
) -> AgentDriver:
    return decorator(driver) if decorator is not None else driver


def _provider(config: ModelConfig) -> ModelProvider:
    if not config.provider or not config.model or not config.base_url:
        return NullModelProvider()
    primary = _single_provider(
        provider=config.provider,
        model=config.model,
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=config.request_timeout_seconds,
        reasoning_effort=config.reasoning_effort,
        max_tokens=config.max_tokens,
    )
    providers: list[ModelProvider] = [primary]
    for index, base_url in enumerate(config.fallback_base_urls):
        providers.append(
            _single_provider(
                provider=(
                    config.fallback_providers[index]
                    if index < len(config.fallback_providers)
                    else config.provider
                ),
                model=config.model,
                base_url=base_url,
                api_key=config.api_key,
                timeout=config.request_timeout_seconds,
                reasoning_effort=config.reasoning_effort,
                max_tokens=config.max_tokens,
            )
        )
    return ResilientModelProvider(
        tuple(providers),
        max_retries=config.max_retries,
        empty_response_retries=config.empty_response_retries,
    )


def _single_provider(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key: str | None,
    timeout: float,
    reasoning_effort: str | None,
    max_tokens: int,
) -> ModelProvider:
    normalized = provider.strip().lower()
    if normalized in {"anthropic", "minimax"}:
        return AnthropicCompatibleProvider(
            provider=normalized,
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            reasoning_effort=reasoning_effort if normalized == "minimax" else None,
            max_tokens=max_tokens,
        )
    if normalized in {"openai", "openai-compatible"}:
        return OpenAICompatibleProvider(
            provider=normalized,
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_tokens=max_tokens,
        )
    raise ValueError(f"unsupported model provider: {provider}")

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from enum import StrEnum
from typing import Any, Protocol


Cleanup = Callable[[], Awaitable[None] | None]


class ServiceKey(StrEnum):
    CONFIG = "config"
    STORE = "store"
    TASK_SERVICE = "task_service"
    SESSION_SERVICE = "session_service"
    PROVIDER_CHAT = "provider_chat"
    PROVIDER_TASK = "provider_task"
    TOOL_REGISTRY = "tool_registry"
    SKILL_REGISTRY = "skill_registry"
    AGENT_POOL = "agent_pool"
    CONVERSATION_DRIVER = "conversation_driver"
    TASK_RUNTIME = "task_runtime"
    TASK_EVENT_NOTIFIER = "task_event_notifier"
    ARTIFACT_SERVICE = "artifact_service"
    NAPCAT_SENDER = "napcat_sender"
    NAPCAT_ADAPTER = "napcat_adapter"
    MEDIA_PROVIDER = "media_provider"
    TEXT_RENDERER = "text_renderer"
    OUTBOX = "outbox"
    REPLY_LEDGER = "reply_ledger"
    CAPABILITY_HOST = "capability_host"
    WEB_PROVIDER = "web_provider"
    CODE_PROVIDER = "code_provider"
    IMAGE_PROVIDER = "image_provider"
    AUDIO_PROVIDER = "audio_provider"
    AUDIO_TRANSCRIBER = "audio_transcriber"


class PluginError(RuntimeError):
    pass


class Plugin(Protocol):
    name: str
    requires: frozenset[ServiceKey]

    def setup(self, context: PluginContext) -> None: ...


class PluginContext:
    def __init__(
        self,
        services: dict[ServiceKey, Any],
        *,
        owner: str,
    ) -> None:
        self._services = services
        self._owner = owner
        self._effects: list[Cleanup] = []

    def require(self, key: ServiceKey) -> Any:
        try:
            return self._services[key]
        except KeyError as exc:
            raise PluginError(
                f"plugin {self._owner} requires missing service {key.value}"
            ) from exc

    def provide(self, key: ServiceKey, value: Any) -> None:
        if key in self._services:
            raise PluginError(
                f"plugin {self._owner} cannot replace service {key.value}"
            )
        self._services[key] = value

        def remove() -> None:
            if self._services.get(key) is value:
                del self._services[key]

        self.effect(remove)

    def effect(self, cleanup: Cleanup) -> None:
        self._effects.append(cleanup)

    async def rollback(self) -> None:
        errors: list[BaseException] = []
        for cleanup in reversed(self._effects):
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        self._effects.clear()
        if errors:
            raise PluginError(
                f"plugin {self._owner} cleanup failed: {errors[0]}"
            ) from errors[0]


class PluginManager:
    def __init__(
        self,
        initial_services: Mapping[ServiceKey, Any] | None = None,
    ) -> None:
        self._services = dict(initial_services or {})
        self._mounted: list[tuple[Plugin, PluginContext]] = []
        self._closed = False

    def service(self, key: ServiceKey) -> Any:
        try:
            return self._services[key]
        except KeyError as exc:
            raise PluginError(f"service is not available: {key.value}") from exc

    def mount_all(self, plugins: Iterable[Plugin]) -> None:
        if self._closed:
            raise PluginError("plugin manager is closed")
        pending = list(plugins)
        names = [plugin.name for plugin in pending]
        if len(names) != len(set(names)):
            raise PluginError("duplicate plugin name")
        while pending:
            ready = [
                plugin
                for plugin in pending
                if plugin.requires.issubset(self._services)
            ]
            if not ready:
                details = ", ".join(
                    f"{plugin.name}=["
                    + ",".join(
                        sorted(
                            key.value
                            for key in plugin.requires
                            if key not in self._services
                        )
                    )
                    + "]"
                    for plugin in pending
                )
                raise PluginError(f"unresolved plugin dependencies: {details}")
            for plugin in ready:
                context = PluginContext(self._services, owner=plugin.name)
                try:
                    plugin.setup(context)
                except Exception:
                    self._rollback_sync(context)
                    raise
                self._mounted.append((plugin, context))
                pending.remove(plugin)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        for _, context in reversed(self._mounted):
            try:
                await context.rollback()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        self._mounted.clear()
        if errors:
            raise PluginError(f"plugin shutdown failed: {errors[0]}") from errors[0]

    @staticmethod
    def _rollback_sync(context: PluginContext) -> None:
        for cleanup in reversed(context._effects):
            result = cleanup()
            if inspect.isawaitable(result):
                raise PluginError(
                    "async cleanup is not supported during synchronous setup rollback"
                )
        context._effects.clear()

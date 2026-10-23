from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .plugins import PluginContext, ServiceKey


ServiceFactory = Callable[[PluginContext], Any]


@dataclass(frozen=True, slots=True)
class ServiceProvider:
    key: ServiceKey
    factory: ServiceFactory


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    name: str
    requires: frozenset[ServiceKey]
    providers: tuple[ServiceProvider, ...]

    def setup(self, context: PluginContext) -> None:
        provided: set[ServiceKey] = set()
        for provider in self.providers:
            if provider.key in provided:
                raise ValueError(
                    f"service definition {self.name} provides "
                    f"{provider.key.value} more than once"
                )
            context.provide(provider.key, provider.factory(context))
            provided.add(provider.key)

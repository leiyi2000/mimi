from dataclasses import dataclass
from pathlib import Path
import tomllib


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    description: str
    badge: str = ""


@dataclass(frozen=True)
class Plugin:
    key: str
    name: str
    description: str
    order: int
    commands: tuple[Command, ...]


class CommandCatalog:
    def __init__(self, plugins_dir: Path) -> None:
        self.plugins_dir = plugins_dir

    def load(self) -> tuple[Plugin, ...]:
        plugins = tuple(
            self._load_file(path)
            for path in sorted(self.plugins_dir.glob("*/commands.toml"))
        )
        self._validate(plugins)
        return tuple(sorted(plugins, key=lambda item: (item.order, item.name)))

    def find(self, query: str) -> tuple[Plugin, ...]:
        plugins = self.load()
        needle = query.strip().casefold()
        if not needle:
            return plugins
        return tuple(
            plugin
            for plugin in plugins
            if needle in plugin.key.casefold() or needle in plugin.name.casefold()
        )

    @staticmethod
    def _load_file(path: Path) -> Plugin:
        with path.open("rb") as file:
            data = tomllib.load(file)

        metadata = data.get("plugin")
        entries = data.get("commands")
        if not isinstance(metadata, dict) or not isinstance(entries, list):
            raise CatalogError(f"invalid command manifest: {path}")

        try:
            commands = tuple(
                Command(
                    name=str(entry["name"]).strip(),
                    usage=str(entry["usage"]).strip(),
                    description=str(entry["description"]).strip(),
                    badge=str(entry.get("badge", "")).strip(),
                )
                for entry in entries
            )
            return Plugin(
                key=str(metadata["key"]).strip(),
                name=str(metadata["name"]).strip(),
                description=str(metadata.get("description", "")).strip(),
                order=int(metadata.get("order", 100)),
                commands=commands,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError(f"invalid command manifest: {path}") from exc

    @staticmethod
    def _validate(plugins: tuple[Plugin, ...]) -> None:
        plugin_keys: set[str] = set()
        command_names: dict[str, str] = {}
        for plugin in plugins:
            key = plugin.key.casefold()
            if not key or key in plugin_keys:
                raise CatalogError(f"duplicate or empty plugin key: {plugin.key!r}")
            plugin_keys.add(key)
            if not plugin.name or not plugin.commands:
                raise CatalogError(f"plugin {plugin.key!r} has no display content")

            for command in plugin.commands:
                normalized = command.name.casefold()
                if not normalized or not command.usage or not command.description:
                    raise CatalogError(f"incomplete command in plugin {plugin.key!r}")
                owner = command_names.get(normalized)
                if owner is not None:
                    raise CatalogError(
                        f"duplicate command {command.name!r}: {owner}, {plugin.key}"
                    )
                command_names[normalized] = plugin.key

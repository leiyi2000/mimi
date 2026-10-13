from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class PromptError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PromptCatalog:
    system: str
    personality: str
    napcat: str

    @classmethod
    def load(cls, root: Path) -> PromptCatalog:
        return cls(
            system=cls._read(root / "system.md"),
            personality=cls._read(root / "personality.md"),
            napcat=cls._read(root / "napcat.md"),
        )

    def fixed_prompt(self) -> str:
        return f"{self.system}\n\n{self.personality}\n\n{self.napcat}"

    @staticmethod
    def _read(path: Path) -> str:
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PromptError(f"failed to read prompt: {path.name}") from exc
        if not content:
            raise PromptError(f"prompt must not be empty: {path.name}")
        return content

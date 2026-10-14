from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path


SECTION_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class PromptError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PromptCatalog:
    system: str
    execution: str
    personality: str
    napcat: str

    @classmethod
    def load(cls, root: Path) -> PromptCatalog:
        return cls(
            system=cls._read(root / "system.md"),
            execution=cls._read(root / "execution.md"),
            personality=cls._read(root / "personality.md"),
            napcat=cls._read(root / "napcat.md"),
        )

    def fixed_prompt(self) -> str:
        return "\n\n".join(
            (
                self.system,
                self.section("execution_protocol", self.execution),
                self.section("conversation_style", self.personality),
                self.section("napcat_protocol", self.napcat),
            )
        )

    def build(
        self,
        *,
        capability_catalog: str,
        memory_context: str,
        active_skills: str,
        task_state: str = "",
        run_directive: str = "",
    ) -> str:
        return "\n\n".join(
            part
            for part in (
                self.fixed_prompt(),
                self.section("capability_catalog", capability_catalog),
                self.section(
                    "memory_context",
                    memory_context,
                    untrusted=True,
                ),
                self.section("active_skills", active_skills),
                self.section("task_state", task_state),
                self.section("run_directive", run_directive),
            )
            if part
        )

    @staticmethod
    def section(
        name: str,
        content: str,
        *,
        untrusted: bool = False,
    ) -> str:
        if not content:
            return ""
        if not SECTION_NAME.fullmatch(name):
            raise ValueError(f"invalid prompt section name: {name}")
        body = html.escape(content, quote=False) if untrusted else content
        return f"<{name}>\n{body}\n</{name}>"

    @staticmethod
    def _read(path: Path) -> str:
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PromptError(f"failed to read prompt: {path.name}") from exc
        if not content:
            raise PromptError(f"prompt must not be empty: {path.name}")
        return content

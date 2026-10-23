from __future__ import annotations

import asyncio

from tortoise import Tortoise

from .repositories.agent import AgentInboxRepositoryMixin, AgentSessionRepositoryMixin
from .repositories.artifacts import ArtifactRepositoryMixin
from .repositories.conversation import ConversationRepositoryMixin
from .repositories.idempotency import IdempotencyRepositoryMixin
from .repositories.messages import MessageRepositoryMixin
from .repositories.outbox import OutboxRepositoryMixin
from .repositories.tasks import TaskRepositoryMixin


class PumpkinStore(
    AgentSessionRepositoryMixin,
    AgentInboxRepositoryMixin,
    OutboxRepositoryMixin,
    MessageRepositoryMixin,
    ConversationRepositoryMixin,
    TaskRepositoryMixin,
    ArtifactRepositoryMixin,
    IdempotencyRepositoryMixin,
):
    """Compatibility composition root for Pumpkin's domain repositories.

    Runtime services depend on repository Protocols. This class only combines
    the SQLite implementations and owns database lifecycle for the application.
    """

    def __init__(self) -> None:
        self._archive_lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._inbox_locks: dict[str, asyncio.Lock] = {}
        self._outbox_lock = asyncio.Lock()

    async def initialize(self, db_url: str) -> None:
        await Tortoise.init(
            db_url=db_url,
            modules={"models": ["persistence.models"]},
        )
        await self._configure_sqlite()
        await Tortoise.generate_schemas(safe=True)
        await self._ensure_compatibility_schema()

    @staticmethod
    async def _configure_sqlite() -> None:
        connection = Tortoise.get_connection("default")
        if getattr(connection.capabilities, "dialect", "") != "sqlite":
            return
        await connection.execute_script(
            "PRAGMA journal_mode=DELETE;"
            "PRAGMA synchronous=FULL;"
        )

    async def _ensure_compatibility_schema(self) -> None:
        connection = Tortoise.get_connection("default")
        if getattr(connection.capabilities, "dialect", "") != "sqlite":
            return
        columns = await connection.execute_query_dict(
            "PRAGMA table_info(pumpkin_tasks)"
        )
        if not columns:
            return
        names = {str(column.get("name")) for column in columns}
        if "source_message_ids" not in names:
            await connection.execute_script(
                "ALTER TABLE pumpkin_tasks "
                "ADD COLUMN source_message_ids JSON NOT NULL DEFAULT '[]'"
            )
        if "supplement_message_ids" not in names:
            await connection.execute_script(
                "ALTER TABLE pumpkin_tasks "
                "ADD COLUMN supplement_message_ids JSON NOT NULL DEFAULT '[]'"
            )

    async def close(self) -> None:
        if Tortoise._inited:
            await Tortoise.close_connections()

    async def recover_claimed_agent_inputs(self) -> int:
        """Backward-compatible test and migration entry point."""
        from runtime.recovery import RuntimeRecovery

        return await RuntimeRecovery(
            inbox=self,
            tasks=self,
        ).recover_claimed_agent_inputs()

    async def recover_tasks(self):
        """Backward-compatible test and migration entry point."""
        from runtime.recovery import RuntimeRecovery

        return await RuntimeRecovery(
            inbox=self,
            tasks=self,
        ).recover_tasks()

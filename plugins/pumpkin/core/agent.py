from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from .domain import (
    AgentEventType,
    AgentInboxItem,
    AgentInboxSource,
    AgentInboxTarget,
    AgentProfile,
    AgentStatus,
    ToolExecutionStatus,
)
from persistence.repositories import AgentStore
from .session import AgentSession


class AgentDriver(Protocol):
    async def run_turn(
        self,
        agent: Agent,
        item: AgentInboxItem,
    ) -> None: ...


@dataclass(slots=True)
class Agent:
    agent_id: str
    scope_id: str
    profile: AgentProfile
    session: AgentSession
    store: AgentStore
    driver: AgentDriver
    status: AgentStatus = field(default=AgentStatus.IDLE, init=False)
    _worker: asyncio.Task[None] | None = field(default=None, init=False)
    _lifecycle_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
    )
    _idle: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _cancel_cause: str | None = field(default=None, init=False)
    _failure: BaseException | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._idle.set()

    @classmethod
    def create(
        cls,
        *,
        store: AgentStore,
        driver: AgentDriver,
        agent_id: str,
        scope_id: str,
        profile: AgentProfile,
        session_id: str | None = None,
    ) -> Agent:
        session = AgentSession(
            store=store,
            agent_id=agent_id,
            session_id=session_id or f"{agent_id}:session",
            scope_id=scope_id,
        )
        return cls(
            agent_id=agent_id,
            scope_id=scope_id,
            profile=profile,
            session=session,
            store=store,
            driver=driver,
        )

    async def send(
        self,
        payload: dict[str, Any],
        *,
        source_type: AgentInboxSource,
        target: AgentInboxTarget = AgentInboxTarget.NEXT_TURN,
        item_id: str | None = None,
        wakeup: bool = True,
    ) -> AgentInboxItem:
        item = await self.store.enqueue_agent_input(
            item_id=item_id or uuid.uuid4().hex,
            agent_id=self.agent_id,
            session_id=self.session.session_id,
            scope_id=self.scope_id,
            target=target,
            source_type=source_type.value,
            payload=payload,
        )
        if wakeup:
            await self.wake()
        return item

    async def wake(self) -> None:
        async with self._lifecycle_lock:
            if self._worker is not None and not self._worker.done():
                return
            self._cancel_cause = None
            self._failure = None
            self.status = AgentStatus.RUNNING
            self._idle.clear()
            self._worker = asyncio.create_task(
                self._run(),
                name=f"pumpkin-agent:{self.agent_id}",
            )

    async def cancel(
        self,
        cause: str,
        *,
        keep_inbox: bool = True,
    ) -> None:
        async with self._lifecycle_lock:
            self._cancel_cause = cause
            worker = self._worker
            if worker is not None and not worker.done():
                worker.cancel()
        if worker is not None:
            try:
                await worker
            except asyncio.CancelledError:
                pass
        if not keep_inbox:
            pending = await self.store.list_pending_agent_inputs(self.agent_id)
            for item in pending:
                await self.store.discard_agent_input(item.item_id)

    async def when_idle(self) -> None:
        while True:
            await self._idle.wait()
            if self.status is AgentStatus.IDLE:
                if self._failure is not None:
                    raise self._failure
                return

    async def _run(self) -> None:
        try:
            while True:
                item = await self.store.claim_agent_input(self.agent_id)
                if item is None:
                    async with self._lifecycle_lock:
                        pending = await self.store.list_pending_agent_inputs(
                            self.agent_id
                        )
                        if pending:
                            continue
                        self.status = AgentStatus.IDLE
                        self._worker = None
                        self._idle.set()
                        return
                await self.driver.run_turn(self, item)
                if not await self.store.complete_agent_input(item.item_id):
                    raise RuntimeError(
                        f"claimed inbox item was not completed: {item.item_id}"
                    )
        except asyncio.CancelledError:
            projection = await self.session.project()
            unfinished_tool = any(
                execution.status
                in {
                    ToolExecutionStatus.NOT_STARTED,
                    ToolExecutionStatus.IN_PROGRESS,
                }
                for execution in projection.tool_executions.values()
            )
            if projection.open_turn_id is not None and not unfinished_tool:
                await self.session.append(
                    AgentEventType.TURN_CANCELLED,
                    {"cause": self._cancel_cause or "cancelled"},
                    turn_id=projection.open_turn_id,
                    step_id=projection.open_step_id,
                )
            raise
        except BaseException as exc:  # noqa: BLE001
            self._failure = exc
        finally:
            async with self._lifecycle_lock:
                if self._worker is asyncio.current_task():
                    self.status = AgentStatus.IDLE
                    self._worker = None
                    self._idle.set()

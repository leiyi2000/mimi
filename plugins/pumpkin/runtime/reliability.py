from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from core.agent import Agent, AgentDriver
from core.domain import (
    AgentEventType,
    AgentInboxItem,
    AgentInboxSource,
    OutboxStatus,
)
from .app import PumpkinRuntime


log = logging.getLogger(__name__)


@dataclass(slots=True)
class TurnReceiptDriver:
    delegate: AgentDriver
    runtime: ReliablePumpkinRuntime

    async def run_turn(self, agent: Agent, item: AgentInboxItem) -> None:
        existing_events = await agent.session.events()
        first_sequence = existing_events[-1].sequence if existing_events else 0
        try:
            await self.delegate.run_turn(agent, item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception(
                "conversation turn crashed item=%s error=%s",
                item.item_id,
                type(exc).__name__,
            )
            await self._record_failure(agent, item, type(exc).__name__)

        new_events = tuple(
            event
            for event in await agent.session.events()
            if event.sequence > first_sequence
        )
        failed = next(
            (
                event
                for event in reversed(new_events)
                if event.event_type is AgentEventType.TURN_FAILED
            ),
            None,
        )
        if failed is None:
            return
        await self.runtime.send_turn_failure_receipt(
            item,
            source_event_id=failed.turn_id or failed.event_id,
        )

    @staticmethod
    async def _record_failure(
        agent: Agent,
        item: AgentInboxItem,
        reason: str,
    ) -> None:
        projection = await agent.session.project()
        turn_id = projection.open_turn_id
        if turn_id is None:
            turn_id = uuid.uuid4().hex
            await agent.session.append(
                AgentEventType.TURN_STARTED,
                {
                    "inbox_item_id": item.item_id,
                    "source_type": item.source_type,
                    "profile": agent.profile.value,
                },
                turn_id=turn_id,
            )
            projection = await agent.session.project()
        if projection.open_step_id is not None:
            await agent.session.append(
                AgentEventType.STEP_COMPLETED,
                {"status": "failed", "reason": reason},
                turn_id=turn_id,
                step_id=projection.open_step_id,
            )
        await agent.session.append(
            AgentEventType.TURN_FAILED,
            {"reason": reason, "error_code": "TURN_RUNTIME_FAILURE"},
            turn_id=turn_id,
        )


class ReliablePumpkinRuntime(PumpkinRuntime):
    """Reliability policy around the persisted Pumpkin runtime."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["conversation_driver_decorator"] = (
            lambda driver: TurnReceiptDriver(driver, self)
        )
        super().__init__(*args, **kwargs)

    async def send_turn_failure_receipt(
        self,
        item: AgentInboxItem,
        *,
        source_event_id: str,
    ) -> None:
        if item.source_type is not AgentInboxSource.USER_MESSAGE:
            return
        result = await self.outbox.submit(
            idempotency_key=f"turn-failed:{item.item_id}",
            scope_id=item.scope_id,
            source_event_id=source_event_id,
            payload={
                "text": "刚才这条消息处理失败了，请稍后再试一次。",
                "original_text": "刚才这条消息处理失败了，请稍后再试一次。",
                "segments": [],
                "reply_to_message_id": item.payload.get("message_id"),
                "source": "turn_failure_receipt",
            },
        )
        if result.status is not OutboxStatus.SENT:
            log.error(
                "turn failure receipt was not sent item=%s status=%s error=%s",
                item.item_id,
                result.status.value,
                result.error_code,
            )
            return
        await self.sessions.renew(item.scope_id)

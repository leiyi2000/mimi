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
    TaskEventType,
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
        terminal_turn_ids: set[str] = set()
        try:
            try:
                await self.delegate.run_turn(agent, item)
            except asyncio.CancelledError:
                projection = await agent.session.project()
                if projection.open_turn_id is not None:
                    self.runtime.reply_ledger.clear(projection.open_turn_id)
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
            terminal_turn_ids = {
                event.turn_id
                for event in new_events
                if event.turn_id is not None
                and event.event_type
                in {
                    AgentEventType.TURN_COMPLETED,
                    AgentEventType.TURN_FAILED,
                    AgentEventType.TURN_CANCELLED,
                }
            }
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
            if any(
                event.event_type is AgentEventType.OUTBOX_SENT
                and event.turn_id == failed.turn_id
                for event in new_events
            ):
                log.warning(
                    "failure receipt suppressed after confirmed reply item=%s",
                    item.item_id,
                )
                return
            await self.runtime.send_turn_failure_receipt(
                item,
                source_event_id=failed.turn_id or failed.event_id,
            )
        finally:
            for turn_id in terminal_turn_ids:
                self.runtime.reply_ledger.clear(turn_id)

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
        if item.source_type is AgentInboxSource.USER_MESSAGE:
            text = "刚才这条消息处理失败了，请稍后再试一次。"
            idempotency_key = f"turn-failed:{item.item_id}"
            source = "turn_failure_receipt"
            reply_to_message_id = item.payload.get("message_id")
        elif item.source_type is AgentInboxSource.TASK_EVENT:
            text = _task_event_fallback_text(item)
            idempotency_key = f"task-event-turn-failed:{item.item_id}"
            source = "task_event_failure_receipt"
            reply_to_message_id = None
        else:
            return
        result = await self.outbox.submit(
            idempotency_key=idempotency_key,
            scope_id=item.scope_id,
            source_event_id=source_event_id,
            payload={
                "text": text,
                "original_text": text,
                "segments": [],
                "reply_to_message_id": reply_to_message_id,
                "source": source,
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


def _task_event_fallback_text(item: AgentInboxItem) -> str:
    event = item.payload.get("task_event")
    event_type = event.get("event_type") if isinstance(event, dict) else None
    return {
        TaskEventType.FAILED.value: (
            "这个任务执行失败了，暂时没有生成可交付结果。"
        ),
        TaskEventType.WAITING_FOR_INPUT.value: (
            "这个任务还需要补充信息，请重新说明要求或查询任务状态。"
        ),
        TaskEventType.PAUSED.value: "这个任务已暂停，请稍后查询任务状态。",
        TaskEventType.COMPLETED.value: (
            "任务已经完成，但结果通知失败了，请重新查询任务状态。"
        ),
    }.get(
        str(event_type),
        "任务状态已经更新，但通知处理失败了，请重新查询任务状态。",
    )

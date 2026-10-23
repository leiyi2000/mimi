from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TurnReplyLedger:
    """Track successful outbound message IDs for each Agent Turn."""

    _message_ids: dict[str, list[str]] = field(default_factory=dict)

    def record(self, run_id: str, message_ids: tuple[str, ...] | list[str]) -> None:
        if not message_ids:
            return
        recorded = self._message_ids.setdefault(run_id, [])
        for message_id in message_ids:
            value = str(message_id)
            if value and value not in recorded:
                recorded.append(value)

    def message_ids(self, run_id: str) -> tuple[str, ...]:
        return tuple(self._message_ids.get(run_id, ()))

    def clear(self, run_id: str) -> None:
        self._message_ids.pop(run_id, None)

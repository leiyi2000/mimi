from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal


RunStatus = Literal[
    "completed",
    "partially_completed",
    "failed",
]


@dataclass(frozen=True, slots=True)
class RunEvent:
    kind: str
    round_index: int | None = None
    tool_name: str | None = None
    duration_ms: int | None = None
    error_code: str | None = None


@dataclass(slots=True)
class RunTrace:
    group_id: int
    message_id: int
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    status: RunStatus = "failed"
    model_rounds: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    plan_sent: bool = False
    error_code: str | None = None
    events: list[RunEvent] = field(default_factory=list)

    def finish(
        self,
        status: RunStatus,
        *,
        error_code: str | None = None,
    ) -> None:
        self.status = status
        self.error_code = error_code
        self.finished_at = time.monotonic()

    def as_log(self) -> dict[str, object]:
        finished_at = self.finished_at or time.monotonic()
        return {
            "run_id": self.run_id,
            "group_id": self.group_id,
            "message_id": self.message_id,
            "status": self.status,
            "duration_ms": int((finished_at - self.started_at) * 1000),
            "model_rounds": self.model_rounds,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "plan_sent": self.plan_sent,
            "error_code": self.error_code,
            "events": [asdict(event) for event in self.events],
        }

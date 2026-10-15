from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


TopicAction = Literal["ignore", "absorb", "respond"]


@dataclass(frozen=True, slots=True)
class TopicCandidate:
    summary: str
    recent_turns: tuple[str, ...]
    user_id: int
    nickname: str
    text: str
    has_images: bool
    has_emojis: bool
    seconds_since_activity: float


@dataclass(frozen=True, slots=True)
class TopicGateDecision:
    action: TopicAction
    confidence: float
    reason: str


class TopicGate(Protocol):
    async def classify(self, candidate: TopicCandidate) -> TopicGateDecision: ...

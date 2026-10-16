from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


TopicAction = Literal["ignore", "absorb", "respond"]
TaskRelation = Literal[
    "new",
    "continue",
    "correction",
    "supplement",
    "clarify",
    "unrelated",
    "unclear",
]


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


@dataclass(frozen=True, slots=True)
class TaskRelationCandidate:
    task_context: str
    topic_summary: str
    recent_turns: tuple[str, ...]
    user_id: int
    nickname: str
    text: str
    has_images: bool
    has_emojis: bool
    seconds_since_activity: float


@dataclass(frozen=True, slots=True)
class TaskRelationDecision:
    relation: TaskRelation
    confidence: float
    constraint_patch: tuple[str, ...] = ()
    reason: str = ""


class TaskRelationRouter(Protocol):
    async def classify_relation(
        self,
        candidate: TaskRelationCandidate,
    ) -> TaskRelationDecision: ...

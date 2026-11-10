import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


PROGRAM_HISTORY_MAX_BYTES = 24 * 1024
BROWSER_OPERATION_FINGERPRINT = "goal-only"


class BrowserTaskStatus(StrEnum):
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"


class BrowserExpectedOutput(StrEnum):
    FACTS = "facts"
    SUMMARY = "summary"
    LINKS = "links"
    SCREENSHOT = "screenshot"
    PDF = "pdf"


class BrowserInteractionMode(StrEnum):
    READ_ONLY = "read_only"
    PUBLIC_INTERACTION = "public_interaction"
    PUBLIC_SUBMISSION = "public_submission"

    @property
    def operation_fingerprint(self) -> str:
        return hashlib.sha256(self.value.encode()).hexdigest()


class BrowserArtifactMode(StrEnum):
    NONE = "none"
    SCREENSHOT = "screenshot"
    PDF = "pdf"


class BrowserRequiredCapability(StrEnum):
    PLAYWRIGHT = "playwright"
    VIEWPORT_SCREENSHOT = "viewport_screenshot"
    FULL_PAGE_SCREENSHOT = "full_page_screenshot"
    FULL_PAGE_CONTENT = "full_page_content"
    PDF = "pdf"


@dataclass(frozen=True, slots=True)
class BrowserArtifact:
    kind: BrowserArtifactMode
    content: bytes
    media_type: str
    run_id: str = ""
    resource_version: int = 0
    evidence_id: str = ""
    finalization_id: str = ""
    observed_after_action_seq: int = 0
    last_confirmed_action_seq: int = 0
    execution_id: str = ""


@dataclass(frozen=True, slots=True)
class BrowserEvidenceRequirement:
    channel: str
    text: str

    @classmethod
    def from_payload(cls, value: Any, allowed_channels: frozenset[str]) -> "BrowserEvidenceRequirement":
        if (
            not isinstance(value, dict) or set(value) != {"channel", "text"}
            or value["channel"] not in allowed_channels or not isinstance(value["text"], str)
            or not 1 <= len(value["text"].strip()) <= 500
        ):
            raise ValueError("invalid browser evidence requirement")
        return cls(value["channel"], value["text"].strip())


@dataclass(frozen=True, slots=True)
class BrowserTaskContract:
    goal: str
    start_url: str
    expected_output: BrowserExpectedOutput
    interaction_mode: BrowserInteractionMode
    artifact_mode: BrowserArtifactMode
    original_requests: tuple[str, ...] = ()
    required_capabilities: tuple[BrowserRequiredCapability, ...] = ()
    required_evidence: tuple[BrowserEvidenceRequirement, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "goal": self.goal, "start_url": self.start_url,
            "expected_output": self.expected_output.value,
            "interaction_mode": self.interaction_mode.value,
            "artifact_mode": self.artifact_mode.value,
            "required_capabilities": [item.value for item in self.required_capabilities],
            "required_evidence": [{"channel": item.channel, "text": item.text} for item in self.required_evidence],
        }


@dataclass(frozen=True, slots=True)
class BrowserRunResult:
    status: BrowserTaskStatus
    summary: str
    facts: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    artifacts: tuple[BrowserArtifact, ...] = ()
    error_code: str | None = None
    retryable: bool = False
    steps: int = 0
    command_count: int = 0
    execution_report: dict[str, Any] = field(default_factory=dict)


class BrowserAgentError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code, self.retryable = code, retryable


class BrowserArgumentError(BrowserAgentError):
    pass


@dataclass(slots=True)
class BrowserExecutionState:
    run_id: str = ""
    engine: str = ""
    playwright_version: str = ""
    chromium_version: str = ""
    decision_count: int = 0
    program_count: int = 0
    sdk_call_count: int = 0
    current_observation: dict[str, Any] = field(default_factory=dict)
    last_program: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    last_confirmed_action_seq: int = 0
    outcome_unknown: bool = False
    decision_errors: int = 0
    task_contract: dict[str, Any] = field(default_factory=dict)
    program_history: list[dict[str, Any]] = field(default_factory=list)
    program_history_omitted: int = 0
    finish_attempts: int = 0
    finish_error: dict[str, Any] = field(default_factory=dict)

    def remember_program(self, program: str, result: dict[str, Any]) -> None:
        entry = {
            "decision": self.decision_count,
            "program": program,
            "result": {key: result[key] for key in (
                "execution_id", "status", "return_value", "error", "program_error", "sdk_call_count",
            ) if key in result},
        }
        if len(json.dumps([entry], ensure_ascii=False).encode()) > PROGRAM_HISTORY_MAX_BYTES:
            entry.pop("program")
            entry["program_digest"] = hashlib.sha256(program.encode()).hexdigest()
            entry["program_omitted"] = True
        if len(json.dumps([entry], ensure_ascii=False).encode()) > PROGRAM_HISTORY_MAX_BYTES:
            entry["result"].pop("return_value", None)
            entry["return_value_omitted"] = True
        self.program_history.append(entry)
        while len(json.dumps(self.program_history, ensure_ascii=False).encode()) > PROGRAM_HISTORY_MAX_BYTES:
            self.program_history.pop(0)
            self.program_history_omitted += 1

    def observe(self, snapshot: Any) -> None:
        if (
            not isinstance(snapshot, dict) or not isinstance(snapshot.get("observation_id"), str)
            or any(type(snapshot.get(key)) is not int for key in (
                "document_epoch", "resource_version", "observed_after_action_seq",
            ))
            or not isinstance(snapshot.get("elements"), (list, tuple))
        ):
            raise BrowserAgentError("BROWSER_WORKER_PROTOCOL_ERROR", "浏览器页面观察格式无效。")
        self.current_observation = snapshot

    def record(self, result: dict[str, Any]) -> None:
        self.last_program = result
        self.sdk_call_count += result.get("sdk_call_count", 0)
        actions = {item["action_sequence"]: item for item in self.actions}
        actions.update({item["action_sequence"]: item for item in result.get("action_ledger") or []})
        self.actions = [actions[key] for key in sorted(actions)]
        self.last_confirmed_action_seq = result.get("last_confirmed_action_seq", 0)
        self.outcome_unknown |= bool(result.get("outcome_unknown") or result.get("status") == "outcome_unknown")
        if result.get("final_observation") is None:
            self.current_observation = {}
        else:
            self.observe(result["final_observation"])

    def report(self, *, goal_verified: bool = False) -> dict[str, Any]:
        current = self.current_observation
        return {
            "controller": "browser_code_execution", "run_id": self.run_id,
            "engine": self.engine, "playwright_version": self.playwright_version,
            "chromium_version": self.chromium_version,
            "document_epoch": current.get("document_epoch", 0),
            "page_version": current.get("resource_version", 0),
            "blocked_requests": current.get("blocked_requests", 0),
            "program_count": self.program_count, "sdk_call_count": self.sdk_call_count,
            "last_program": {
                "execution_id": self.last_program.get("execution_id"),
                "status": self.last_program.get("status"),
                "error": self.last_program.get("program_error") or self.last_program.get("error"),
            },
            "last_confirmed_action_seq": self.last_confirmed_action_seq,
            "last_observation": {
                "tool": "browser_snapshot", "observation_id": current.get("observation_id"),
                "page_version": current.get("resource_version"),
                "observed_after_action_seq": current.get("observed_after_action_seq"),
                "text": str(current.get("visible_text", ""))[:2_000],
                "dialog": current.get("dialog"), "stale": bool(current.get("dialog")),
            } if current else {},
            "actions": self.actions,
            "outcome_unknown": self.outcome_unknown,
            "task_contract": self.task_contract, "goal_verified": goal_verified,
        }

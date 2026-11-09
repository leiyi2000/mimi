from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


PROTOCOL_VERSION = 3
MAX_FRAME_BYTES = 32 * 1024 * 1024
MAX_OBSERVATION_TEXT_CHARS = 48_000


class WorkerOperation(StrEnum):
    HEALTH = "health"
    CREATE_RUN = "create_run"
    EXECUTE_PROGRAM = "execute_program"
    CAPTURE = "capture"
    GET_RUN = "get_run"
    CANCEL_RUN = "cancel_run"
    HEARTBEAT = "heartbeat"


class BrowserCommandStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class BrowserCommandExecution(StrEnum):
    NOT_STARTED = "not_started"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"


class BrowserCommandEffect(StrEnum):
    CONFIRMED = "confirmed"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class WorkerErrorCode(StrEnum):
    AUTHENTICATION_FAILED = "BROWSER_WORKER_AUTHENTICATION_FAILED"
    CAPACITY_EXHAUSTED = "BROWSER_CAPACITY_EXHAUSTED"
    INVALID_REQUEST = "BROWSER_WORKER_INVALID_REQUEST"
    RUN_NOT_FOUND = "BROWSER_RUN_NOT_FOUND"
    RUN_CAPABILITY_INVALID = "BROWSER_RUN_CAPABILITY_INVALID"
    ARGUMENT_INVALID = "BROWSER_ARGUMENT_INVALID"
    OUTCOME_UNKNOWN = "BROWSER_OUTCOME_UNKNOWN"
    URL_REJECTED = "BROWSER_URL_REJECTED"
    TOOL_REJECTED = "BROWSER_TOOL_REJECTED"
    CAPABILITY_UNSUPPORTED = "BROWSER_CAPABILITY_UNSUPPORTED"
    ENGINE_FAILED = "BROWSER_ENGINE_FAILED"
    TIMEOUT = "BROWSER_TIMEOUT"
    CAPTURE_TOO_LARGE = "BROWSER_CAPTURE_TOO_LARGE"
    OUTPUT_TOO_LARGE = "BROWSER_OUTPUT_TOO_LARGE"
    WORKER_UNAVAILABLE = "BROWSER_WORKER_UNAVAILABLE"
    REF_STALE = "BROWSER_REF_STALE"
    REF_NOT_FOUND = "BROWSER_REF_NOT_FOUND"
    ASSERTION_FAILED = "BROWSER_ASSERTION_FAILED"
    PROGRAM_INVALID = "BROWSER_PROGRAM_INVALID"
    PROGRAM_FAILED = "BROWSER_PROGRAM_FAILED"
    PROGRAM_LIMIT = "BROWSER_PROGRAM_LIMIT"
    SDK_LIMIT = "BROWSER_SDK_LIMIT"
    SEQUENCE_CONFLICT = "BROWSER_SEQUENCE_CONFLICT"
    EXECUTION_CONFLICT = "BROWSER_EXECUTION_CONFLICT"
    EXECUTION_REVOKED = "BROWSER_EXECUTION_REVOKED"
    WRITE_BLOCKED = "BROWSER_WRITE_BLOCKED"
    PAGE_UNSTABLE = "BROWSER_PAGE_UNSTABLE"
    RUNNER_UNAVAILABLE = "BROWSER_RUNNER_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class WorkerFault:
    code: WorkerErrorCode
    message: str
    retryable: bool = False
    field: str | None = None

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.field is not None:
            result["field"] = self.field
        return result


def response_ok(request_id: str, result: Any) -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": True,
        "result": result,
    }


def response_error(request_id: str, fault: WorkerFault) -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "error": fault.payload(),
    }

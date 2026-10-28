from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 2 * 1024 * 1024


class WorkerOperation(StrEnum):
    HEALTH = "health"
    CREATE_RUN = "create_run"
    MCP_REQUEST = "mcp_request"
    GET_RUN = "get_run"
    CANCEL_RUN = "cancel_run"


class WorkerErrorCode(StrEnum):
    AUTHENTICATION_FAILED = "BROWSER_WORKER_AUTHENTICATION_FAILED"
    CAPACITY_EXHAUSTED = "BROWSER_CAPACITY_EXHAUSTED"
    INVALID_REQUEST = "BROWSER_WORKER_INVALID_REQUEST"
    RUN_NOT_FOUND = "BROWSER_RUN_NOT_FOUND"
    RUN_CAPABILITY_INVALID = "BROWSER_RUN_CAPABILITY_INVALID"
    MCP_FAILED = "BROWSER_MCP_FAILED"
    MCP_PROTOCOL_ERROR = "BROWSER_MCP_PROTOCOL_ERROR"
    OUTPUT_TOO_LARGE = "BROWSER_OUTPUT_TOO_LARGE"
    WORKER_UNAVAILABLE = "BROWSER_WORKER_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class WorkerFault:
    code: WorkerErrorCode
    message: str
    retryable: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }


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

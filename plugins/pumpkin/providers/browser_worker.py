from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from browser.protocol import MAX_FRAME_BYTES, PROTOCOL_VERSION, WorkerOperation


class BrowserWorkerError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class BrowserWorkerRun:
    run_id: str
    capability: str


class BrowserWorkerClient:
    def __init__(
        self,
        *,
        socket_path: str | Path,
        token: str,
        timeout: float = 65,
    ) -> None:
        self.socket_path = str(socket_path)
        self.token = token
        self.timeout = timeout

    async def health(self) -> dict[str, Any]:
        return await self._request(WorkerOperation.HEALTH, {})

    async def create_run(self) -> BrowserWorkerRun:
        result = await self._request(WorkerOperation.CREATE_RUN, {})
        return BrowserWorkerRun(
            run_id=str(result["run_id"]),
            capability=str(result["run_capability"]),
        )

    async def mcp_request(
        self,
        run: BrowserWorkerRun,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            WorkerOperation.MCP_REQUEST,
            self._run_payload(run, message=message),
        )

    async def get_run(self, run: BrowserWorkerRun) -> dict[str, Any]:
        return await self._request(
            WorkerOperation.GET_RUN,
            self._run_payload(run),
        )

    async def cancel_run(self, run: BrowserWorkerRun) -> None:
        await self._request(
            WorkerOperation.CANCEL_RUN,
            self._run_payload(run),
        )

    async def _request(
        self,
        operation: WorkerOperation,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request = {
            "version": PROTOCOL_VERSION,
            "request_id": uuid.uuid4().hex,
            "token": self.token,
            "operation": operation.value,
            "payload": payload,
        }
        encoded = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > MAX_FRAME_BYTES:
            raise BrowserWorkerError(
                "BROWSER_WORKER_INVALID_REQUEST",
                "browser worker request is too large",
            )
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path),
                timeout=self.timeout,
            )
            try:
                writer.write(encoded)
                await writer.drain()
                raw = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            finally:
                writer.close()
                await writer.wait_closed()
        except (OSError, TimeoutError) as exc:
            raise BrowserWorkerError(
                "BROWSER_WORKER_UNAVAILABLE",
                "browser worker is unavailable",
                retryable=True,
            ) from exc
        if not raw or len(raw) > MAX_FRAME_BYTES:
            raise BrowserWorkerError(
                "BROWSER_WORKER_PROTOCOL_ERROR",
                "browser worker returned an invalid frame",
            )
        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrowserWorkerError(
                "BROWSER_WORKER_PROTOCOL_ERROR",
                "browser worker returned invalid JSON",
            ) from exc
        if not isinstance(response, dict) or response.get("request_id") != request["request_id"]:
            raise BrowserWorkerError(
                "BROWSER_WORKER_PROTOCOL_ERROR",
                "browser worker response does not match the request",
            )
        if not response.get("ok"):
            error = response.get("error") or {}
            raise BrowserWorkerError(
                str(error.get("code") or "BROWSER_WORKER_FAILED"),
                str(error.get("message") or "browser worker failed"),
                retryable=bool(error.get("retryable")),
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise BrowserWorkerError(
                "BROWSER_WORKER_PROTOCOL_ERROR",
                "browser worker result must be an object",
            )
        return result

    @staticmethod
    def _run_payload(
        run: BrowserWorkerRun,
        **values: Any,
    ) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "run_capability": run.capability,
            **values,
        }

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
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
    task_owner: str = ""
    sdk_version: int = 0
    initial_observation: dict[str, Any] = field(default_factory=dict)
    capabilities: frozenset[str] = frozenset()
    engine: str = ""
    playwright_version: str = ""
    chromium_version: str = ""


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
        self.instance_id = uuid.uuid4().hex

    async def health(self) -> dict[str, Any]:
        return await self._request(WorkerOperation.HEALTH, {})

    async def create_run(self, *, contract: dict[str, Any], task_owner: str = "") -> BrowserWorkerRun:
        task_owner = task_owner or uuid.uuid4().hex
        request = asyncio.create_task(self._request(WorkerOperation.CREATE_RUN, {
            "runtime_instance": self.instance_id, "task_owner": task_owner,
            "contract": contract,
        }))
        interrupted = False
        while not request.done():
            try:
                await asyncio.shield(request)
            except asyncio.CancelledError:
                interrupted = True
            except BrowserWorkerError as exc:
                if interrupted:
                    raise asyncio.CancelledError from exc
                raise
        if interrupted and request.exception() is not None:
            raise asyncio.CancelledError from request.exception()
        result = request.result()
        run = BrowserWorkerRun(
            run_id=str(result["run_id"]),
            capability=str(result["run_capability"]),
            task_owner=task_owner,
            sdk_version=result.get("sdk_version", 0),
            initial_observation=result.get("initial_observation") or {},
            capabilities=frozenset(
                str(key)
                for key, enabled in (result.get("capabilities") or {}).items()
                if enabled
            ),
            engine=str(result.get("engine", "")),
            playwright_version=str(result.get("playwright_version", "")),
            chromium_version=str(result.get("chromium_version", "")),
        )
        if interrupted:
            cleanup = asyncio.create_task(self.cancel_run(run))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    pass
            cleanup.result()
            raise asyncio.CancelledError
        return run

    async def heartbeat(self, run: BrowserWorkerRun) -> None:
        await self._request(
            WorkerOperation.HEARTBEAT, self._run_payload(run), timeout=min(self.timeout, 5),
        )

    async def execute_program(
        self,
        run: BrowserWorkerRun,
        *,
        execution_id: str,
        program: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        return await self._request(
            WorkerOperation.EXECUTE_PROGRAM,
            self._run_payload(
                run,
                execution_id=execution_id,
                program=program,
                timeout_seconds=timeout_seconds,
            ),
        )

    async def capture(
        self, run: BrowserWorkerRun, *, mode: str, full_page: bool = False,
        submission_evidence: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return await self._request(
            WorkerOperation.CAPTURE, self._run_payload(
                run, mode=mode, full_page=full_page, submission_evidence=list(submission_evidence),
            ),
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
            timeout=min(self.timeout, 5),
        )

    async def _request(
        self,
        operation: WorkerOperation,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        timeout = self.timeout if timeout is None else timeout
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
                asyncio.open_unix_connection(
                    self.socket_path,
                    limit=MAX_FRAME_BYTES,
                ),
                timeout=timeout,
            )
            try:
                writer.write(encoded)
                await writer.drain()
                raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
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

    def _run_payload(
        self,
        run: BrowserWorkerRun,
        **values: Any,
    ) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "run_capability": run.capability,
            "runtime_instance": self.instance_id,
            "task_owner": run.task_owner,
            **values,
        }

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.config import BrowserConfig
from providers.browser_worker import (
    BrowserWorkerClient,
    BrowserWorkerError,
    BrowserWorkerRun,
)

from .models import BrowserAgentError, BrowserExecutionState, BrowserTaskContract


_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class BrowserWorkerSession:
    worker: BrowserWorkerClient
    policy: BrowserConfig
    max_programs: int
    trace: Callable[[str], None] | None = None
    contract: BrowserTaskContract | None = None
    run: BrowserWorkerRun | None = None
    available: frozenset[str] = frozenset()
    heartbeat: asyncio.Task[None] | None = None
    heartbeat_fault: BrowserWorkerError | None = None

    def check(self) -> None:
        if self.heartbeat_fault is not None:
            raise self.heartbeat_fault

    async def acquire(self, deadline: float, task_owner: str) -> BrowserWorkerRun:
        wait_deadline = min(
            deadline,
            asyncio.get_running_loop().time() + self.policy.capacity_wait_seconds,
        )
        try:
            async with asyncio.timeout_at(wait_deadline):
                while True:
                    try:
                        assert self.contract is not None
                        self.run = await self.worker.create_run(
                            contract=self.contract.payload(), task_owner=task_owner,
                        )
                        self.heartbeat = asyncio.create_task(self._keep_alive())
                        return self.run
                    except BrowserWorkerError as exc:
                        if exc.code != "BROWSER_CAPACITY_EXHAUSTED":
                            raise
                        await asyncio.sleep(self.policy.capacity_poll_seconds)
        except TimeoutError as exc:
            raise BrowserAgentError(
                "BROWSER_CAPACITY_WAIT_TIMEOUT",
                "浏览器暂时被占用，等待超时，本次未执行页面操作。",
                retryable=True,
            ) from exc

    async def execute(
        self,
        state: BrowserExecutionState,
        program: str,
        *, timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if self.run is None:
            raise BrowserAgentError(
                "BROWSER_RUN_NOT_FOUND",
                "浏览器会话尚未创建。",
            )
        if state.program_count >= self.max_programs:
            raise BrowserAgentError(
                "BROWSER_PROGRAM_LIMIT",
                "浏览器程序预算已用尽。",
            )
        state.program_count += 1
        started = asyncio.get_running_loop().time()
        response: dict[str, Any] = {}
        error_code: str | None = None
        try:
            response = await self.worker.execute_program(
                self.run,
                execution_id=uuid.uuid4().hex, program=program,
                timeout_seconds=min(
                    self.policy.program_timeout_seconds,
                    timeout_seconds if timeout_seconds is not None else self.policy.program_timeout_seconds,
                ),
            )
            if not isinstance(response, dict) or "status" not in response:
                raise BrowserAgentError(
                    "BROWSER_WORKER_PROTOCOL_ERROR",
                    "浏览器 Worker 返回格式无效。",
                )
            state.record(response)
            return response
        except (BrowserAgentError, BrowserWorkerError) as exc:
            error_code = exc.code
            raise
        finally:
            self._emit(
                {
                    "event": "tool.finished",
                    "run_id": self.run.run_id,
                    "tool": "browser_execute",
                    "duration_ms": round(
                        (asyncio.get_running_loop().time() - started) * 1_000
                    ),
                    "error_code": error_code or self.error_code(response),
                    "status": response.get("status"),
                }
            )

    async def close(self) -> None:
        if self.run is None:
            return

        async def release() -> None:
            async with asyncio.timeout(self.policy.cleanup_timeout_seconds):
                if self.heartbeat:
                    self.heartbeat.cancel()
                    await asyncio.gather(self.heartbeat, return_exceptions=True)
                try:
                    await self.worker.cancel_run(self.run)
                except BrowserWorkerError as exc:
                    if exc.code != "BROWSER_RUN_NOT_FOUND":
                        raise

        cleanup = asyncio.create_task(release())
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        try:
            cleanup.result()
        except TimeoutError as exc:
            raise BrowserWorkerError(
                "BROWSER_CLEANUP_TIMEOUT",
                "browser cleanup timed out",
            ) from exc
        if interrupted:
            raise asyncio.CancelledError

    async def _keep_alive(self) -> None:
        assert self.run is not None
        try:
            while True:
                await asyncio.sleep(self.policy.heartbeat_interval_seconds)
                await self.worker.heartbeat(self.run)
        except asyncio.CancelledError:
            raise
        except BrowserWorkerError as exc:
            self.heartbeat_fault = exc

    @staticmethod
    def error_code(result: dict[str, Any]) -> str | None:
        error = result.get("error")
        return (
            str(error["code"])
            if isinstance(error, dict) and error.get("code")
            else None
        )

    @staticmethod
    def _non_negative(value: Any, default: int) -> int:
        return value if type(value) is int and value >= 0 else default

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.trace is None:
            return
        try:
            self.trace(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        except Exception:
            _LOGGER.debug("Browser trace callback failed", exc_info=True)

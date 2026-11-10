from __future__ import annotations

import asyncio
import importlib.metadata
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .code_runner import RunnerClient
from .code_validator import BrowserCodeValidator
from .engine import PlaywrightRun
from .models import BrowserInteractionMode
from .protocol import (
    MAX_FRAME_BYTES, PROTOCOL_VERSION, WorkerErrorCode, WorkerFault,
    WorkerOperation, response_error, response_ok,
)
from .sdk_protocol import (
    SDK_VERSION, SDK_MAX_FRAME_BYTES, MAX_PROGRAMS, MAX_SDK_CALLS,
    PROGRAM_TIMEOUT_SECONDS, ProgramStatus, SdkError, SdkMethod,
)


_OWNER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_COMMAND_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class WorkerRequestError(RuntimeError):
    def __init__(self, fault: WorkerFault) -> None:
        super().__init__(fault.message)
        self.fault = fault


@dataclass(slots=True)
class ProgramExecution:
    digest: str
    capability: str
    initial_observation_id: str | None = None
    deadline: float = float("inf")
    task: asyncio.Task[dict[str, Any]] | None = None
    revoked: bool = False
    next_sequence: int = 1
    replies: dict[int, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class BrowserRun:
    run_id: str
    capability: str
    engine: PlaywrightRun
    temp_dir: Path
    runtime_instance: str
    task_owner: str
    programs: dict[str, ProgramExecution] = field(default_factory=dict)
    sdk_server: asyncio.Server | None = None
    sdk_socket: Path | None = None
    sdk_count: int = 0
    sdk_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    created_at: float = field(default_factory=time.monotonic)
    last_heartbeat: float = field(default_factory=time.monotonic)
    lease_task: asyncio.Task[None] | None = None
    cleanup_task: asyncio.Task[None] | None = None


class BrowserWorker:
    def __init__(
        self,
        *,
        token: str,
        request_timeout_seconds: float = 65,
        navigation_timeout_seconds: float = 20,
        action_timeout_seconds: float = 8,
        lease_seconds: float = 900,
        heartbeat_timeout_seconds: float = 30,
        runner_socket: str = "/run/pumpkin-browser/runner.sock",
        runner_token: str = "",
        sdk_socket_dir: Path = Path("/run/pumpkin-browser/sdk"),
    ) -> None:
        if min(
            request_timeout_seconds,
            navigation_timeout_seconds,
            action_timeout_seconds,
            lease_seconds,
            heartbeat_timeout_seconds,
        ) <= 0:
            raise ValueError("browser worker timeouts must be positive")
        self.token = token
        self.request_timeout_seconds = request_timeout_seconds
        self.navigation_timeout_seconds = navigation_timeout_seconds
        self.action_timeout_seconds = action_timeout_seconds
        self.lease_seconds = lease_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.runner_socket = runner_socket
        self.runner_token = runner_token
        self.sdk_socket_dir = sdk_socket_dir
        self._run: BrowserRun | None = None
        self._lease_lock = asyncio.Lock()

    async def dispatch(self, request: dict[str, Any]) -> Any:
        if request.get("version") != PROTOCOL_VERSION:
            self._invalid("unsupported protocol version")
        if not secrets.compare_digest(str(request.get("token", "")), self.token):
            raise WorkerRequestError(
                WorkerFault(
                    WorkerErrorCode.AUTHENTICATION_FAILED,
                    "worker authentication failed",
                )
            )
        try:
            operation = WorkerOperation(str(request.get("operation", "")))
        except ValueError:
            self._invalid("unknown operation")
        payload = request.get("payload") or {}
        if not isinstance(payload, dict):
            self._invalid("payload must be an object")
        if operation is WorkerOperation.HEALTH:
            return self.health()
        if operation is WorkerOperation.CREATE_RUN:
            return await self.create_run(
                runtime_instance=self._owner(payload, "runtime_instance"),
                task_owner=self._owner(payload, "task_owner"),
                contract=payload.get("contract"),
            )
        run = self._authorized_run(payload)
        if operation is WorkerOperation.HEARTBEAT:
            if run.cleanup_task is not None:
                self._invalid("run is closing")
            run.last_heartbeat = time.monotonic()
            return {"status": "running"}
        if operation is WorkerOperation.GET_RUN:
            return {
                "run_id": run.run_id,
                "status": "closing" if run.cleanup_task is not None else "running",
                "program_count": len(run.programs),
                "sdk_call_count": run.sdk_count,
                "outcome_unknown": run.engine.outcome_unknown,
                "document_epoch": run.engine.document_epoch,
                "resource_version": run.engine.resource_version,
                "observation_id": run.engine.observation_id,
                "progress_fingerprint": getattr(
                    run.engine, "progress_fingerprint", None,
                ),
            }
        if operation is WorkerOperation.CANCEL_RUN:
            await self._cleanup(run)
            return {"run_id": run.run_id, "status": "cancelled"}
        if operation is WorkerOperation.EXECUTE_PROGRAM:
            return await self.execute_program(run, payload)
        if operation is WorkerOperation.CAPTURE:
            if run.cleanup_task or any(item.task and not item.task.done() for item in run.programs.values()):
                self._invalid("cannot finalize an active or closing run")
            return await run.engine.capture(
                str(payload.get("mode", "none")), payload.get("full_page", False),
                payload.get("submission_evidence", ()),
            )
        self._invalid("unsupported operation")

    def health(self) -> dict[str, Any]:
        try:
            version = importlib.metadata.version("playwright")
        except importlib.metadata.PackageNotFoundError:
            version = "unavailable"
        return {
            "status": "ok",
            "active_runs": int(self._run is not None),
            "engine": "playwright",
            "playwright_version": version,
            "protocol_version": PROTOCOL_VERSION,
            "sdk_version": SDK_VERSION,
            "capabilities": {
                "playwright": True,
                "viewport_screenshot": True,
                "full_page_screenshot": True,
                "full_page_content": True,
                "pdf": True,
            },
        }

    async def create_run(
        self,
        *,
        runtime_instance: str,
        task_owner: str,
        contract: Any,
    ) -> dict[str, Any]:
        if not isinstance(contract, dict):
            self._invalid("contract is required")
        start_url = contract.get("start_url")
        mode = contract.get("interaction_mode")
        if not isinstance(start_url, str) or mode not in {item.value for item in BrowserInteractionMode}:
            self._invalid("contract URL or interaction mode is invalid")
        async with self._lease_lock:
            if self._run is not None:
                raise WorkerRequestError(
                    WorkerFault(
                        WorkerErrorCode.CAPACITY_EXHAUSTED,
                        "browser worker is already in use",
                        retryable=True,
                    )
                )
            temp_dir = Path(tempfile.mkdtemp(prefix="pumpkin-browser-"))
            temp_dir.chmod(0o700)
            engine = PlaywrightRun(
                temp_dir=temp_dir,
                navigation_timeout_ms=int(self.navigation_timeout_seconds * 1000),
                action_timeout_ms=int(self.action_timeout_seconds * 1000),
            )
            try:
                await engine.validate_url(start_url)
                await engine.start()
                await engine.sdk_call(SdkMethod.NAVIGATE, {"url": start_url}, 0)
                initial = await engine.snapshot()
            except SdkError as exc:
                await engine.close()
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise WorkerRequestError(exc.fault) from exc
            except Exception as exc:
                await engine.close()
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise WorkerRequestError(
                    WorkerFault(
                        WorkerErrorCode.ENGINE_FAILED,
                        "browser engine failed to start",
                        retryable=True,
                    )
                ) from exc
            run = BrowserRun(
                run_id=uuid.uuid4().hex,
                capability=secrets.token_urlsafe(32),
                engine=engine,
                temp_dir=temp_dir,
                runtime_instance=runtime_instance,
                task_owner=task_owner,
            )
            self._run = run
            try:
                self.sdk_socket_dir.mkdir(parents=True, exist_ok=True)
                run.sdk_socket = self.sdk_socket_dir / f"{run.run_id}.sock"
                run.sdk_server = await asyncio.start_unix_server(
                    lambda reader, writer: self._serve_sdk(run, reader, writer),
                    path=run.sdk_socket, limit=SDK_MAX_FRAME_BYTES,
                )
                run.sdk_socket.chmod(0o660)
            except OSError:
                await engine.close()
                shutil.rmtree(temp_dir, ignore_errors=True)
                self._run = None
                raise
            run.lease_task = asyncio.create_task(self._expire_run(run))
            try:
                playwright_version = importlib.metadata.version("playwright")
            except importlib.metadata.PackageNotFoundError:
                playwright_version = "unavailable"
            return {
                "run_id": run.run_id,
                "run_capability": run.capability,
                "status": "running",
                "engine": "playwright",
                "playwright_version": playwright_version,
                "chromium_version": (
                    str(engine.browser.version) if engine.browser is not None else ""
                ),
                "sdk_version": SDK_VERSION,
                "initial_observation": initial.payload(),
                "capabilities": self.health()["capabilities"],
            }

    async def execute_program(self, run: BrowserRun, payload: dict[str, Any]) -> dict[str, Any]:
        execution_id = payload.get("execution_id", "")
        if not isinstance(execution_id, str) or not _COMMAND_ID.fullmatch(execution_id):
            self._invalid("execution_id is invalid")
        if run.cleanup_task:
            self._invalid("run is closing")
        program = payload.get("program")
        BrowserCodeValidator().validate(program)
        timeout = payload.get("timeout_seconds", PROGRAM_TIMEOUT_SECONDS)
        if type(timeout) not in {int, float} or not 0 < timeout <= PROGRAM_TIMEOUT_SECONDS:
            self._invalid("invalid program timeout")
        digest = hashlib.sha256(json.dumps(
            {"program": program, "timeout": timeout}, sort_keys=True,
        ).encode()).hexdigest()
        existing = run.programs.get(execution_id)
        if existing:
            if existing.digest != digest:
                raise SdkError(WorkerErrorCode.EXECUTION_CONFLICT, "execution ID has different inputs")
            assert existing.task
            return await asyncio.shield(existing.task)
        if any(item.task and not item.task.done() for item in run.programs.values()):
            raise SdkError(WorkerErrorCode.CAPACITY_EXHAUSTED, "program is already running")
        if len(run.programs) >= MAX_PROGRAMS:
            raise SdkError(WorkerErrorCode.PROGRAM_LIMIT, "program budget is exhausted")
        execution = ProgramExecution(
            digest, secrets.token_urlsafe(32), run.engine.observation_id,
            time.monotonic() + timeout,
        )
        run.programs[execution_id] = execution
        execution.task = asyncio.create_task(self._execute(run, execution_id, execution, program, timeout))
        return await asyncio.shield(execution.task)

    async def _execute(
        self, run: BrowserRun, execution_id: str, execution: ProgramExecution,
        program: str, timeout: float,
    ) -> dict[str, Any]:
        ledger_start = len(run.engine.ledger)
        try:
            return await self._execute_once(run, execution_id, execution, program, timeout)
        except asyncio.CancelledError:
            execution.revoked = True
            return {
                "status": ProgramStatus.OUTCOME_UNKNOWN.value
                if run.engine.outcome_unknown else ProgramStatus.CANCELLED.value,
                "execution_id": execution_id, "sdk_call_count": execution.next_sequence - 1,
                "action_ledger": [item.payload() for item in run.engine.ledger[ledger_start:]],
                "last_confirmed_action_seq": run.engine.last_confirmed_action_seq,
                "final_observation": None, "outcome_unknown": run.engine.outcome_unknown,
            }

    async def _execute_once(
        self, run: BrowserRun, execution_id: str, execution: ProgramExecution,
        program: str, timeout: float,
    ) -> dict[str, Any]:
        ledger_start = len(run.engine.ledger)
        pending_sequences = {item.action_sequence for item in run.engine.ledger if item.status == "pending"}
        result: dict[str, Any]
        try:
            reply = await RunnerClient.request(self.runner_socket, {
                "version": SDK_VERSION, "token": self.runner_token, "operation": "execute",
                "execution_id": execution_id, "run_id": run.run_id,
                "sdk_socket": str(run.sdk_socket), "sdk_capability": execution.capability,
                "program": program, "timeout_seconds": timeout,
            }, timeout=timeout + 5)
            result = reply.get("result") if reply.get("ok") else {
                "status": ProgramStatus.REJECTED.value, "error": reply.get("error"),
            }
            if not isinstance(result, dict):
                raise TypeError("invalid runner response")
        except (OSError, TimeoutError, ValueError, TypeError) as exc:
            result = {"status": ProgramStatus.OUTCOME_UNKNOWN.value, "error": {
                "code": WorkerErrorCode.RUNNER_UNAVAILABLE.value,
                "message": f"runner response unavailable: {type(exc).__name__}",
            }}
        except asyncio.CancelledError:
            result = {"status": ProgramStatus.CANCELLED.value}
        finally:
            execution.revoked = True
        async with execution.lock:
            receipts = [
                item for index, item in enumerate(run.engine.ledger)
                if index >= ledger_start or item.action_sequence in pending_sequences
            ]
            if any(item.effect == "unknown" for item in receipts):
                run.engine.outcome_unknown = True
            if result["status"] == ProgramStatus.OUTCOME_UNKNOWN.value and receipts:
                run.engine.outcome_unknown = True
            if run.engine.outcome_unknown:
                result["status"] = ProgramStatus.OUTCOME_UNKNOWN.value
            elif result["status"] != ProgramStatus.SUCCEEDED.value and receipts:
                result["status"] = ProgramStatus.PARTIALLY_EXECUTED.value
            try:
                observation = await run.engine.snapshot() if not run.engine.closed else None
            except Exception:  # noqa: BLE001
                run.engine.outcome_unknown = True
                observation = None
                result["status"] = ProgramStatus.OUTCOME_UNKNOWN.value
                result["error"] = WorkerFault(
                    WorkerErrorCode.ENGINE_FAILED, "final page observation failed",
                ).payload()
            return {
                **result, "execution_id": execution_id,
                "sdk_call_count": execution.next_sequence - 1,
                "action_ledger": [item.payload() for item in receipts],
                "last_confirmed_action_seq": run.engine.last_confirmed_action_seq,
                "final_observation": observation.payload() if observation else None,
                "outcome_unknown": run.engine.outcome_unknown,
            }

    async def sdk_request(self, run: BrowserRun, request: dict[str, Any]) -> dict[str, Any]:
        execution = run.programs.get(request.get("execution_id"))
        if (
            request.get("version") != SDK_VERSION or request.get("run_id") != run.run_id
            or self._run is not run or run.cleanup_task or execution is None or execution.revoked
            or time.monotonic() > execution.deadline
            or not secrets.compare_digest(str(request.get("sdk_capability", "")), execution.capability)
        ):
            raise SdkError(WorkerErrorCode.EXECUTION_REVOKED, "SDK execution binding is invalid or revoked")
        if request.get("operation") == "bind":
            return {"ok": True, "result": {"status": "bound"}}
        sequence = request.get("sequence")
        if type(sequence) is not int or sequence <= 0:
            raise SdkError(WorkerErrorCode.SEQUENCE_CONFLICT, "invalid SDK sequence")
        digest = hashlib.sha256(json.dumps({
            "method": request.get("method"), "arguments": request.get("arguments"),
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        async with execution.lock:
            if execution.revoked or run.cleanup_task or time.monotonic() > execution.deadline:
                raise SdkError(WorkerErrorCode.EXECUTION_REVOKED, "SDK execution was revoked")
            cached = execution.replies.get(sequence)
            if cached:
                if cached[0] != digest:
                    raise SdkError(WorkerErrorCode.SEQUENCE_CONFLICT, "sequence has different inputs")
                return cached[1]
            if sequence != execution.next_sequence:
                raise SdkError(WorkerErrorCode.SEQUENCE_CONFLICT, "SDK sequence is out of order")
            if run.sdk_count >= MAX_SDK_CALLS:
                raise SdkError(WorkerErrorCode.SDK_LIMIT, "run SDK call budget is exhausted")
            run.sdk_count += 1
            execution.next_sequence += 1
            try:
                try:
                    method = SdkMethod(request.get("method"))
                except (TypeError, ValueError):
                    raise SdkError(WorkerErrorCode.TOOL_REJECTED, "SDK method is not supported") from None
                result = await run.engine.sdk_call(method, request.get("arguments"), sequence)
                reply = {"ok": True, "result": result}
            except SdkError as exc:
                reply = {"ok": False, "error": {**exc.fault.payload(), "sdk_call_index": sequence}}
            except Exception:  # noqa: BLE001
                reply = {"ok": False, "error": {
                    **WorkerFault(WorkerErrorCode.ENGINE_FAILED, "SDK page operation failed").payload(),
                    "sdk_call_index": sequence,
                }}
            execution.replies[sequence] = digest, reply
            return reply

    async def _serve_sdk(
        self, run: BrowserRun, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        run.sdk_tasks.add(task)
        try:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=5)
                if not raw or len(raw) > SDK_MAX_FRAME_BYTES:
                    raise ValueError("invalid SDK frame")
                request = json.loads(raw)
                if not isinstance(request, dict):
                    raise TypeError("invalid SDK request")
                reply = await self.sdk_request(run, request)
            except SdkError as exc:
                reply = {"ok": False, "error": exc.fault.payload()}
            except (ValueError, TypeError, OSError, TimeoutError):
                reply = {"ok": False, "error": WorkerFault(
                    WorkerErrorCode.INVALID_REQUEST, "invalid SDK request",
                ).payload()}
            writer.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
        finally:
            run.sdk_tasks.discard(task)
            writer.close()
            await writer.wait_closed()

    async def _expire_run(self, run: BrowserRun) -> None:
        while True:
            remaining = min(
                run.created_at + self.lease_seconds,
                run.last_heartbeat + self.heartbeat_timeout_seconds,
            ) - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        await self._cleanup(run)

    def _authorized_run(self, payload: dict[str, Any]) -> BrowserRun:
        run = self._run
        if run is None or str(payload.get("run_id", "")) != run.run_id:
            raise WorkerRequestError(
                WorkerFault(WorkerErrorCode.RUN_NOT_FOUND, "browser run was not found")
            )
        if not secrets.compare_digest(
            str(payload.get("run_capability", "")),
            run.capability,
        ):
            raise WorkerRequestError(
                WorkerFault(
                    WorkerErrorCode.RUN_CAPABILITY_INVALID,
                    "browser run capability is invalid",
                )
            )
        if (
            str(payload.get("runtime_instance", "")) != run.runtime_instance
            or str(payload.get("task_owner", "")) != run.task_owner
        ):
            raise WorkerRequestError(
                WorkerFault(
                    WorkerErrorCode.RUN_CAPABILITY_INVALID,
                    "browser run owner is invalid",
                )
            )
        return run

    async def _cleanup(self, run: BrowserRun) -> None:
        if run.cleanup_task is None:
            run.cleanup_task = asyncio.create_task(
                self._cleanup_once(run, initiator=asyncio.current_task())
            )
        interrupted = False
        while not run.cleanup_task.done():
            try:
                await asyncio.shield(run.cleanup_task)
            except asyncio.CancelledError:
                interrupted = True
        run.cleanup_task.result()
        if interrupted:
            raise asyncio.CancelledError

    async def _cleanup_once(
        self,
        run: BrowserRun,
        *,
        initiator: asyncio.Task[Any] | None,
    ) -> None:
        async with self._lease_lock:
            if self._run is not run:
                return
            for execution_id, execution in run.programs.items():
                execution.revoked = True
                if execution.task and not execution.task.done():
                    try:
                        await RunnerClient.request(self.runner_socket, {
                            "version": SDK_VERSION, "token": self.runner_token,
                            "operation": "cancel", "execution_id": execution_id,
                        }, timeout=3)
                    except (OSError, ValueError, TimeoutError):
                        pass
                    sdk_tasks = tuple(run.sdk_tasks)
                    for task in sdk_tasks:
                        task.cancel()
                    await asyncio.gather(*sdk_tasks, return_exceptions=True)
                    execution.task.cancel()
                    await asyncio.gather(execution.task, return_exceptions=True)
            if run.sdk_server:
                run.sdk_server.close()
                await run.sdk_server.wait_closed()
            if run.sdk_socket:
                run.sdk_socket.unlink(missing_ok=True)
            await run.engine.close()
            if run.lease_task is not None and run.lease_task is not initiator:
                run.lease_task.cancel()
                await asyncio.gather(run.lease_task, return_exceptions=True)
            shutil.rmtree(run.temp_dir, ignore_errors=True)
            self._run = None

    @staticmethod
    def _owner(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not _OWNER.fullmatch(value):
            raise WorkerRequestError(
                WorkerFault(
                    WorkerErrorCode.INVALID_REQUEST,
                    f"{key} is required",
                )
            )
        return value

    @staticmethod
    def _invalid(message: str) -> None:
        raise WorkerRequestError(
            WorkerFault(WorkerErrorCode.INVALID_REQUEST, message)
        )


async def _serve_client(
    worker: BrowserWorker,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    request_id = ""
    try:
        raw = await reader.readline()
        if not raw or len(raw) > MAX_FRAME_BYTES:
            raise WorkerRequestError(
                WorkerFault(WorkerErrorCode.INVALID_REQUEST, "invalid request frame")
            )
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise TypeError("request must be an object")
        request_id = str(request.get("request_id", ""))
        result = await worker.dispatch(request)
        response = response_ok(request_id, result)
    except (WorkerRequestError, SdkError) as exc:
        response = response_error(request_id, exc.fault)
    except (json.JSONDecodeError, TypeError, ValueError):
        response = response_error(
            request_id,
            WorkerFault(WorkerErrorCode.INVALID_REQUEST, "invalid request"),
        )
    except Exception:  # noqa: BLE001
        response = response_error(
            request_id,
            WorkerFault(
                WorkerErrorCode.WORKER_UNAVAILABLE,
                "browser worker failed",
                retryable=True,
            ),
        )
    encoded = json.dumps(response, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > MAX_FRAME_BYTES:
        encoded = json.dumps(response_error(
            request_id,
            WorkerFault(
                WorkerErrorCode.OUTPUT_TOO_LARGE,
                "browser worker response is too large",
            ),
        ), separators=(",", ":")).encode() + b"\n"
    writer.write(encoded)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def main() -> None:
    socket_path = Path(
        os.environ.get(
            "BROWSER_WORKER_SOCKET",
            "/run/pumpkin-browser/control.sock",
        )
    )
    token = os.environ.get("BROWSER_WORKER_TOKEN", "")
    if not token:
        raise RuntimeError("BROWSER_WORKER_TOKEN is required")
    if not os.environ.get("BROWSER_CODE_RUNNER_TOKEN"):
        raise RuntimeError("BROWSER_CODE_RUNNER_TOKEN is required")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    worker = BrowserWorker(
        token=token,
        request_timeout_seconds=float(
            os.environ.get("BROWSER_WORKER_REQUEST_TIMEOUT_SECONDS", "65")
        ),
        navigation_timeout_seconds=float(
            os.environ.get("BROWSER_WORKER_NAVIGATION_TIMEOUT_SECONDS", "20")
        ),
        action_timeout_seconds=float(
            os.environ.get("BROWSER_WORKER_ACTION_TIMEOUT_SECONDS", "8")
        ),
        lease_seconds=float(
            os.environ.get("BROWSER_WORKER_LEASE_SECONDS", "900")
        ),
        heartbeat_timeout_seconds=float(
            os.environ.get("BROWSER_WORKER_HEARTBEAT_TIMEOUT_SECONDS", "30")
        ),
        runner_token=os.environ.get("BROWSER_CODE_RUNNER_TOKEN", ""),
        runner_socket=os.environ.get("BROWSER_CODE_RUNNER_SOCKET", "/run/pumpkin-browser/runner.sock"),
        sdk_socket_dir=Path(os.environ.get("BROWSER_WORKER_SDK_SOCKET_DIR", "/run/pumpkin-browser/sdk")),
    )
    server = await asyncio.start_unix_server(
        lambda reader, writer: _serve_client(worker, reader, writer),
        path=socket_path,
        limit=MAX_FRAME_BYTES,
    )
    socket_path.chmod(0o660)
    loop = asyncio.get_running_loop()
    execution = asyncio.current_task()
    assert execution is not None
    loop.add_signal_handler(signal.SIGTERM, execution.cancel)
    try:
        async with server:
            await server.serve_forever()
    finally:
        if worker._run is not None:
            await worker._cleanup(worker._run)
        socket_path.unlink(missing_ok=True)
        loop.remove_signal_handler(signal.SIGTERM)


def healthcheck() -> None:
    socket_path = os.environ.get(
        "BROWSER_WORKER_SOCKET",
        "/run/pumpkin-browser/control.sock",
    )
    request = {
        "version": PROTOCOL_VERSION,
        "request_id": "healthcheck",
        "token": os.environ.get("BROWSER_WORKER_TOKEN", ""),
        "operation": WorkerOperation.HEALTH.value,
        "payload": {},
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(socket_path)
        client.sendall(
            json.dumps(request, separators=(",", ":")).encode() + b"\n"
        )
        raw = client.makefile("rb", buffering=0).readline(MAX_FRAME_BYTES)
    response = json.loads(raw)
    if not response.get("ok") or response.get("result", {}).get("status") != "ok":
        raise RuntimeError("browser worker healthcheck failed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--healthcheck"]:
        healthcheck()
    else:
        asyncio.run(main())

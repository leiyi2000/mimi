from __future__ import annotations

import asyncio
import json
import ipaddress
import os
import re
import secrets
import shutil
import signal
import socket
import tempfile
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from .protocol import (
        MAX_FRAME_BYTES,
        PROTOCOL_VERSION,
        WorkerErrorCode,
        WorkerFault,
        WorkerOperation,
        response_error,
        response_ok,
    )
except ImportError:
    from protocol import (  # type: ignore[no-redef]
        MAX_FRAME_BYTES,
        PROTOCOL_VERSION,
        WorkerErrorCode,
        WorkerFault,
        WorkerOperation,
        response_error,
        response_ok,
    )


ALLOWED_MCP_METHODS = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    }
)
ALLOWED_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_snapshot",
        "browser_markdown",
        "browser_links",
        "browser_interactive_elements",
        "browser_click",
        "browser_fill",
        "browser_type",
        "browser_press_key",
        "browser_scroll",
        "browser_wait_for_text",
        "browser_screenshot",
        "browser_pdf",
        "browser_close",
    }
)
_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class WorkerRequestError(RuntimeError):
    def __init__(self, fault: WorkerFault) -> None:
        super().__init__(fault.message)
        self.fault = fault


@dataclass(slots=True)
class BrowserRun:
    run_id: str
    capability: str
    process: asyncio.subprocess.Process
    temp_dir: Path
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=100))
    stderr_task: asyncio.Task[None] | None = None
    request_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    mcp_calls: int = 0


class BrowserWorker:
    def __init__(self, *, token: str, obscura_path: str) -> None:
        self.token = token
        self.obscura_path = obscura_path
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
            return {
                "status": "ok",
                "active_runs": int(self._run is not None),
                "obscura_path": self.obscura_path,
            }
        if operation is WorkerOperation.CREATE_RUN:
            return await self.create_run()
        run = self._authorized_run(payload)
        if operation is WorkerOperation.GET_RUN:
            return {
                "run_id": run.run_id,
                "status": "running" if run.process.returncode is None else "stopped",
                "mcp_calls": run.mcp_calls,
            }
        if operation is WorkerOperation.CANCEL_RUN:
            await self._cleanup(run)
            return {"run_id": run.run_id, "status": "cancelled"}
        if operation is WorkerOperation.MCP_REQUEST:
            message = payload.get("message")
            if not isinstance(message, dict):
                self._invalid("message must be an object")
            return await self.mcp_request(run, message)
        self._invalid("unsupported operation")

    async def create_run(self) -> dict[str, Any]:
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
            env = {
                "HOME": str(temp_dir),
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "TMPDIR": str(temp_dir),
                "LANG": "C.UTF-8",
            }
            try:
                process = await asyncio.create_subprocess_exec(
                    self.obscura_path,
                    "mcp",
                    "--stealth",
                    "--font-dir",
                    "/fonts",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=temp_dir,
                    env=env,
                    start_new_session=True,
                    limit=MAX_FRAME_BYTES,
                )
            except Exception:
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise
            run = BrowserRun(
                run_id=uuid.uuid4().hex,
                capability=secrets.token_urlsafe(32),
                process=process,
                temp_dir=temp_dir,
            )
            run.stderr_task = asyncio.create_task(self._drain_stderr(run))
            self._run = run
            return {
                "run_id": run.run_id,
                "run_capability": run.capability,
                "status": "running",
            }

    async def mcp_request(
        self,
        run: BrowserRun,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        self._validate_mcp(message)
        if message.get("method") == "tools/call":
            params = message["params"]
            params["arguments"] = await self._validate_arguments(
                str(params["name"]),
                params.get("arguments"),
            )
        process = run.process
        if process.returncode is not None or process.stdin is None or process.stdout is None:
            raise WorkerRequestError(
                WorkerFault(WorkerErrorCode.MCP_FAILED, "Obscura process is not running")
            )
        encoded = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > MAX_FRAME_BYTES:
            raise WorkerRequestError(
                WorkerFault(WorkerErrorCode.OUTPUT_TOO_LARGE, "MCP request is too large")
            )
        async with run.request_lock:
            try:
                process.stdin.write(encoded)
                await process.stdin.drain()
                if "id" not in message:
                    return {"notification": True}
                raw = await asyncio.wait_for(process.stdout.readline(), timeout=60)
            except (BrokenPipeError, ConnectionError, TimeoutError) as exc:
                raise WorkerRequestError(
                    WorkerFault(
                        WorkerErrorCode.MCP_FAILED,
                        "Obscura MCP request failed",
                        retryable=True,
                    )
                ) from exc
        if not raw or len(raw) > MAX_FRAME_BYTES:
            raise WorkerRequestError(
                WorkerFault(WorkerErrorCode.MCP_PROTOCOL_ERROR, "invalid MCP response")
            )
        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerRequestError(
                WorkerFault(WorkerErrorCode.MCP_PROTOCOL_ERROR, "invalid MCP JSON")
            ) from exc
        if not isinstance(response, dict):
            raise WorkerRequestError(
                WorkerFault(WorkerErrorCode.MCP_PROTOCOL_ERROR, "invalid MCP response")
            )
        run.mcp_calls += int(message.get("method") == "tools/call")
        return response

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
        return run

    @staticmethod
    def _validate_mcp(message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        if method not in ALLOWED_MCP_METHODS:
            raise WorkerRequestError(
                WorkerFault(WorkerErrorCode.INVALID_REQUEST, "MCP method is not allowed")
            )
        if method == "tools/call":
            params = message.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if name not in ALLOWED_TOOLS:
                raise WorkerRequestError(
                    WorkerFault(WorkerErrorCode.INVALID_REQUEST, "MCP tool is not allowed")
                )

    @staticmethod
    async def _validate_arguments(
        name: str,
        arguments: Any,
    ) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            BrowserWorker._invalid("MCP arguments must be an object")
        allowed_keys = {
            "browser_navigate": {"url"},
            "browser_snapshot": set(),
            "browser_markdown": set(),
            "browser_links": set(),
            "browser_interactive_elements": set(),
            "browser_click": {"ref"},
            "browser_fill": {"ref", "value"},
            "browser_type": {"ref", "text"},
            "browser_press_key": {"key"},
            "browser_scroll": {"direction", "amount"},
            "browser_wait_for_text": {"text", "timeout"},
            "browser_screenshot": set(),
            "browser_pdf": set(),
            "browser_close": set(),
        }[name]
        if set(arguments) - allowed_keys:
            BrowserWorker._invalid("MCP arguments contain forbidden fields")
        if name == "browser_navigate":
            await BrowserWorker._validate_public_url(str(arguments.get("url", "")))
        if "ref" in arguments and not _REF.fullmatch(str(arguments["ref"])):
            BrowserWorker._invalid("invalid element reference")
        for key in ("value", "text"):
            if key in arguments and len(str(arguments[key])) > 2000:
                BrowserWorker._invalid("MCP text argument is too large")
        return arguments

    @staticmethod
    async def _validate_public_url(url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(char in url for char in "\r\n\x00")
        ):
            BrowserWorker._invalid("navigation URL is invalid")
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = {record[4][0] for record in records}
            if not addresses or any(
                not ipaddress.ip_address(address).is_global
                for address in addresses
            ):
                BrowserWorker._invalid("navigation URL is not public")
        except WorkerRequestError:
            raise
        except (OSError, ValueError, UnicodeError) as exc:
            raise WorkerRequestError(
                WorkerFault(
                    WorkerErrorCode.INVALID_REQUEST,
                    "navigation URL DNS validation failed",
                )
            ) from exc

    async def _cleanup(self, run: BrowserRun) -> None:
        async with self._lease_lock:
            if self._run is not run:
                return
            process = run.process
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), timeout=3)
                except (ProcessLookupError, TimeoutError):
                    if process.returncode is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await process.wait()
            if run.stderr_task is not None:
                run.stderr_task.cancel()
                await asyncio.gather(run.stderr_task, return_exceptions=True)
            shutil.rmtree(run.temp_dir, ignore_errors=True)
            self._run = None

    @staticmethod
    async def _drain_stderr(run: BrowserRun) -> None:
        if run.process.stderr is None:
            return
        while line := await run.process.stderr.readline():
            run.stderr_lines.append(line.decode("utf-8", errors="replace")[:2000])

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
    except WorkerRequestError as exc:
        response = response_error(request_id, exc.fault)
    except (json.JSONDecodeError, TypeError, ValueError):
        response = response_error(
            request_id,
            WorkerFault(WorkerErrorCode.INVALID_REQUEST, "invalid request"),
        )
    except Exception:
        response = response_error(
            request_id,
            WorkerFault(
                WorkerErrorCode.WORKER_UNAVAILABLE,
                "browser worker failed",
                retryable=True,
            ),
        )
    writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def main() -> None:
    socket_path = Path(os.environ.get("BROWSER_WORKER_SOCKET", "/run/pumpkin-browser/control.sock"))
    token = os.environ.get("BROWSER_WORKER_TOKEN", "")
    if not token:
        raise RuntimeError("BROWSER_WORKER_TOKEN is required")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    worker = BrowserWorker(
        token=token,
        obscura_path=os.environ.get("OBSCURA_PATH", "/usr/local/bin/obscura"),
    )
    server = await asyncio.start_unix_server(
        lambda reader, writer: _serve_client(worker, reader, writer),
        path=socket_path,
    )
    socket_path.chmod(0o660)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

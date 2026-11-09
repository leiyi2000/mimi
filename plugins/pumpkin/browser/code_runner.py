import asyncio
import hashlib
import json
import os
import secrets
import signal
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .code_validator import BrowserCodeValidator
from .protocol import WorkerErrorCode
from .sdk_protocol import (
    MAX_OUTPUT_BYTES, PROGRAM_TIMEOUT_SECONDS, SDK_MAX_FRAME_BYTES, SDK_VERSION,
    ProgramStatus, SdkError,
)


@dataclass(slots=True)
class RunnerExecution:
    digest: str
    task: asyncio.Task[dict[str, Any]]


@dataclass(slots=True)
class BoundedOutput:
    limit: int
    content: bytearray = field(default_factory=bytearray)
    truncated: bool = False

    async def drain(self, reader: asyncio.StreamReader) -> None:
        while block := await reader.read(4096):
            available = max(0, self.limit - len(self.content))
            self.content.extend(block[:available])
            self.truncated |= len(block) > available

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class BrowserCodeRunner:
    def __init__(
        self, *, token: str, sdk_socket_dir: Path,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> None:
        if not token or max_output_bytes <= 0:
            raise ValueError("runner token and positive output limit are required")
        self.token = token
        self.sdk_socket_dir = sdk_socket_dir.resolve()
        self.max_output_bytes = max_output_bytes
        self.executions: dict[str, RunnerExecution] = {}
        self._active: str | None = None

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("version") != SDK_VERSION or not secrets.compare_digest(
            str(request.get("token", "")), self.token,
        ):
            raise SdkError(WorkerErrorCode.AUTHENTICATION_FAILED, "runner authentication failed")
        operation = request.get("operation")
        if operation == "health":
            return {"status": "ok"}
        execution_id = request.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id or len(execution_id) > 128:
            raise SdkError(WorkerErrorCode.INVALID_REQUEST, "invalid execution ID")
        if operation == "cancel":
            execution = self.executions.get(execution_id)
            if execution and not execution.task.done():
                execution.task.cancel()
                await asyncio.gather(execution.task, return_exceptions=True)
            return {"status": "cancelled"}
        if operation != "execute":
            raise SdkError(WorkerErrorCode.INVALID_REQUEST, "unknown runner operation")
        digest = hashlib.sha256(json.dumps(
            {key: value for key, value in request.items() if key != "token"},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        existing = self.executions.get(execution_id)
        if existing:
            if existing.digest != digest:
                raise SdkError(WorkerErrorCode.EXECUTION_CONFLICT, "execution ID has different inputs")
            return await asyncio.shield(existing.task)
        if self._active is not None:
            raise SdkError(WorkerErrorCode.CAPACITY_EXHAUSTED, "runner is already in use")
        program = request.get("program")
        BrowserCodeValidator().validate(program)
        path = Path(str(request.get("sdk_socket", "")))
        if path.parent.resolve() != self.sdk_socket_dir or not path.name.endswith(".sock"):
            raise SdkError(WorkerErrorCode.INVALID_REQUEST, "SDK socket is outside runner binding")
        timeout = request.get("timeout_seconds")
        if type(timeout) not in {int, float} or not 0 < timeout <= PROGRAM_TIMEOUT_SECONDS:
            raise SdkError(WorkerErrorCode.INVALID_REQUEST, "invalid program timeout")
        self._active = execution_id
        if len(self.executions) >= 128:
            self.executions = {
                key: value for key, value in self.executions.items() if not value.task.done()
            }
        task = asyncio.create_task(self._execute(request))
        self.executions[execution_id] = RunnerExecution(digest, task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._active = None

    async def _execute(self, request: dict[str, Any]) -> dict[str, Any]:
        parent, child = socket.socketpair()
        parent.setblocking(False)
        process: asyncio.subprocess.Process | None = None
        output = BoundedOutput(self.max_output_bytes // 2)
        errors = BoundedOutput(self.max_output_bytes // 2)
        returned: dict[str, Any] = {}
        tasks: list[asyncio.Task[Any]] = []
        status = ProgramStatus.FAILED
        try:
            bound = await RunnerClient.request(str(request["sdk_socket"]), {
                "version": SDK_VERSION, "operation": "bind",
                "run_id": request["run_id"], "execution_id": request["execution_id"],
                "sdk_capability": request["sdk_capability"],
            }, timeout=3)
            if not bound.get("ok"):
                raise SdkError(WorkerErrorCode.EXECUTION_REVOKED, "SDK binding was revoked")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-B",
                str(Path(__file__).with_name("child_entry.py")), str(child.fileno()),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(child.fileno(),), start_new_session=True,
                cwd="/tmp", env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
            child.close()
            assert process.stdin and process.stdout and process.stderr
            tasks = [
                asyncio.create_task(output.drain(process.stdout)),
                asyncio.create_task(errors.drain(process.stderr)),
                asyncio.create_task(self._broker(parent, request, returned)),
            ]
            process.stdin.write(request["program"].encode())
            await process.stdin.drain()
            process.stdin.close()
            async with asyncio.timeout(request["timeout_seconds"]):
                await process.wait()
                await asyncio.gather(*tasks)
            status = ProgramStatus.SUCCEEDED if process.returncode == 0 else ProgramStatus.FAILED
        except TimeoutError:
            status = ProgramStatus.TIMED_OUT
        except asyncio.CancelledError:
            status = ProgramStatus.CANCELLED
        except (OSError, SdkError, ValueError) as exc:
            returned["program_error"] = {
                "code": exc.fault.code.value if isinstance(exc, SdkError) else WorkerErrorCode.RUNNER_UNAVAILABLE.value,
                "message": f"runner execution failed: {type(exc).__name__}",
            }
        finally:
            if process and process.returncode is None:
                await self._terminate(process)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            parent.close()
            child.close()
            self._active = None
        return {
            "status": status.value,
            "exit_code": process.returncode if process else None,
            "stdout": output.text(), "stderr": errors.text(),
            "output_truncated": output.truncated or errors.truncated,
            **returned,
        }

    async def _broker(
        self, channel: socket.socket, request: dict[str, Any], returned: dict[str, Any],
    ) -> None:
        reader, writer = await asyncio.open_connection(sock=channel, limit=SDK_MAX_FRAME_BYTES)
        try:
            while raw := await reader.readline():
                if len(raw) > SDK_MAX_FRAME_BYTES:
                    raise ValueError("SDK frame exceeds limit")
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise TypeError("invalid SDK frame")
                if "return_value" in message or "program_error" in message:
                    if len(raw) > self.max_output_bytes:
                        raise ValueError("return value exceeds limit")
                    returned.update(message)
                    continue
                reply = await RunnerClient.request(
                    str(request["sdk_socket"]),
                    {
                        "version": SDK_VERSION,
                        "run_id": request["run_id"],
                        "execution_id": request["execution_id"],
                        "sdk_capability": request["sdk_capability"],
                        "sequence": message.get("sequence"),
                        "method": message.get("method"),
                        "arguments": message.get("arguments"),
                    },
                    timeout=request["timeout_seconds"],
                )
                writer.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()


class RunnerClient:
    @staticmethod
    async def request(path: str, request: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(path, limit=SDK_MAX_FRAME_BYTES)
            try:
                encoded = json.dumps(request, separators=(",", ":")).encode() + b"\n"
                if len(encoded) > SDK_MAX_FRAME_BYTES:
                    raise ValueError("runner request exceeds limit")
                writer.write(encoded)
                await writer.drain()
                raw = await reader.readline()
                if not raw or len(raw) > SDK_MAX_FRAME_BYTES:
                    raise TypeError("invalid runner response")
                response = json.loads(raw)
                if not isinstance(response, dict):
                    raise TypeError("invalid runner response")
                return response
            finally:
                writer.close()
                await writer.wait_closed()


class RunnerServer:
    def __init__(self, runner: BrowserCodeRunner) -> None:
        self.runner = runner

    async def serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            if not raw or len(raw) > SDK_MAX_FRAME_BYTES:
                raise ValueError("invalid runner frame")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise TypeError("runner request must be an object")
            result = await self.runner.dispatch(request)
            response = {"ok": True, "result": result}
        except SdkError as exc:
            response = {"ok": False, "error": exc.fault.payload()}
        except (ValueError, TypeError, OSError, TimeoutError):
            response = {"ok": False, "error": {
                "code": WorkerErrorCode.RUNNER_UNAVAILABLE.value,
                "message": "runner request failed",
            }}
        try:
            writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def run(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self.serve_client, path=path, limit=SDK_MAX_FRAME_BYTES,
        )
        path.chmod(0o660)
        loop = asyncio.get_running_loop()
        execution = asyncio.current_task()
        assert execution is not None
        loop.add_signal_handler(signal.SIGTERM, execution.cancel)
        try:
            async with server:
                await server.serve_forever()
        finally:
            for item in self.runner.executions.values():
                if not item.task.done():
                    item.task.cancel()
            await asyncio.gather(
                *(item.task for item in self.runner.executions.values()),
                return_exceptions=True,
            )
            path.unlink(missing_ok=True)
            loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    path = Path(os.environ.get("BROWSER_CODE_RUNNER_SOCKET", "/run/pumpkin-browser/runner.sock"))
    token = os.environ.get("BROWSER_CODE_RUNNER_TOKEN", "")
    if sys.argv[1:] == ["--healthcheck"]:
        result = asyncio.run(RunnerClient.request(str(path), {
            "version": SDK_VERSION, "token": token, "operation": "health",
        }, timeout=3))
        if not result.get("ok") or result.get("result", {}).get("status") != "ok":
            raise RuntimeError("browser runner healthcheck failed")
    else:
        asyncio.run(RunnerServer(BrowserCodeRunner(
            token=token,
            sdk_socket_dir=Path(os.environ.get(
                "BROWSER_WORKER_SDK_SOCKET_DIR", "/run/pumpkin-browser/sdk",
            )),
            max_output_bytes=int(os.environ.get(
                "BROWSER_CODE_RUNNER_MAX_OUTPUT_CHARS", str(MAX_OUTPUT_BYTES),
            )),
        )).run(path))

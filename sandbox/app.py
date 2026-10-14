from __future__ import annotations

import base64
import ctypes
import hmac
import json
import mimetypes
import os
import shutil
import signal
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    workspace_root: Path
    max_request_bytes: int
    max_script_bytes: int
    max_input_bytes: int
    max_input_files: int
    max_output_bytes: int
    max_artifact_bytes: int
    max_artifacts: int
    max_timeout: int
    max_concurrency: int
    memory_bytes: int
    max_processes: int
    proxy_url: str

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("SANDBOX_TOKEN", "").strip()
        if not token:
            raise RuntimeError("SANDBOX_TOKEN must not be empty")
        return cls(
            token=token,
            workspace_root=Path(
                os.environ.get("SANDBOX_WORKSPACE_ROOT", "/workspaces")
            ),
            max_request_bytes=_env_int("SANDBOX_MAX_REQUEST_BYTES", 1_048_576),
            max_script_bytes=_env_int("SANDBOX_MAX_SCRIPT_BYTES", 65_536),
            max_input_bytes=_env_int("SANDBOX_MAX_INPUT_BYTES", 524_288),
            max_input_files=_env_int("SANDBOX_MAX_INPUT_FILES", 20),
            max_output_bytes=_env_int("SANDBOX_MAX_OUTPUT_BYTES", 262_144),
            max_artifact_bytes=_env_int(
                "SANDBOX_MAX_ARTIFACT_BYTES", 10 * 1024 * 1024
            ),
            max_artifacts=_env_int("SANDBOX_MAX_ARTIFACTS", 10),
            max_timeout=_env_int("SANDBOX_MAX_TIMEOUT", 60),
            max_concurrency=_env_int("SANDBOX_MAX_CONCURRENCY", 2),
            memory_bytes=_env_int(
                "SANDBOX_MEMORY_BYTES", 512 * 1024 * 1024
            ),
            max_processes=_env_int("SANDBOX_MAX_PROCESSES", 64),
            proxy_url=os.environ.get(
                "SANDBOX_PROXY_URL", "http://sandbox-egress-proxy:3128"
            ).strip(),
        )


@dataclass(frozen=True, slots=True)
class Artifact:
    path: str
    mime_type: str
    data: str
    size: int


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    files: tuple[Artifact, ...]


class RequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SandboxExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._slots = threading.BoundedSemaphore(settings.max_concurrency)
        self._uid_lock = threading.Lock()
        self._next_uid = 20_000

    def execute(self, payload: dict[str, Any]) -> ExecutionResult:
        script = payload.get("script")
        if not isinstance(script, str) or not script.strip():
            raise RequestError("INVALID_SCRIPT", "script must not be empty")
        if len(script.encode()) > self.settings.max_script_bytes:
            raise RequestError("SCRIPT_TOO_LARGE", "script exceeds size limit")
        timeout = payload.get("timeout", min(30, self.settings.max_timeout))
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= self.settings.max_timeout
        ):
            raise RequestError(
                "INVALID_TIMEOUT",
                f"timeout must be between 1 and {self.settings.max_timeout}",
            )
        files = self._validate_files(payload.get("files", {}))

        if not self._slots.acquire(blocking=False):
            raise RequestError("SANDBOX_BUSY", "sandbox concurrency limit reached")
        workspace = self.settings.workspace_root / uuid.uuid4().hex
        uid = self._allocate_uid()
        try:
            workspace.mkdir(mode=0o700, parents=True)
            self._write_inputs(workspace, files)
            output = workspace / "output"
            output.mkdir(mode=0o700)
            script_path = workspace / ".runner.sh"
            script_path.write_text(script, encoding="utf-8")
            script_path.chmod(0o700)
            for path in workspace.rglob("*"):
                os.chown(path, uid, uid)
            os.chown(workspace, uid, uid)
            os.chown(output, uid, uid)
            return self._run(workspace, script_path, timeout, uid)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
            self._slots.release()

    def _validate_files(self, value: Any) -> dict[PurePosixPath, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise RequestError("INVALID_FILES", "files must be an object")
        if len(value) > self.settings.max_input_files:
            raise RequestError(
                "TOO_MANY_INPUT_FILES", "input file count exceeds limit"
            )
        result: dict[PurePosixPath, str] = {}
        total = 0
        for raw_path, content in value.items():
            if not isinstance(raw_path, str) or not isinstance(content, str):
                raise RequestError(
                    "INVALID_FILES", "file paths and contents must be strings"
                )
            path = _safe_relative_path(raw_path)
            if path.parts[0] == "output":
                raise RequestError(
                    "INVALID_FILE_PATH", "input files cannot use output/"
                )
            total += len(content.encode())
            if total > self.settings.max_input_bytes:
                raise RequestError(
                    "INPUT_TOO_LARGE", "input files exceed size limit"
                )
            result[path] = content
        return result

    def _write_inputs(
        self, workspace: Path, files: dict[PurePosixPath, str]
    ) -> None:
        for relative, content in files.items():
            target = workspace.joinpath(*relative.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def _run(
        self, workspace: Path, script_path: Path, timeout: int, uid: int
    ) -> ExecutionResult:
        stdout_path = workspace / ".stdout"
        stderr_path = workspace / ".stderr"
        env = {
            "HOME": str(workspace),
            "LANG": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PWD": str(workspace),
            "TMPDIR": str(workspace),
            "HTTP_PROXY": self.settings.proxy_url,
            "HTTPS_PROXY": self.settings.proxy_url,
            "ALL_PROXY": self.settings.proxy_url,
            "http_proxy": self.settings.proxy_url,
            "https_proxy": self.settings.proxy_url,
            "all_proxy": self.settings.proxy_url,
            "NO_PROXY": "",
            "no_proxy": "",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        with stdout_path.open("wb") as stdout_file, stderr_path.open(
            "wb"
        ) as stderr_file:
            process = subprocess.Popen(
                [
                    "/usr/bin/prlimit",
                    "--core=0",
                    f"--cpu={timeout + 1}",
                    f"--fsize={self.settings.max_artifact_bytes}",
                    f"--as={self.settings.memory_bytes}",
                    "--nofile=256",
                    f"--nproc={self.settings.max_processes}",
                    "--",
                    "/usr/bin/setpriv",
                    f"--reuid={uid}",
                    f"--regid={uid}",
                    "--clear-groups",
                    "--no-new-privs",
                    "--inh-caps=-all",
                    "--ambient-caps=-all",
                    "--bounding-set=-all",
                    "/bin/bash",
                    str(script_path),
                ],
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            timed_out = False
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                exit_code = process.wait()
            finally:
                self._terminate_uid_processes(uid)

        stdout, stdout_truncated = _read_limited(
            stdout_path, self.settings.max_output_bytes // 2
        )
        stderr, stderr_truncated = _read_limited(
            stderr_path, self.settings.max_output_bytes // 2
        )
        artifacts, artifact_truncated = self._collect_artifacts(
            workspace / "output"
        )
        return ExecutionResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            truncated=(
                stdout_truncated or stderr_truncated or artifact_truncated
            ),
            files=artifacts,
        )

    @staticmethod
    def _terminate_uid_processes(uid: int) -> None:
        for _ in range(3):
            pids: list[int] = []
            for status_path in Path("/proc").glob("[0-9]*/status"):
                try:
                    lines = status_path.read_text().splitlines()
                    uid_line = next(
                        line for line in lines if line.startswith("Uid:")
                    )
                    effective_uid = int(uid_line.split()[2])
                    pid = int(status_path.parent.name)
                except (FileNotFoundError, PermissionError, StopIteration):
                    continue
                if effective_uid != uid:
                    continue
                pids.append(pid)
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(0.01)
            for pid in pids:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            if not pids:
                break

    def _collect_artifacts(
        self, output: Path
    ) -> tuple[tuple[Artifact, ...], bool]:
        artifacts: list[Artifact] = []
        total = 0
        truncated = False
        for scanned, path in enumerate(output.rglob("*"), start=1):
            if scanned > 1000:
                truncated = True
                break
            if path.is_symlink():
                truncated = True
                continue
            if not path.is_file():
                continue
            if len(artifacts) >= self.settings.max_artifacts:
                truncated = True
                break
            size = path.stat().st_size
            if size > self.settings.max_artifact_bytes:
                truncated = True
                continue
            if total + size > self.settings.max_artifact_bytes:
                truncated = True
                break
            data = path.read_bytes()
            total += len(data)
            artifacts.append(
                Artifact(
                    path=path.relative_to(output).as_posix(),
                    mime_type=(
                        mimetypes.guess_type(path.name)[0]
                        or "application/octet-stream"
                    ),
                    data=base64.b64encode(data).decode(),
                    size=len(data),
                )
            )
        return tuple(artifacts), truncated

    def _allocate_uid(self) -> int:
        with self._uid_lock:
            uid = self._next_uid
            self._next_uid += 1
            if self._next_uid > 29_999:
                self._next_uid = 20_000
            return uid


class SandboxHandler(BaseHTTPRequestHandler):
    server: SandboxServer

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/execute":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authenticated():
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"error": {"code": "UNAUTHORIZED", "message": "unauthorized"}},
            )
            return
        try:
            payload = self._read_payload()
            result = self.server.executor.execute(payload)
        except RequestError as exc:
            status = (
                HTTPStatus.TOO_MANY_REQUESTS
                if exc.code == "SANDBOX_BUSY"
                else HTTPStatus.BAD_REQUEST
            )
            self._json(
                status,
                {"error": {"code": exc.code, "message": exc.message}},
            )
            return
        except Exception:
            traceback.print_exc()
            self.log_error("unhandled execute error")
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": {
                        "code": "EXECUTION_FAILED",
                        "message": "execution failed",
                    }
                },
            )
            return
        self._json(HTTPStatus.OK, asdict(result))

    def _authenticated(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            return False
        return hmac.compare_digest(
            authorization[len(prefix) :], self.server.settings.token
        )

    def _read_payload(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise RequestError(
                "INVALID_REQUEST", "Content-Length is required"
            ) from exc
        if not 0 < length <= self.server.settings.max_request_bytes:
            raise RequestError(
                "REQUEST_TOO_LARGE", "request exceeds size limit"
            )
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RequestError("INVALID_JSON", "request must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise RequestError("INVALID_REQUEST", "request must be an object")
        return payload

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


class SandboxServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, settings: Settings, executor: SandboxExecutor) -> None:
        super().__init__(("0.0.0.0", 8080), SandboxHandler)
        self.settings = settings
        self.executor = executor


def _env_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\x00" in value
        or len(value.encode()) > 240
        or value.startswith("/")
        or path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or "." in path.parts
    ):
        raise RequestError("INVALID_FILE_PATH", f"unsafe file path: {value}")
    return path


def _read_limited(path: Path, limit: int) -> tuple[str, bool]:
    size = path.stat().st_size
    with path.open("rb") as file:
        data = file.read(limit)
    return data.decode("utf-8", errors="replace"), size > len(data)


def main() -> None:
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError("failed to enable child subreaper")
    settings = Settings.from_env()
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    SandboxServer(settings, SandboxExecutor(settings)).serve_forever()


if __name__ == "__main__":
    main()

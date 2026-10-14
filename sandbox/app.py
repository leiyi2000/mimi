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


@dataclass(slots=True)
class WorkspaceState:
    path: Path
    uid: int
    lock: threading.RLock


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
        self._workspace_lock = threading.Lock()
        self._workspaces: dict[str, WorkspaceState] = {}
        self._deleted_workspaces: set[str] = set()

    def execute(self, payload: dict[str, Any]) -> ExecutionResult:
        script, timeout = self._validate_execution(payload)
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

    def execute_workspace(
        self, workspace_id: str, payload: dict[str, Any]
    ) -> ExecutionResult:
        script, timeout = self._validate_execution(payload)
        state = self._get_workspace(workspace_id)
        if not self._slots.acquire(blocking=False):
            raise RequestError(
                "SANDBOX_BUSY", "sandbox concurrency limit reached"
            )
        with state.lock:
            control = state.path / ".sandbox"
            script_path = control / f"{uuid.uuid4().hex}.sh"
            try:
                control.mkdir(mode=0o700, exist_ok=True)
                os.chown(control, state.uid, state.uid)
                script_path.write_text(script, encoding="utf-8")
                script_path.chmod(0o700)
                os.chown(script_path, state.uid, state.uid)
                return self._run(
                    state.path,
                    script_path,
                    timeout,
                    state.uid,
                    collect_artifacts=False,
                )
            finally:
                script_path.unlink(missing_ok=True)
                for name in (".stdout", ".stderr"):
                    (state.path / name).unlink(missing_ok=True)
                self._slots.release()

    def write_workspace(
        self, workspace_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        raw_path = payload.get("path")
        content = payload.get("content")
        if not isinstance(raw_path, str) or not isinstance(content, str):
            raise RequestError(
                "INVALID_REQUEST", "path and content must be strings"
            )
        encoded = content.encode()
        if len(encoded) > self.settings.max_input_bytes:
            raise RequestError("INPUT_TOO_LARGE", "content exceeds size limit")
        relative = self._workspace_relative_path(raw_path)
        state = self._get_workspace(workspace_id)
        with state.lock:
            target = self._resolve_workspace_path(
                state, relative, allow_missing=True
            )
            self._create_workspace_parents(state, target.parent)
            if target.exists() and not target.is_file():
                raise RequestError(
                    "INVALID_FILE_PATH", "target must be a regular file"
                )
            temporary = target.with_name(f".sandbox-write-{uuid.uuid4().hex}")
            try:
                temporary.write_bytes(encoded)
                temporary.chmod(0o600)
                os.chown(temporary, state.uid, state.uid)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return {"path": relative.as_posix(), "size": len(encoded)}

    def read_workspace(
        self, workspace_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        raw_path = payload.get("path")
        if not isinstance(raw_path, str):
            raise RequestError("INVALID_REQUEST", "path must be a string")
        relative = self._workspace_relative_path(raw_path)
        state = self._get_workspace(workspace_id)
        with state.lock:
            target = self._resolve_workspace_path(
                state, relative, allow_missing=False
            )
            if not target.is_file():
                raise RequestError(
                    "INVALID_FILE_PATH", "target must be a regular file"
                )
            data, truncated = _read_bytes_limited(
                target, self.settings.max_output_bytes
            )
            size = target.stat().st_size
        return {
            "path": relative.as_posix(),
            "content": data.decode("utf-8", errors="replace"),
            "size": size,
            "truncated": truncated,
        }

    def list_workspace(
        self, workspace_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        raw_path = payload.get("path", "")
        recursive = payload.get("recursive", False)
        if not isinstance(raw_path, str) or not isinstance(recursive, bool):
            raise RequestError(
                "INVALID_REQUEST", "path must be a string and recursive a boolean"
            )
        relative = (
            self._workspace_relative_path(raw_path) if raw_path else None
        )
        state = self._get_workspace(workspace_id)
        with state.lock:
            target = (
                self._resolve_workspace_path(
                    state, relative, allow_missing=False
                )
                if relative is not None
                else state.path
            )
            if not target.is_dir():
                raise RequestError(
                    "INVALID_FILE_PATH", "target must be a directory"
                )
            iterator = target.rglob("*") if recursive else target.iterdir()
            entries: list[dict[str, Any]] = []
            truncated = False
            for path in iterator:
                if path.name in {".sandbox", ".stdout", ".stderr"}:
                    continue
                if path.is_symlink():
                    raise RequestError(
                        "SYMLINK_NOT_ALLOWED",
                        "symbolic links are not allowed",
                    )
                if len(entries) >= 1000:
                    truncated = True
                    break
                entries.append(
                    {
                        "path": path.relative_to(state.path).as_posix(),
                        "type": "directory" if path.is_dir() else "file",
                        "size": path.stat().st_size if path.is_file() else 0,
                    }
                )
        return {"entries": entries, "truncated": truncated}

    def export_workspace(
        self, workspace_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        raw_paths = payload.get("paths")
        if (
            not isinstance(raw_paths, list)
            or not 1 <= len(raw_paths) <= self.settings.max_artifacts
            or any(not isinstance(path, str) for path in raw_paths)
        ):
            raise RequestError(
                "INVALID_REQUEST",
                "paths must be a non-empty list of file paths",
            )
        state = self._get_workspace(workspace_id)
        artifacts: list[Artifact] = []
        total = 0
        with state.lock:
            for raw_path in raw_paths:
                relative = self._workspace_relative_path(raw_path)
                target = self._resolve_workspace_path(
                    state, relative, allow_missing=False
                )
                if not target.is_file():
                    raise RequestError(
                        "INVALID_FILE_PATH",
                        "only regular files can be exported",
                    )
                size = target.stat().st_size
                if (
                    size > self.settings.max_artifact_bytes
                    or total + size > self.settings.max_artifact_bytes
                ):
                    raise RequestError(
                        "ARTIFACT_TOO_LARGE",
                        "export exceeds artifact size limit",
                    )
                data = target.read_bytes()
                total += len(data)
                artifacts.append(
                    Artifact(
                        path=relative.as_posix(),
                        mime_type=(
                            mimetypes.guess_type(target.name)[0]
                            or "application/octet-stream"
                        ),
                        data=base64.b64encode(data).decode(),
                        size=len(data),
                    )
                )
        return {"files": [asdict(artifact) for artifact in artifacts]}

    def delete_workspace(self, workspace_id: str) -> None:
        workspace_id = _validate_workspace_id(workspace_id)
        with self._workspace_lock:
            state = self._workspaces.get(workspace_id)
            self._deleted_workspaces.add(workspace_id)
            if len(self._deleted_workspaces) > 10_000:
                self._deleted_workspaces.pop()
        if state is None:
            return
        with state.lock:
            self._terminate_uid_processes(state.uid)
            shutil.rmtree(state.path, ignore_errors=True)
        with self._workspace_lock:
            self._workspaces.pop(workspace_id, None)

    def _validate_execution(
        self, payload: dict[str, Any]
    ) -> tuple[str, int]:
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
        return script, timeout

    def _get_workspace(self, workspace_id: str) -> WorkspaceState:
        workspace_id = _validate_workspace_id(workspace_id)
        with self._workspace_lock:
            if workspace_id in self._deleted_workspaces:
                raise RequestError(
                    "WORKSPACE_CLOSED", "workspace has already been deleted"
                )
            state = self._workspaces.get(workspace_id)
            if state is not None:
                return state
            path = self.settings.workspace_root / workspace_id
            if path.exists():
                shutil.rmtree(path)
            try:
                path.mkdir(mode=0o700, parents=True)
            except FileExistsError as exc:
                raise RequestError(
                    "WORKSPACE_CONFLICT", "workspace path already exists"
                ) from exc
            uid = self._allocate_uid()
            os.chown(path, uid, uid)
            state = WorkspaceState(path, uid, threading.RLock())
            self._workspaces[workspace_id] = state
            return state

    @staticmethod
    def _workspace_relative_path(value: str) -> PurePosixPath:
        path = _safe_relative_path(value)
        if path.parts[0] == ".sandbox":
            raise RequestError(
                "INVALID_FILE_PATH", "reserved workspace path"
            )
        return path

    @staticmethod
    def _resolve_workspace_path(
        state: WorkspaceState,
        relative: PurePosixPath,
        *,
        allow_missing: bool,
    ) -> Path:
        current = state.path
        for part in relative.parts:
            current = current / part
            if not os.path.lexists(current):
                if allow_missing:
                    continue
                raise RequestError("FILE_NOT_FOUND", "workspace path not found")
            if current.is_symlink():
                raise RequestError(
                    "SYMLINK_NOT_ALLOWED", "symbolic links are not allowed"
                )
        return current

    @staticmethod
    def _create_workspace_parents(
        state: WorkspaceState, parent: Path
    ) -> None:
        missing: list[Path] = []
        current = parent
        while current != state.path and not current.exists():
            missing.append(current)
            current = current.parent
        if current.is_symlink() or not current.is_dir():
            raise RequestError(
                "INVALID_FILE_PATH", "parent must be a regular directory"
            )
        for path in reversed(missing):
            path.mkdir(mode=0o700)
            os.chown(path, state.uid, state.uid)

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
        self,
        workspace: Path,
        script_path: Path,
        timeout: int,
        uid: int,
        *,
        collect_artifacts: bool = True,
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
        if collect_artifacts:
            artifacts, artifact_truncated = self._collect_artifacts(
                workspace / "output"
            )
        else:
            artifacts = ()
            artifact_truncated = False
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
        if not self._authenticated():
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"error": {"code": "UNAUTHORIZED", "message": "unauthorized"}},
            )
            return
        try:
            payload = self._read_payload()
            if self.path == "/v1/execute":
                result: Any = asdict(self.server.executor.execute(payload))
            else:
                workspace_id, action = self._workspace_route()
                operations = {
                    "execute": self.server.executor.execute_workspace,
                    "write": self.server.executor.write_workspace,
                    "read": self.server.executor.read_workspace,
                    "files": self.server.executor.list_workspace,
                    "export": self.server.executor.export_workspace,
                }
                operation = operations.get(action)
                if operation is None:
                    raise RequestError(
                        "NOT_FOUND", "workspace operation not found"
                    )
                operation_result = operation(workspace_id, payload)
                result = (
                    asdict(operation_result)
                    if isinstance(operation_result, ExecutionResult)
                    else operation_result
                )
        except RequestError as exc:
            status = self._request_error_status(exc.code)
            self._json(
                status,
                {"error": {"code": exc.code, "message": exc.message}},
            )
            return
        except Exception:  # noqa: BLE001 - HTTP boundary must return safe errors.
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
        self._json(HTTPStatus.OK, result)

    def do_DELETE(self) -> None:
        if not self._authenticated():
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"error": {"code": "UNAUTHORIZED", "message": "unauthorized"}},
            )
            return
        try:
            workspace_id, action = self._workspace_route(
                allow_workspace_root=True
            )
            if action:
                raise RequestError(
                    "NOT_FOUND", "workspace operation not found"
                )
            self.server.executor.delete_workspace(workspace_id)
        except RequestError as exc:
            self._json(
                self._request_error_status(exc.code),
                {"error": {"code": exc.code, "message": exc.message}},
            )
            return
        self._json(HTTPStatus.OK, {"deleted": True})

    def _workspace_route(
        self, *, allow_workspace_root: bool = False
    ) -> tuple[str, str]:
        parts = self.path.split("?")[0].strip("/").split("/")
        expected = 3 if allow_workspace_root else 4
        if (
            len(parts) != expected
            or parts[:2] != ["v1", "workspaces"]
        ):
            raise RequestError("NOT_FOUND", "route not found")
        workspace_id = _validate_workspace_id(parts[2])
        return workspace_id, parts[3] if len(parts) == 4 else ""

    @staticmethod
    def _request_error_status(code: str) -> HTTPStatus:
        if code == "SANDBOX_BUSY":
            return HTTPStatus.TOO_MANY_REQUESTS
        if code in {"FILE_NOT_FOUND", "NOT_FOUND"}:
            return HTTPStatus.NOT_FOUND
        if code in {"WORKSPACE_CLOSED", "WORKSPACE_CONFLICT"}:
            return HTTPStatus.CONFLICT
        return HTTPStatus.BAD_REQUEST

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


def _validate_workspace_id(value: str) -> str:
    if (
        len(value) != 32
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RequestError(
            "INVALID_WORKSPACE_ID", "workspace id must be 32 lowercase hex chars"
        )
    return value


def _read_bytes_limited(path: Path, limit: int) -> tuple[bytes, bool]:
    size = path.stat().st_size
    with path.open("rb") as file:
        data = file.read(limit)
    return data, size > len(data)


def _read_limited(path: Path, limit: int) -> tuple[str, bool]:
    data, truncated = _read_bytes_limited(path, limit)
    return data.decode("utf-8", errors="replace"), truncated


def main() -> None:
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError("failed to enable child subreaper")
    settings = Settings.from_env()
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    SandboxServer(settings, SandboxExecutor(settings)).serve_forever()


if __name__ == "__main__":
    main()

from __future__ import annotations

import ast
import base64
import json
import os
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


MAX_BODY_BYTES = 64 * 1024
MAX_SCRIPT_CHARS = 32_768
MAX_OUTPUT_CHARS = 20_000
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 8


class RequestError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def validate_script(script: str) -> None:
    if not isinstance(script, str) or not script.strip():
        raise RequestError("SCRIPT_EMPTY")
    if len(script) > MAX_SCRIPT_CHARS:
        raise RequestError("SCRIPT_TOO_LARGE")
    try:
        ast.parse(script, mode="exec")
    except SyntaxError as exc:
        raise RequestError("SCRIPT_SYNTAX_ERROR") from exc


def execute(script: str, timeout: int) -> dict[str, Any]:
    validate_script(script)
    timeout = max(1, min(int(timeout), 30))
    with tempfile.TemporaryDirectory(prefix="pumpkin-run-") as directory:
        workspace = Path(directory)
        output = workspace / "output"
        output.mkdir()
        script_path = workspace / "main.py"
        script_path.write_text(script, encoding="utf-8")
        try:
            process = subprocess.Popen(
                ["python3", str(script_path)],
                cwd=output,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"},
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                timed_out = True
        except OSError as exc:
            raise RequestError("RUNNER_EXECUTION_FAILED", status=500) from exc
        files = []
        total = 0
        truncated = False
        for path in sorted(output.rglob("*")) if output.exists() else ():
            if not path.is_file():
                continue
            relative = path.relative_to(output)
            if len(files) >= MAX_FILES or any(
                part in {".", ".."} for part in relative.parts
            ):
                truncated = True
                continue
            size = path.stat().st_size
            if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_FILE_BYTES:
                truncated = True
                continue
            data = path.read_bytes()
            files.append(
                {
                    "name": str(relative),
                    "content_type": _content_type(path),
                    "data": base64.b64encode(data).decode("ascii"),
                }
            )
            total += size
        stdout, stdout_truncated = _limit_text(stdout)
        stderr, stderr_truncated = _limit_text(stderr)
        return {
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
            "truncated": truncated or stdout_truncated or stderr_truncated,
            "files": files,
        }


def _limit_text(value: str) -> tuple[str, bool]:
    return value[:MAX_OUTPUT_CHARS], len(value) > MAX_OUTPUT_CHARS


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".json": "application/json",
        ".csv": "text/csv",
        ".txt": "text/plain",
    }.get(suffix, "application/octet-stream")


class Handler(BaseHTTPRequestHandler):
    server_version = "pumpkin-code-runner/1.0"

    def do_POST(self) -> None:
        if self.path != "/v1/execute":
            self._send(404, {"error": {"code": "NOT_FOUND"}})
            return
        expected = os.getenv("CODE_RUNNER_TOKEN")
        if expected and self.headers.get("Authorization") != f"Bearer {expected}":
            self._send(401, {"error": {"code": "UNAUTHORIZED"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise RequestError("REQUEST_TOO_LARGE")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise RequestError("REQUEST_INVALID")
            result = execute(payload.get("script"), payload.get("timeout", 30))
        except RequestError as exc:
            self._send(exc.status, {"error": {"code": exc.code}})
            return
        except (TypeError, ValueError, json.JSONDecodeError):
            self._send(400, {"error": {"code": "REQUEST_INVALID"}})
            return
        self._send(200, result)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

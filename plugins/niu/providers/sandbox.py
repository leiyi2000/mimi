from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import httpx

from core.tools import Artifact, Tool, ToolContext, ToolRegistry, ToolResult


@dataclass(frozen=True, slots=True)
class SandboxFile:
    path: str
    mime_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class SandboxResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    files: tuple[SandboxFile, ...]


class SandboxProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class SandboxProvider:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        request_timeout: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.request_timeout = request_timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(request_timeout + 5)
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def execute(
        self,
        *,
        script: str,
        files: dict[str, str],
        timeout: int,
    ) -> SandboxResult:
        try:
            response = await self._client.post(
                f"{self.base_url}/v1/execute",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"script": script, "files": files, "timeout": timeout},
            )
        except httpx.TimeoutException as exc:
            raise SandboxProviderError(
                "SANDBOX_TIMEOUT",
                "沙箱服务响应超时，请缩小任务范围后重试。",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise SandboxProviderError(
                "SANDBOX_UNAVAILABLE",
                "沙箱服务当前不可用，请稍后重试。",
                retryable=True,
            ) from exc

        if response.status_code != 200:
            self._raise_response_error(response)
        try:
            payload = response.json()
            raw_files = payload["files"]
            if not isinstance(raw_files, list):
                raise TypeError("files must be a list")
            result_files = tuple(self._decode_file(item) for item in raw_files)
            return SandboxResult(
                exit_code=payload["exit_code"],
                stdout=str(payload["stdout"]),
                stderr=str(payload["stderr"]),
                timed_out=bool(payload["timed_out"]),
                truncated=bool(payload["truncated"]),
                files=result_files,
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            binascii.Error,
            json.JSONDecodeError,
        ) as exc:
            raise SandboxProviderError(
                "SANDBOX_INVALID_RESPONSE",
                "沙箱服务返回了无效响应。",
                retryable=False,
            ) from exc

    def _raise_response_error(self, response: httpx.Response) -> None:
        code = "SANDBOX_REJECTED"
        message = "沙箱拒绝了本次执行请求。"
        try:
            error = response.json()["error"]
            code = str(error["code"])
            message = str(error["message"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        if response.status_code == 401:
            message = "沙箱服务认证失败，请检查部署配置。"
        raise SandboxProviderError(
            code,
            message,
            retryable=response.status_code in {429, 502, 503, 504},
        )

    @staticmethod
    def _decode_file(value: Any) -> SandboxFile:
        if not isinstance(value, dict):
            raise TypeError("file must be an object")
        path = str(value["path"])
        pure_path = PurePosixPath(path)
        if (
            not path
            or pure_path.is_absolute()
            or ".." in pure_path.parts
            or "." in pure_path.parts
        ):
            raise ValueError("invalid artifact path")
        return SandboxFile(
            path=path,
            mime_type=str(value["mime_type"]),
            data=base64.b64decode(value["data"], validate=True),
        )


class SandboxTools:
    def __init__(
        self, provider: SandboxProvider, *, max_timeout: int
    ) -> None:
        self.provider = provider
        self.max_timeout = max_timeout

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            Tool(
                name="code",
                description=(
                    "Run a Bash script in an isolated Linux workspace with "
                    "Python, pip, curl, Git, controlled internet access, and "
                    "ephemeral files. Save deliverables under output/."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "script": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 65536,
                        },
                        "files": {
                            "type": "object",
                            "additionalProperties": {
                                "type": "string",
                            },
                        },
                        "timeout": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": self.max_timeout,
                        },
                    },
                    "required": ["script"],
                    "additionalProperties": False,
                },
                handler=self.execute,
                timeout=self.max_timeout + 10,
                read_only=False,
                skill_name="code",
            )
        )

    async def execute(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        del context
        script = arguments["script"]
        files = arguments.get("files") or {}
        timeout = arguments.get("timeout", self.max_timeout)
        if not isinstance(script, str) or not script.strip():
            raise ValueError("script must not be empty")
        if not isinstance(files, dict) or not all(
            isinstance(path, str) and isinstance(content, str)
            for path, content in files.items()
        ):
            raise TypeError("files must contain text values")
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= self.max_timeout
        ):
            raise ValueError("invalid timeout")
        try:
            result = await self.provider.execute(
                script=script,
                files=files,
                timeout=timeout,
            )
        except SandboxProviderError as exc:
            return ToolResult.error(
                exc.code,
                exc.message,
                retryable=exc.retryable,
            )

        artifacts = tuple(
            Artifact(
                kind=self._artifact_kind(file.mime_type),
                data=file.data,
                mime_type=file.mime_type,
                title=file.path,
            )
            for file in result.files
        )
        content = json.dumps(
            {
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": result.timed_out,
                "truncated": result.truncated,
                "files": [
                    {
                        "path": file.path,
                        "mime_type": file.mime_type,
                        "size": len(file.data),
                    }
                    for file in result.files
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        failed = result.timed_out or result.exit_code != 0
        return ToolResult(
            content,
            is_error=failed,
            artifacts=artifacts,
            error_code="CODE_TIMEOUT" if result.timed_out else (
                "CODE_EXIT_NONZERO" if failed else None
            ),
        )

    @staticmethod
    def _artifact_kind(mime_type: str) -> str:
        if mime_type.startswith("image/"):
            return "image"
        if mime_type.startswith("audio/"):
            return "audio"
        return "file"

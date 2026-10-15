from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

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
        response = await self._request(
            "POST",
            "/v1/execute",
            {"script": script, "files": files, "timeout": timeout},
        )
        try:
            return self._decode_result(response.json())
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

    async def execute_workspace(
        self,
        *,
        workspace_id: str,
        script: str,
        timeout: int,
    ) -> SandboxResult:
        response = await self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/execute",
            {"script": script, "timeout": timeout},
        )
        try:
            return self._decode_result(response.json())
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

    async def write_workspace(
        self, *, workspace_id: str, path: str, content: str
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/write",
            {"path": path, "content": content},
        )
        return self._response_object(response)

    async def read_workspace(
        self, *, workspace_id: str, path: str
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/read",
            {"path": path},
        )
        return self._response_object(response)

    async def list_workspace(
        self,
        *,
        workspace_id: str,
        path: str,
        recursive: bool,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/files",
            {"path": path, "recursive": recursive},
        )
        return self._response_object(response)

    async def export_workspace(
        self, *, workspace_id: str, paths: list[str]
    ) -> tuple[SandboxFile, ...]:
        response = await self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/export",
            {"paths": paths},
        )
        try:
            payload = response.json()
            raw_files = payload["files"]
            if not isinstance(raw_files, list):
                raise TypeError("files must be a list")
            return tuple(self._decode_file(item) for item in raw_files)
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

    async def delete_workspace(self, workspace_id: str) -> None:
        await self._request(
            "DELETE",
            f"/v1/workspaces/{workspace_id}",
            None,
        )

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
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
        return response

    @classmethod
    def _decode_result(cls, payload: Any) -> SandboxResult:
        if not isinstance(payload, dict):
            raise TypeError("response must be an object")
        raw_files = payload["files"]
        if not isinstance(raw_files, list):
            raise TypeError("files must be a list")
        result_files = tuple(cls._decode_file(item) for item in raw_files)
        return SandboxResult(
            exit_code=payload["exit_code"],
            stdout=str(payload["stdout"]),
            stderr=str(payload["stderr"]),
            timed_out=bool(payload["timed_out"]),
            truncated=bool(payload["truncated"]),
            files=result_files,
        )

    @staticmethod
    def _response_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise SandboxProviderError(
                "SANDBOX_INVALID_RESPONSE",
                "沙箱服务返回了无效响应。",
                retryable=False,
            ) from exc
        if not isinstance(payload, dict):
            raise SandboxProviderError(
                "SANDBOX_INVALID_RESPONSE",
                "沙箱服务返回了无效响应。",
                retryable=False,
            )
        return payload

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
                completion_required=True,
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


class RemoteFileTools:
    def __init__(
        self, provider: SandboxProvider, *, max_timeout: int
    ) -> None:
        self.provider = provider
        self.max_timeout = max_timeout

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            Tool(
                name="attach_remote_file",
                description=(
                    "Download any public HTTPS file and attach its original "
                    "binary content to the current QQ group. Images are sent "
                    "as QQ images by default; other content is sent as a file."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2048,
                            "description": "Direct public HTTPS file URL.",
                        },
                        "filename": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 100,
                            "description": (
                                "Optional display filename without directories."
                            ),
                        },
                        "send_as": {
                            "type": "string",
                            "enum": ["auto", "file", "image"],
                            "description": (
                                "Use auto to send common image types as QQ "
                                "images and everything else as files."
                            ),
                        },
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                handler=self.attach_remote_file,
                timeout=self.max_timeout + 10,
                read_only=False,
                skill_name="reply",
                completion_required=True,
            )
        )

    async def attach_remote_file(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        del context
        url = arguments["url"]
        requested_filename = arguments.get("filename")
        send_as = arguments.get("send_as", "auto")
        if not isinstance(url, str):
            raise TypeError("url must be a string")
        if send_as not in {"auto", "file", "image"}:
            raise ValueError("invalid remote file send mode")
        filename = self._filename(url, requested_filename)
        self._validate_url(url)
        script = (
            "set -eu\n"
            "mkdir -p output\n"
            "curl --fail --silent --show-error --location "
            "--proto '=https' --proto-redir '=https' "
            f"--output output/download -- {shlex.quote(url)}\n"
            "test -s output/download\n"
        )
        try:
            result = await self.provider.execute(
                script=script,
                files={},
                timeout=self.max_timeout,
            )
        except SandboxProviderError as exc:
            return ToolResult.error(
                exc.code,
                exc.message,
                retryable=exc.retryable,
            )
        if result.timed_out:
            return ToolResult.error(
                "REMOTE_FILE_TIMEOUT",
                "文件下载超时，请换一个直链或稍后重试。",
                retryable=True,
            )
        if result.exit_code != 0:
            return ToolResult.error(
                "REMOTE_FILE_DOWNLOAD_FAILED",
                (
                    "文件下载失败。该地址可能是详情页而非文件直链；"
                    "请读取页面资源链接、重新搜索直链或更换公开来源后重试。"
                ),
                retryable=True,
            )
        if result.truncated or len(result.files) != 1:
            return ToolResult.error(
                "REMOTE_FILE_NOT_EXPORTED",
                "文件未能完整导出，可能超过附件大小限制。",
                retryable=False,
            )
        downloaded = result.files[0]
        mime_type = (
            mimetypes.guess_type(filename)[0]
            or downloaded.mime_type
            or "application/octet-stream"
        )
        artifact_kind = (
            "image"
            if send_as == "image"
            or (send_as == "auto" and mime_type.startswith("image/"))
            else "file"
        )
        return ToolResult(
            "IMAGE_ATTACHED" if artifact_kind == "image" else "FILE_ATTACHED",
            artifacts=(
                Artifact(
                    kind=artifact_kind,
                    data=downloaded.data,
                    mime_type=mime_type,
                    title=filename,
                ),
            ),
            suppress_final_text=artifact_kind == "image",
        )

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlsplit(url)
        if (
            len(url) > 2048
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(character in url for character in "\r\n\x00")
        ):
            raise ValueError("invalid remote file URL")

    @staticmethod
    def _filename(url: str, requested: Any) -> str:
        if requested is None:
            requested = PurePosixPath(
                unquote(urlsplit(url).path)
            ).name or "attachment.bin"
        if not isinstance(requested, str):
            raise TypeError("filename must be a string")
        filename = requested.strip()
        if (
            not 1 <= len(filename) <= 100
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or any(character in filename for character in "\r\n\x00")
        ):
            raise ValueError("invalid remote file name")
        return filename


class WorkspaceTools:
    def __init__(
        self, provider: SandboxProvider, *, max_timeout: int
    ) -> None:
        self.provider = provider
        self.max_timeout = max_timeout
        self._used_runs: set[str] = set()

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            Tool(
                name="list_workspace",
                description=(
                    "List files in this Agent run's isolated workspace."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "maxLength": 240},
                        "recursive": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                handler=self.list_workspace,
                read_only=True,
                skill_name="workspace",
            )
        )
        registry.register(
            Tool(
                name="read_workspace_file",
                description=(
                    "Read one UTF-8 text file from this Agent run's isolated "
                    "workspace."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 240,
                        }
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=self.read_workspace_file,
                read_only=True,
                skill_name="workspace",
            )
        )
        registry.register(
            Tool(
                name="write_workspace_file",
                description=(
                    "Create or replace one UTF-8 text file in this Agent run's "
                    "isolated workspace."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 240,
                        },
                        "content": {"type": "string", "maxLength": 524288},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                handler=self.write_workspace_file,
                read_only=False,
                skill_name="workspace",
                completion_required=True,
            )
        )
        registry.register(
            Tool(
                name="run_shell",
                description=(
                    "Run Bash in this Agent run's persistent isolated workspace. "
                    "Files remain available to later workspace tools in the same "
                    "run."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "script": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 65536,
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
                handler=self.run_shell,
                timeout=self.max_timeout + 10,
                read_only=False,
                skill_name="workspace",
                completion_required=True,
            )
        )
        registry.register(
            Tool(
                name="export_workspace",
                description=(
                    "Export one or more regular files from this Agent run's "
                    "isolated workspace as user-visible attachments."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 10,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 240,
                            },
                        }
                    },
                    "required": ["paths"],
                    "additionalProperties": False,
                },
                handler=self.export_workspace,
                read_only=True,
                skill_name="workspace",
                completion_required=True,
            )
        )
        registry.register_run_cleanup(self.cleanup)

    async def list_workspace(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        run_id = self._run_id(context)
        self._used_runs.add(run_id)
        path = arguments.get("path", "")
        recursive = arguments.get("recursive", False)
        if not isinstance(path, str) or not isinstance(recursive, bool):
            raise TypeError("invalid workspace listing")
        try:
            result = await self.provider.list_workspace(
                workspace_id=run_id,
                path=path,
                recursive=recursive,
            )
        except SandboxProviderError as exc:
            return self._provider_error(exc)
        return ToolResult(
            json.dumps(
                result,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    async def read_workspace_file(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        run_id = self._run_id(context)
        self._used_runs.add(run_id)
        path = arguments["path"]
        if not isinstance(path, str) or not path:
            raise ValueError("path must not be empty")
        try:
            result = await self.provider.read_workspace(
                workspace_id=run_id,
                path=path,
            )
        except SandboxProviderError as exc:
            return self._provider_error(exc)
        return ToolResult(
            json.dumps(
                result,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    async def write_workspace_file(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        run_id = self._run_id(context)
        self._used_runs.add(run_id)
        path = arguments["path"]
        content = arguments["content"]
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(content, str)
        ):
            raise TypeError("invalid workspace file")
        try:
            result = await self.provider.write_workspace(
                workspace_id=run_id,
                path=path,
                content=content,
            )
        except SandboxProviderError as exc:
            return self._provider_error(exc)
        return ToolResult(
            json.dumps(
                result,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    async def run_shell(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        run_id = self._run_id(context)
        self._used_runs.add(run_id)
        script = arguments["script"]
        timeout = arguments.get("timeout", self.max_timeout)
        if not isinstance(script, str) or not script.strip():
            raise ValueError("script must not be empty")
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= self.max_timeout
        ):
            raise ValueError("invalid timeout")
        try:
            result = await self.provider.execute_workspace(
                workspace_id=run_id,
                script=script,
                timeout=timeout,
            )
        except SandboxProviderError as exc:
            return self._provider_error(exc)
        failed = result.timed_out or result.exit_code != 0
        return ToolResult(
            json.dumps(
                {
                    "exit_code": result.exit_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "timed_out": result.timed_out,
                    "truncated": result.truncated,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            is_error=failed,
            error_code=(
                "SHELL_TIMEOUT"
                if result.timed_out
                else ("SHELL_EXIT_NONZERO" if failed else None)
            ),
        )

    async def export_workspace(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        run_id = self._run_id(context)
        self._used_runs.add(run_id)
        paths = arguments["paths"]
        if (
            not isinstance(paths, list)
            or not paths
            or any(not isinstance(path, str) or not path for path in paths)
        ):
            raise TypeError("invalid export paths")
        try:
            files = await self.provider.export_workspace(
                workspace_id=run_id,
                paths=paths,
            )
        except SandboxProviderError as exc:
            return self._provider_error(exc)
        artifacts = tuple(
            Artifact(
                kind=SandboxTools._artifact_kind(file.mime_type),
                data=file.data,
                mime_type=file.mime_type,
                title=file.path,
            )
            for file in files
        )
        return ToolResult(
            json.dumps(
                {
                    "files": [
                        {
                            "path": file.path,
                            "mime_type": file.mime_type,
                            "size": len(file.data),
                        }
                        for file in files
                    ]
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            artifacts=artifacts,
        )

    async def cleanup(self, context: ToolContext) -> None:
        if context.run_id not in self._used_runs:
            return
        self._used_runs.discard(context.run_id)
        await self.provider.delete_workspace(context.run_id)

    @staticmethod
    def _run_id(context: ToolContext) -> str:
        if len(context.run_id) != 32:
            raise ValueError("workspace requires an active run")
        return context.run_id

    @staticmethod
    def _provider_error(exc: SandboxProviderError) -> ToolResult:
        return ToolResult.error(
            exc.code,
            exc.message,
            retryable=exc.retryable,
        )

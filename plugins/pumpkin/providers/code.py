from __future__ import annotations

import asyncio
import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

import httpx


class CodeProviderError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class CodeFile:
    name: str
    data: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class CodeResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    files: tuple[CodeFile, ...]


class CodeRunnerProvider:
    def __init__(
        self,
        *,
        base_url: str,
        token: str | None,
        timeout: int,
        max_output_chars: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self._client = client

    async def execute(self, script: str, timeout: int) -> CodeResult:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = {"script": script, "timeout": timeout}
        for attempt in range(2):
            try:
                if self._client is not None:
                    response = await self._client.post(
                        f"{self.base_url}/v1/execute", json=payload, headers=headers
                    )
                else:
                    async with httpx.AsyncClient(
                        timeout=timeout + 5, trust_env=False
                    ) as client:
                        response = await client.post(
                            f"{self.base_url}/v1/execute",
                            json=payload,
                            headers=headers,
                        )
            except (httpx.HTTPError, TimeoutError) as exc:
                if attempt == 0:
                    await asyncio.sleep(0.1)
                    continue
                raise CodeProviderError(
                    "CODE_RUNNER_UNAVAILABLE",
                    retryable=False,
                ) from exc
            if response.status_code in {429, *range(500, 600)}:
                if attempt == 0:
                    await asyncio.sleep(0.1)
                    continue
                raise CodeProviderError(
                    "CODE_RUNNER_RATE_LIMITED"
                    if response.status_code == 429
                    else "CODE_RUNNER_UNAVAILABLE",
                    retryable=False,
                )
            break
        if response.status_code == 401:
            raise CodeProviderError("CODE_RUNNER_UNAUTHORIZED")
        if response.status_code >= 400:
            raise CodeProviderError(self._error_code(response))
        try:
            return self._decode_result(response.json())
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            binascii.Error,
        ) as exc:
            raise CodeProviderError("CODE_RUNNER_INVALID_RESPONSE") from exc

    def _decode_result(self, payload: Any) -> CodeResult:
        if not isinstance(payload, dict):
            raise TypeError("code runner response must be an object")
        return CodeResult(
            exit_code=int(payload["exit_code"]),
            stdout=str(payload.get("stdout", ""))[: self.max_output_chars],
            stderr=str(payload.get("stderr", ""))[: self.max_output_chars],
            timed_out=bool(payload.get("timed_out", False)),
            truncated=bool(payload.get("truncated", False)),
            files=tuple(
                CodeFile(
                    name=str(item.get("name") or item["path"]),
                    data=base64.b64decode(item["data"], validate=True),
                    content_type=str(
                        item.get("content_type")
                        or item.get("mime_type")
                        or "application/octet-stream"
                    ),
                )
                for item in payload.get("files", [])
            ),
        )

    @staticmethod
    def _error_code(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except (TypeError, ValueError, json.JSONDecodeError):
            return "CODE_REQUEST_REJECTED"
        if not isinstance(payload, dict):
            return "CODE_REQUEST_REJECTED"
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if not isinstance(code, str) or not code.strip():
            return "CODE_REQUEST_REJECTED"
        normalized = code.strip().upper()
        if not all(character.isalnum() or character == "_" for character in normalized):
            return "CODE_REQUEST_REJECTED"
        return normalized

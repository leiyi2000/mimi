from __future__ import annotations

import asyncio
import json
import keyword
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from pydantic_monty import (
    AsyncMonty,
    MontyConversionError,
    MontyCrashedError,
    MontyRuntimeError,
    MontySyntaxError,
    MontyTypingError,
)

from core.config import Config
from core.tools import Tool, ToolContext, ToolRegistry, ToolResult


@dataclass(frozen=True, slots=True)
class CodeResult:
    value: Any = None
    stdout: str = ""
    stderr: str = ""
    error_type: str | None = None
    timed_out: bool = False
    truncated: bool = False


class _OutputCollector:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.size = 0
        self.truncated = False

    def __call__(self, stream: str, text: str) -> None:
        encoded = text.encode("utf-8")
        remaining = self.max_bytes - self.size
        if remaining <= 0:
            self.truncated = True
            return
        if len(encoded) > remaining:
            text = encoded[:remaining].decode("utf-8", errors="ignore")
            encoded = text.encode("utf-8")
            self.truncated = True
        target = self.stderr if stream == "stderr" else self.stdout
        target.append(text)
        self.size += len(encoded)


class MontyRunner:
    def __init__(self, config: Config, tools: ToolRegistry) -> None:
        self.config = config
        self.tools = tools
        self._pool: AsyncMonty | None = None
        self._pool_lock = asyncio.Lock()

    def register(self) -> None:
        names = sorted(self.tools.read_only_names())
        self.tools.register(
            Tool(
                name="code",
                description=(
                    "Run pure Python-subset code in Monty for calculation, JSON "
                    "transformation, filtering, aggregation, or orchestration of "
                    f"these read-only tools: {', '.join(names) or 'none'}. "
                    "No filesystem, network, environment, packages, or subprocess."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "maxLength": 10000},
                        "inputs": {"type": "object"},
                        "allowed_tools": {
                            "type": "array",
                            "items": {"type": "string", "enum": names},
                            "maxItems": len(names),
                        },
                    },
                    "required": ["code"],
                    "additionalProperties": False,
                },
                handler=self.execute,
                timeout=self.config.monty_request_timeout + 1,
                read_only=True,
                skill_name="code",
            )
        )

    async def start(self) -> None:
        if self._pool is not None:
            return
        async with self._pool_lock:
            if self._pool is not None:
                return
            pool = AsyncMonty(
                binary_path=self.config.monty_binary_path,
                min_processes=1,
                max_processes=self.config.monty_max_processes,
                request_timeout=self.config.monty_request_timeout,
                max_checkouts_per_worker=100,
            )
            await pool.__aenter__()
            self._pool = pool

    async def close(self) -> None:
        async with self._pool_lock:
            pool = self._pool
            self._pool = None
            if pool is not None:
                await pool.__aexit__(None, None, None)

    async def execute(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        code = str(arguments["code"])
        if not code or len(code) > 10000:
            raise ValueError("invalid code")
        inputs = arguments.get("inputs") or {}
        if not isinstance(inputs, dict):
            raise TypeError("inputs must be an object")
        if len(json.dumps(inputs, ensure_ascii=False)) > 32768:
            raise ValueError("inputs too large")

        requested = set(arguments.get("allowed_tools") or [])
        allowed = requested & self.tools.read_only_names()
        calls = 0
        execution_id = uuid.uuid4().hex

        async def call_tool(name: str, tool_arguments: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            if name not in allowed:
                return {"content": "TOOL_NOT_ALLOWED", "is_error": True}
            calls += 1
            if calls > self.config.monty_max_host_calls:
                return {"content": "HOST_CALL_LIMIT", "is_error": True}
            result = await self.tools.execute(
                tool_use_id=f"monty-{execution_id}-{calls}",
                name=name,
                arguments=tool_arguments,
                context=context,
            )
            return {"content": result.content, "is_error": result.is_error}

        await self.start()
        assert self._pool is not None
        collector = _OutputCollector(self.config.monty_output_kb * 1024)
        external_lookup = {"call_tool": call_tool} if allowed else {}
        stub_lines = ["from typing import Any"]
        stub_lines.extend(
            f"{name}: Any"
            for name in inputs
            if name.isidentifier() and not keyword.iskeyword(name)
        )
        if allowed:
            stub_lines.append(
                "async def call_tool(name: str, arguments: dict[str, Any]) "
                "-> dict[str, Any]: ..."
            )
        stubs = "\n".join(stub_lines)
        try:
            async with self._pool.checkout(
                script_name="agent_code.py",
                limits={
                    "max_memory": self.config.monty_memory_mb * 1024 * 1024,
                    "max_feed_duration_secs": self.config.monty_feed_timeout,
                    "max_turn_duration_secs": self.config.monty_feed_timeout,
                    "max_suspensions": self.config.monty_max_host_calls + 8,
                    "max_total_sleep_secs": 0,
                },
                type_check=True,
                type_check_stubs=stubs,
                os_policy={"sleep": "zero", "random_start": {"seed": 0}},
            ) as session:
                value = await session.feed_run(
                    code,
                    inputs=inputs,
                    external_lookup=external_lookup,
                    print_callback=collector,
                )
            result = CodeResult(
                value=value,
                stdout="".join(collector.stdout),
                stderr="".join(collector.stderr),
                truncated=collector.truncated,
            )
        except (MontySyntaxError, MontyTypingError):
            result = CodeResult(error_type="CODE_UNSUPPORTED")
        except MontyConversionError:
            result = CodeResult(error_type="CODE_INPUT_INVALID")
        except MontyCrashedError as exc:
            timed_out = bool(getattr(exc, "timed_out", False))
            result = CodeResult(
                error_type="CODE_TIMEOUT" if timed_out else "WORKER_CRASHED",
                timed_out=timed_out,
            )
        except MontyRuntimeError as exc:
            error = exc.exception()
            if isinstance(error, TimeoutError):
                result = CodeResult(error_type="CODE_TIMEOUT", timed_out=True)
            elif isinstance(error, MemoryError):
                result = CodeResult(error_type="CODE_MEMORY_LIMIT")
            else:
                result = CodeResult(error_type="CODE_RUNTIME_ERROR")
        except TimeoutError:
            result = CodeResult(error_type="CODE_TIMEOUT", timed_out=True)

        return ToolResult(
            json.dumps(asdict(result), ensure_ascii=False, default=str),
            is_error=result.error_type is not None,
        )

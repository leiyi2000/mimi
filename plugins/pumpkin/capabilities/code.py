from __future__ import annotations

import hashlib
from typing import Any, cast

from core.config import PumpkinConfig
from core.domain import AgentRole, ToolContext, ToolResult
from core.plugins import PluginContext, ServiceKey
from core.tools import Tool, ToolRegistry
from providers.code import CodeProviderError, CodeRunnerProvider

from .base import CapabilityHost, error, result, task_error


class CodeCapabilityPlugin:
    name = "code"
    requires = frozenset(
        {
            ServiceKey.CONFIG,
            ServiceKey.TOOL_REGISTRY,
            ServiceKey.CAPABILITY_HOST,
        }
    )

    def __init__(self) -> None:
        self.provider: CodeRunnerProvider | None = None

    def setup(self, context: PluginContext) -> None:
        config = cast(PumpkinConfig, context.require(ServiceKey.CONFIG))
        registry = cast(
            ToolRegistry,
            context.require(ServiceKey.TOOL_REGISTRY),
        )
        host = cast(
            CapabilityHost,
            context.require(ServiceKey.CAPABILITY_HOST),
        )
        provider = CodeRunnerProvider(
            base_url=config.code.base_url,
            token=config.code.token,
            timeout=config.code.timeout_seconds,
            max_output_chars=config.code.max_output_chars,
        )
        self.provider = provider
        context.provide(ServiceKey.CODE_PROVIDER, provider)
        context.effect(
            registry.register(
                Tool(
                    name="execute_code",
                    description="在独立、无宿主挂载的受限代码沙箱中执行短脚本。",
                    input_schema={
                        "type": "object",
                        "required": ["script", "idempotency_key"],
                        "properties": {
                            "script": {"type": "string"},
                            "timeout": {"type": "integer"},
                            "idempotency_key": {"type": "string"},
                        },
                    },
                    handler=lambda tool_context, arguments: self.execute(
                        host,
                        tool_context,
                        arguments,
                    ),
                    skill_name="code-execution",
                    read_only=False,
                    timeout=45,
                    idempotency_required=True,
                    allowed_roles=frozenset({AgentRole.TASK}),
                )
            )
        )

    async def execute(
        self,
        host: CapabilityHost,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        if invalid := task_error(host, context):
            return invalid
        key = host.idempotency_key(
            context,
            "code",
            str(arguments["idempotency_key"]),
        )
        script = str(arguments["script"])
        timeout = int(
            arguments.get("timeout", host.config.code.timeout_seconds)
        )
        if not script.strip() or len(script) > host.config.code.max_script_chars:
            return error("代码为空或超过长度限制", "CODE_REQUEST_INVALID")
        if not 1 <= timeout <= host.config.code.timeout_seconds:
            return error("代码执行超时参数无效", "CODE_REQUEST_INVALID")
        reserved, cached = await host.store.idempotency_reserve(key)
        if not reserved and cached.get("_status") == "in_progress":
            return error(
                "代码执行结果未知，禁止自动重复调用",
                "CODE_OUTCOME_UNKNOWN",
            )
        if not reserved:
            artifacts = await host.store.get_artifacts(
                task_id=context.task_id or "",
                revision=context.revision or 0,
                scope_id=context.scope_id,
                artifact_ids=tuple(
                    str(item["artifact_id"])
                    for item in cached.get("files", [])
                ),
            )
            failed = bool(cached["timed_out"]) or int(cached["exit_code"]) != 0
            return result(
                cached,
                artifacts=artifacts,
                is_error=failed,
                error_code=(
                    "CODE_TIMEOUT"
                    if cached["timed_out"]
                    else ("CODE_EXIT_NONZERO" if failed else None)
                ),
            )
        try:
            execution = await self._provider().execute(script, timeout)
        except CodeProviderError as exc:
            await host.store.idempotency_release(key)
            return error(
                {
                    "message": "代码沙箱暂时不可用，请稍后重试。",
                    "reason_code": exc.code,
                },
                exc.code,
                retryable=exc.retryable,
            )
        artifacts = []
        try:
            for item in execution.files:
                artifacts.append(
                    await host.write_artifact(
                        context,
                        filename=item.name,
                        kind=(
                            "image"
                            if item.content_type.startswith("image/")
                            else "file"
                        ),
                        content=item.data,
                        strategy_ref={
                            "tool": "execute_code",
                            "script_sha256": hashlib.sha256(
                                script.encode("utf-8")
                            ).hexdigest(),
                        },
                    )
                )
        except (OSError, ValueError) as exc:
            await host.store.idempotency_release(key)
            return error(str(exc), "ARTIFACT_WRITE_FAILED")
        failed = execution.timed_out or execution.exit_code != 0
        payload = {
            "exit_code": execution.exit_code,
            "stdout": execution.stdout,
            "stderr": execution.stderr,
            "timed_out": execution.timed_out,
            "truncated": execution.truncated,
            "files": [
                {
                    "artifact_id": item.artifact_id,
                    "name": item.name,
                    "kind": item.kind,
                    "size_bytes": item.size_bytes,
                }
                for item in artifacts
            ],
        }
        await host.store.idempotency_put(key, payload)
        return result(
            payload,
            artifacts=tuple(artifacts),
            is_error=failed,
            error_code=(
                "CODE_TIMEOUT"
                if execution.timed_out
                else ("CODE_EXIT_NONZERO" if failed else None)
            ),
        )

    def _provider(self) -> CodeRunnerProvider:
        if self.provider is None:
            raise RuntimeError("code capability is not mounted")
        return self.provider

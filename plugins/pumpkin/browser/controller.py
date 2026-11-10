import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import MISSING, fields, replace

from core.agent_loop import AgentLoop, AgentLoopProfile, AgentLoopRequest, AgentLoopStep
from core.config import (
    BrowserConfig, DEFAULT_BROWSER_MAX_STEPS, DEFAULT_BROWSER_TIMEOUT_SECONDS,
    DEFAULT_BROWSER_MODEL_TIMEOUT_SECONDS,
)
from core.domain import AgentProfile, AgentRole, ModelMessage, ToolCall
from providers.browser_worker import BrowserWorkerClient, BrowserWorkerError
from providers.model import ModelProvider, ModelProviderError
from .navigation import BrowserUrl, BrowserUrlError

from .code_validator import BrowserCodeValidator
from .gate import BrowserResultGate
from .models import (
    BrowserAgentError, BrowserArtifactMode, BrowserEvidenceRequirement, BrowserExecutionState,
    BrowserExpectedOutput, BrowserInteractionMode, BrowserRequiredCapability, BrowserRunResult,
    BrowserTaskContract,
)
from .sdk_protocol import MAX_PROGRAMS, SDK_VERSION, Element, SdkError
from .session import BrowserWorkerSession
from .tools import (
    BROWSER_SYSTEM_PROMPT, COMPILER_SYSTEM_PROMPT, EXECUTE_TOOL, FINISH_TOOL,
    PLAN_TOOL, REJECT_TOOL,
)


_LOGGER = logging.getLogger(__name__)


class BrowserAgentRunner:
    def __init__(
        self, *, provider: ModelProvider, worker: BrowserWorkerClient,
        max_steps: int = DEFAULT_BROWSER_MAX_STEPS, max_programs: int = MAX_PROGRAMS,
        timeout_seconds: float = DEFAULT_BROWSER_TIMEOUT_SECONDS,
        model_timeout_seconds: float = DEFAULT_BROWSER_MODEL_TIMEOUT_SECONDS,
        policy: BrowserConfig | None = None, trace: Callable[[str], None] | None = None,
    ) -> None:
        self.worker = worker
        self.max_steps, self.max_programs, self.timeout_seconds = max_steps, max_programs, timeout_seconds
        self.policy, self.trace = policy or BrowserConfig(), trace
        if model_timeout_seconds <= 0:
            raise ValueError("browser model timeout must be positive")
        self.model_timeout_seconds = model_timeout_seconds
        self.result_gate = BrowserResultGate()
        self.agent_loop = AgentLoop(
            AgentLoopProfile(
                profile=AgentProfile.BROWSER, tool_role=AgentRole.BROWSER,
                provider_mode=AgentRole.BROWSER.value, system_prompt=BROWSER_SYSTEM_PROMPT,
                completion_tools=frozenset({FINISH_TOOL.name}),
                max_steps=max_steps, min_tool_calls_per_step=1, max_tool_calls_per_step=1,
                allow_parallel_tools=False,
            ), provider, trace,
        )

    async def run(
        self, *, goal: str, remaining_seconds: float | None = None,
        original_requests: tuple[str, ...] = (), task_owner: str = "",
    ) -> BrowserRunResult:
        state = BrowserExecutionState()
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 1_000:
            return self.result_gate.failure(state, "浏览器目标长度无效。", "BROWSER_GOAL_INVALID")
        return await self._bounded_run(
            state, None, goal.strip(), original_requests, remaining_seconds, task_owner,
        )

    async def run_contract(
        self, *, goal: str, start_url: str, expected_output: BrowserExpectedOutput,
        interaction_mode: BrowserInteractionMode, artifact_mode: BrowserArtifactMode,
        remaining_seconds: float | None = None, original_requests: tuple[str, ...] = (),
        task_owner: str = "", required_capabilities: tuple[BrowserRequiredCapability | str, ...] = (),
        required_evidence: tuple[BrowserEvidenceRequirement, ...] = (),
    ) -> BrowserRunResult:
        state = BrowserExecutionState()
        try:
            contract = self._contract(
                goal, start_url, expected_output, interaction_mode, artifact_mode,
                original_requests, required_capabilities, required_evidence,
            )
        except (ValueError, BrowserAgentError) as exc:
            return self.result_gate.failure(state, "浏览器契约无效。", getattr(exc, "code", "BROWSER_CONTRACT_INVALID"))
        return await self._bounded_run(
            state, contract, goal, original_requests, remaining_seconds, task_owner,
        )

    async def _bounded_run(
        self, state: BrowserExecutionState, contract: BrowserTaskContract | None,
        goal: str, original_requests: tuple[str, ...], remaining_seconds: float | None,
        task_owner: str,
    ) -> BrowserRunResult:
        timeout = min(
            self.timeout_seconds,
            remaining_seconds - self.policy.parent_reserve_seconds
            if remaining_seconds is not None else self.timeout_seconds,
        )
        if timeout <= 0:
            return self.result_gate.failure(
                state, "父任务没有留下足够的浏览器执行时间。", "BROWSER_PARENT_DEADLINE", retryable=True,
            )
        session = BrowserWorkerSession(self.worker, self.policy, self.max_programs, self.trace, contract)
        deadline = asyncio.get_running_loop().time() + timeout
        execution_deadline = deadline - min(self.policy.program_timeout_seconds, timeout / 2)
        try:
            async with asyncio.timeout_at(execution_deadline):
                result = await self._drive(
                    state, session, goal, original_requests, task_owner,
                    execution_deadline,
                )
            if result.error_code in {"MODEL_MAX_TOKENS", "BROWSER_BUDGET_EXHAUSTED", "BROWSER_TIMEOUT"}:
                recovered = await self._finalize(session, state, result.error_code, deadline)
                if recovered is not None:
                    return recovered
                return replace(result, execution_report=state.report())
            return result
        except TimeoutError:
            recovered = await self._finalize(session, state, "BROWSER_TIMEOUT", deadline)
            if recovered is not None:
                return recovered
            return self.result_gate.failure(state, "浏览器任务达到总时限，执行已停止。", "BROWSER_TIMEOUT")
        except (BrowserAgentError, BrowserWorkerError, ModelProviderError) as exc:
            if exc.code in {"MODEL_TIMEOUT", "MODEL_MAX_TOKENS"} and session.contract and session.run:
                recovered = await self._finalize(session, state, exc.code, deadline)
                if recovered is not None:
                    return recovered
            return self.result_gate.failure(
                state, "浏览器执行失败，已停止后续操作。", exc.code,
                retryable=exc.retryable and state.program_count == 0,
            )
        finally:
            try:
                await session.close()
            except BrowserWorkerError as exc:
                _LOGGER.warning("Browser cleanup failed: %s", exc.code)

    async def _drive(
        self, state: BrowserExecutionState, session: BrowserWorkerSession,
        goal: str, original_requests: tuple[str, ...], task_owner: str, deadline: float,
    ) -> BrowserRunResult:
        if session.contract:
            await self._start(state, session, task_owner, deadline)

        execution_allowed = False
        finish_failure: BrowserRunResult | None = None

        def request_for_step(index: int) -> AgentLoopRequest:
            nonlocal execution_allowed
            session.check()
            state.decision_count = index
            now = asyncio.get_running_loop().time()
            remaining = max(0, deadline - now)
            execution_seconds = max(0, remaining - min(self.policy.cleanup_timeout_seconds, remaining / 2))
            execution_allowed = (
                index < self.max_steps and state.program_count < self.max_programs
                and not state.outcome_unknown and state.finish_attempts == 0
                and execution_seconds > 0
            )
            model_deadline = now + min(
                self.model_timeout_seconds,
                execution_seconds if session.contract is None or execution_allowed else remaining,
            )
            if session.contract is None:
                return AgentLoopRequest((
                    ModelMessage("system", COMPILER_SYSTEM_PROMPT),
                    ModelMessage("user", json.dumps({
                        "goal": goal, "original_user_requests": original_requests,
                    }, ensure_ascii=False)),
                ), (PLAN_TOOL, REJECT_TOOL), deadline=model_deadline)
            return AgentLoopRequest((
                ModelMessage("system", BROWSER_SYSTEM_PROMPT),
                ModelMessage("user", json.dumps({
                    "contract": session.contract.payload(),
                    "original_user_requests": session.contract.original_requests,
                    "initial_observation": self._model_observation(state.current_observation),
                    "last_program": {key: value for key, value in state.last_program.items()
                                     if key != "final_observation"},
                    "action_ledger": state.actions,
                    "outcome_unknown": state.outcome_unknown,
                    "remaining_programs": self.max_programs - state.program_count,
                    "remaining_decisions": self.max_steps - index + 1,
                    "remaining_seconds": round(remaining, 3),
                    "remaining_execution_seconds": round(execution_seconds, 3),
                    "program_history": state.program_history,
                    "program_history_omitted": state.program_history_omitted,
                    "finish_error": state.finish_error,
                }, ensure_ascii=False, separators=(",", ":"))),
            ), (EXECUTE_TOOL, FINISH_TOOL) if execution_allowed else (FINISH_TOOL,),
                deadline=model_deadline)

        async def handle_step(step: AgentLoopStep) -> BrowserRunResult | None:
            nonlocal finish_failure
            session.check()
            if step.tool_call_error:
                state.decision_errors += 1
                if state.decision_errors >= 2:
                    return self.result_gate.failure(state, "浏览器连续返回无效决策。", "BROWSER_DECISION_INVALID")
                return None
            if step.response.finish_reason in {"length", "max_tokens"}:
                return self.result_gate.failure(state, "浏览器模型达到输出上限。", "MODEL_MAX_TOKENS")
            call = step.response.message.tool_calls[0]
            if session.contract is None:
                compiled = self._compile(state, call, goal, original_requests)
                if isinstance(compiled, BrowserRunResult):
                    return compiled
                session.contract = compiled
                await self._start(state, session, task_owner, deadline)
                return None
            if call.name == FINISH_TOOL.name:
                state.finish_attempts += 1
                result = await self.result_gate.finish(session, state, session.contract, call.arguments)
                if (
                    state.finish_error and state.finish_attempts == 1
                    and step.index < self.max_steps and not state.outcome_unknown
                ):
                    finish_failure = result
                    return None
                if finish_failure is not None and result.error_code == "BROWSER_GOAL_NOT_COMPLETED":
                    return replace(
                        finish_failure, steps=state.decision_count, execution_report=state.report(),
                    )
                return result
            if call.name != EXECUTE_TOOL.name or not execution_allowed:
                return self.result_gate.failure(state, "浏览器返回不支持的工具。", "BROWSER_TOOL_REJECTED")
            program = call.arguments.get("program")
            try:
                if set(call.arguments) != {"program"}:
                    raise ValueError("invalid program arguments")
                if isinstance(program, str) and len(program.encode()) > self.policy.program_max_chars:
                    raise ValueError("program exceeds configured source limit")
                BrowserCodeValidator().validate(program)
            except (SdkError, ValueError) as exc:
                state.decision_errors += 1
                state.last_program = {"status": "rejected", "error": (
                    exc.fault.payload() if isinstance(exc, SdkError) else {
                        "code": "BROWSER_PROGRAM_INVALID", "message": str(exc), "field": "program",
                    }
                )}
                state.remember_program(program if isinstance(program, str) else "", state.last_program)
                if state.decision_errors >= 2:
                    return self.result_gate.failure(state, "浏览器程序连续校验失败。", "BROWSER_PROGRAM_INVALID")
                return None
            state.decision_errors = 0
            execution_seconds = deadline - asyncio.get_running_loop().time()
            if execution_seconds <= 0:
                return self.result_gate.failure(state, "浏览器执行时间不足。", "BROWSER_TIMEOUT")
            result = await session.execute(state, program, timeout_seconds=execution_seconds)
            state.remember_program(program, result)
            return None

        return await self.agent_loop.run(
            request_for_step=request_for_step, handle_step=handle_step,
            budget_fallback=lambda: replace(
                finish_failure, steps=state.decision_count, execution_report=state.report(),
            ) if finish_failure is not None else self.result_gate.failure(
                state, "浏览器决策预算已用尽。", "BROWSER_BUDGET_EXHAUSTED",
            ), max_steps=self.max_steps, run_id=state.run_id,
        )

    async def _finalize(
        self, session: BrowserWorkerSession, state: BrowserExecutionState,
        reason_code: str, deadline: float,
    ) -> BrowserRunResult | None:
        if session.contract is None or session.run is None:
            return None
        deadline = min(deadline, asyncio.get_running_loop().time() + self.policy.program_timeout_seconds)
        try:
            async with asyncio.timeout_at(deadline):
                session.check()
                capture = await session.worker.capture(
                    session.run, mode=session.contract.artifact_mode.value,
                    full_page=BrowserRequiredCapability.FULL_PAGE_SCREENSHOT in session.contract.required_capabilities,
                )
                state.observe(capture.get("final_observation"))
                state.outcome_unknown |= bool(capture.get("outcome_unknown"))
                if (
                    state.outcome_unknown or state.current_observation.get("dialog")
                    or state.decision_count >= self.max_steps
                    or any(item.get("status") == "pending" for item in state.actions)
                ):
                    return None
                state.decision_count += 1
                request = AgentLoopRequest((
                    ModelMessage("system", BROWSER_SYSTEM_PROMPT),
                    ModelMessage("user", json.dumps({
                        "contract": session.contract.payload(),
                        "original_user_requests": session.contract.original_requests,
                        "initial_observation": self._model_observation(state.current_observation),
                        "action_ledger": state.actions,
                        "last_program": {key: value for key, value in state.last_program.items()
                                         if key != "final_observation"},
                        "program_history": state.program_history,
                        "program_history_omitted": state.program_history_omitted,
                        "outcome_unknown": state.outcome_unknown,
                        "finish_error": state.finish_error,
                        "remaining_programs": 0, "remaining_decisions": 1,
                        "finalization": {
                            "reason_code": reason_code,
                            "instruction": (
                                "Only verify the original goal against this captured final page and action ledger. "
                                "No further page actions are available. Report remaining gaps explicitly."
                            ),
                        },
                    }, ensure_ascii=False, separators=(",", ":"))),
                ), (FINISH_TOOL,), deadline=min(
                    deadline, asyncio.get_running_loop().time() + self.model_timeout_seconds,
                ))
                async for iteration in self.agent_loop.steps(max_steps=1, run_id=state.run_id):
                    step = await iteration.respond(request)
                    if step.tool_call_error or step.response.finish_reason in {"length", "max_tokens"}:
                        return None
                    call = step.response.message.tool_calls[0]
                    if call.name != FINISH_TOOL.name:
                        return None
                    return await self.result_gate.finish(
                        session, state, session.contract, call.arguments, capture=capture,
                    )
        except (TimeoutError, BrowserWorkerError, BrowserAgentError, ModelProviderError) as exc:
            _LOGGER.warning("Browser final verification failed: %s", getattr(exc, "code", "BROWSER_TIMEOUT"))
            return None

    @staticmethod
    def _model_observation(snapshot: dict) -> dict:
        if not snapshot:
            return snapshot
        defaults = {
            item.name: item.default for item in fields(Element)
            if item.default is not MISSING
        }
        defaults.update(role="generic", name="")
        elements = snapshot["elements"]
        if elements:
            defaults.update(
                frame_id=elements[0]["frame_id"],
                document_epoch=elements[0]["document_epoch"],
            )
        defaults = json.loads(json.dumps(defaults))
        return {
            **snapshot, "element_defaults": defaults,
            "elements": [
                {key: value for key, value in element.items()
                 if key not in defaults or json.loads(json.dumps(value)) != defaults[key]}
                for element in elements
            ],
        }

    async def _start(
        self, state: BrowserExecutionState, session: BrowserWorkerSession,
        task_owner: str, deadline: float,
    ) -> None:
        assert session.contract
        try:
            BrowserUrl.validate(session.contract.start_url)
        except BrowserUrlError as exc:
            raise BrowserAgentError(exc.code, "浏览器起始地址不可访问。") from exc
        state.task_contract = session.contract.payload()
        run = await session.acquire(deadline, task_owner)
        if run.sdk_version != SDK_VERSION:
            raise BrowserAgentError("BROWSER_WORKER_PROTOCOL_ERROR", "浏览器 SDK 版本不匹配。")
        missing = {item.value for item in session.contract.required_capabilities} - run.capabilities
        if missing:
            raise BrowserAgentError("BROWSER_CAPABILITY_UNSUPPORTED", "浏览器缺少任务要求的能力。")
        state.run_id, state.engine = run.run_id, run.engine
        state.playwright_version, state.chromium_version = run.playwright_version, run.chromium_version
        state.observe(run.initial_observation)
        state.last_confirmed_action_seq = run.initial_observation["observed_after_action_seq"]

    def _compile(
        self, state: BrowserExecutionState, call: ToolCall, goal: str,
        original_requests: tuple[str, ...],
    ) -> BrowserTaskContract | BrowserRunResult:
        if call.name == REJECT_TOOL.name:
            codes = REJECT_TOOL.input_schema["properties"]["error_code"]["enum"]
            code = call.arguments.get("error_code")
            return self.result_gate.failure(
                state, str(call.arguments.get("reason") or "浏览器目标不允许执行。"),
                code if code in codes else "BROWSER_TASK_NOT_ALLOWED",
            )
        try:
            if call.name != PLAN_TOOL.name:
                raise ValueError("invalid compiler tool")
            values = call.arguments
            return self._contract(
                goal, values["start_url"], BrowserExpectedOutput(values["expected_output"]),
                BrowserInteractionMode(values["interaction_mode"]), BrowserArtifactMode(values["artifact_mode"]),
                original_requests, tuple(values["required_capabilities"]), (),
            )
        except (KeyError, TypeError, ValueError, BrowserAgentError) as exc:
            return self.result_gate.failure(
                state, "浏览器目标生成的契约无效。", getattr(exc, "code", "BROWSER_CONTRACT_INVALID"),
            )

    @staticmethod
    def _contract(
        goal: str, start_url: str, expected_output: BrowserExpectedOutput,
        interaction_mode: BrowserInteractionMode, artifact_mode: BrowserArtifactMode,
        original_requests: tuple[str, ...], required_capabilities: tuple[BrowserRequiredCapability | str, ...],
        required_evidence: tuple[BrowserEvidenceRequirement, ...],
    ) -> BrowserTaskContract:
        if not 1 <= len(goal.strip()) <= 1_000:
            raise BrowserAgentError("BROWSER_CONTRACT_INVALID", "浏览器目标长度无效。")
        BrowserUrl.validate(start_url)
        if (
            expected_output is BrowserExpectedOutput.SCREENSHOT and artifact_mode is not BrowserArtifactMode.SCREENSHOT
            or expected_output is BrowserExpectedOutput.PDF and artifact_mode is not BrowserArtifactMode.PDF
        ):
            raise BrowserAgentError("BROWSER_ARTIFACT_MODE_INVALID", "期望输出与产物模式不一致。")
        capabilities = {BrowserRequiredCapability.PLAYWRIGHT}
        if artifact_mode is BrowserArtifactMode.SCREENSHOT:
            capabilities.add(BrowserRequiredCapability.VIEWPORT_SCREENSHOT)
        if artifact_mode is BrowserArtifactMode.PDF:
            capabilities.add(BrowserRequiredCapability.PDF)
        capabilities.update(BrowserRequiredCapability(item) for item in required_capabilities)
        return BrowserTaskContract(
            goal.strip(), start_url.strip(), expected_output, interaction_mode, artifact_mode,
            original_requests, tuple(sorted(capabilities)), required_evidence,
        )

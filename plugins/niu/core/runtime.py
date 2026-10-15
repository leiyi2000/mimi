from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from core.skills import SkillRegistry
from core.task import TaskFrame
from core.tools import (
    Artifact,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
)


TurnAction = Literal["finish", "continue", "synthesize"]


@dataclass(slots=True)
class RunState:
    messages: list[dict[str, Any]]
    active_skills: set[str]
    task: TaskFrame = field(default_factory=TaskFrame)
    artifacts: list[Artifact] = field(default_factory=list)
    error_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    read_only_only: bool = False
    plan_published: bool = False
    mention_sender: bool = False
    suppress_final_text: bool = False


@dataclass(frozen=True, slots=True)
class ToolBatch:
    activations: tuple[Any, ...]
    plans: tuple[Any, ...]
    regular: tuple[Any, ...]
    completions: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class TurnDecision:
    action: TurnAction
    reason: str = ""
    directive: str = ""


class RunController:
    def __init__(self, skills: SkillRegistry, tools: ToolRegistry) -> None:
        self.skills = skills
        self.tools = tools

    def start(
        self,
        messages: list[dict[str, Any]],
        user_text: str,
    ) -> tuple[RunState, set[str]]:
        preactivated = self.skills.match_explicit(user_text)
        active = self.skills.default_names() | preactivated
        return RunState(list(messages), active), preactivated

    def prepare_request(self, state: RunState) -> dict[str, Any]:
        return {
            "messages": state.messages,
            "read_only_only": state.read_only_only,
            "active_skill_names": state.active_skills,
            "task": state.task,
        }

    def group_calls(self, tool_uses: list[Any]) -> ToolBatch:
        groups: dict[str, list[Any]] = {
            "activate_skills": [],
            "publish_plan": [],
            "regular": [],
            "complete_task": [],
        }
        for block in tool_uses:
            tool = self.tools.get(str(block.name))
            control = tool.control if tool is not None else ""
            groups[control or "regular"].append(block)
        return ToolBatch(
            tuple(groups["activate_skills"]),
            tuple(groups["publish_plan"]),
            tuple(groups["regular"]),
            tuple(groups["complete_task"]),
        )

    def validate_regular_call(
        self,
        state: RunState,
        block: Any,
    ) -> ToolResult | None:
        tool = self.tools.get(str(block.name))
        if (
            tool is not None
            and tool.skill_name
            and tool.skill_name not in state.active_skills
        ):
            return ToolResult.error(
                "SKILL_NOT_ACTIVE",
                "请先激活该工具所属的 Skill。",
                retryable=True,
                safe_details={"skill": tool.skill_name},
            )
        if tool is not None and tool.completion_required:
            state.task.require(tool.name)
        return None

    def execution_mode(self, block: Any) -> str:
        tool = self.tools.get(str(block.name))
        return tool.execution_mode if tool is not None else "sequential"

    def observe_result(
        self,
        state: RunState,
        block: Any,
        result: ToolResult,
    ) -> ToolResult:
        name = str(block.name)
        tool = self.tools.get(name)
        if tool is not None and tool.completion_required:
            state.task.record(name, result)
        if result.is_error:
            signature = (name, result.error_code or result.content)
            state.error_counts[signature] = state.error_counts.get(signature, 0) + 1
            if state.error_counts[signature] > 1:
                result = ToolResult.error(
                    "REPEATED_TOOL_ERROR",
                    "相同工具错误已经重复，请改用其他方法或尽力回答。",
                    retryable=False,
                    safe_details={"tool": name},
                )
        state.artifacts.extend(result.artifacts)
        if result.mention_sender is not None:
            state.mention_sender = result.mention_sender
        state.suppress_final_text = (
            state.suppress_final_text or result.suppress_final_text
        )
        if tool is not None and tool.restricts_followup_to_read_only:
            state.read_only_only = True
        return result

    def finish_turn(
        self,
        state: RunState,
        *,
        stop_reason: str | None,
    ) -> TurnDecision:
        if stop_reason == "max_tokens":
            return TurnDecision("synthesize", reason="上一轮回答达到输出上限。")
        if state.task.active and not state.task.finished:
            return TurnDecision(
                "continue",
                directive=(
                    "任务完成门尚未通过。请根据真实工具结果调用 "
                    "complete_task，不要直接给出最终答复。"
                ),
            )
        return TurnDecision("finish")

    @staticmethod
    def prepare_next_turn(
        state: RunState,
        *,
        assistant_content: Any,
        continuation: str,
    ) -> None:
        state.messages.extend(
            (
                {"role": "assistant", "content": assistant_content},
                {"role": "user", "content": continuation},
            )
        )


class AgentRuntimeTools:
    def __init__(self, skill_names: tuple[str, ...] = ()) -> None:
        self.skill_names = skill_names

    def register(self, registry: ToolRegistry) -> None:
        skill_items: dict[str, Any] = {"type": "string"}
        if self.skill_names:
            skill_items["enum"] = list(self.skill_names)
        registry.register(
            Tool(
                name="activate_skill",
                description=(
                    "Load the instructions and expose the tools for one to three "
                    "capabilities listed in the skill catalog. Use this when the "
                    "user's goal requires a catalog capability that is not active "
                    "yet; match by intended outcome rather than exact wording. Do "
                    "not use it for skills already active, and do not treat an "
                    "inactive catalog skill as unavailable."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "skills": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 3,
                            "items": skill_items,
                        }
                    },
                    "required": ["skills"],
                },
                handler=self.activate_skill,
                control="activate_skills",
                execution="barrier",
            )
        )
        registry.register(
            Tool(
                name="publish_plan",
                description=(
                    "Publish one short user-visible plan before a genuinely long "
                    "or multi-step task. Do not use for simple questions."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "maxLength": 200},
                        "steps": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 4,
                            "items": {"type": "string", "maxLength": 150},
                        },
                        "deliverable": {"type": "string", "maxLength": 200},
                    },
                    "required": ["goal", "steps", "deliverable"],
                },
                handler=self.publish_plan,
                read_only=False,
                skill_name="agent-runtime",
                control="publish_plan",
                execution="barrier",
            )
        )
        registry.register(
            Tool(
                name="complete_task",
                description=(
                    "Pass the task finish gate before the final answer after a "
                    "tool requiring completion evidence has run. Report partial "
                    "completion honestly when blocked."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "status": {
                            "enum": ["completed", "partially_completed"]
                        },
                        "summary": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                        },
                        "remaining": {
                            "type": "array",
                            "maxItems": 5,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 200,
                            },
                        },
                    },
                    "required": ["status", "summary"],
                },
                handler=self.complete_task,
                read_only=True,
                skill_name="agent-runtime",
                task_only=True,
                control="complete_task",
                execution="barrier",
            )
        )

    async def activate_skill(
        self,
        _context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        raw_skills = arguments["skills"]
        if (
            not isinstance(raw_skills, list)
            or not 1 <= len(raw_skills) <= 3
            or any(
                not isinstance(skill, str) or not skill.strip()
                for skill in raw_skills
            )
        ):
            raise ValueError("invalid skills")
        return ToolResult("SKILLS_ACTIVATED")

    async def publish_plan(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        goal = str(arguments["goal"]).strip()
        deliverable = str(arguments["deliverable"]).strip()
        raw_steps = arguments["steps"]
        if not isinstance(raw_steps, list):
            raise TypeError("steps must be a list")
        steps = [str(step).strip() for step in raw_steps]
        if (
            context.send_group_text is None
            or not 1 <= len(goal) <= 200
            or not 2 <= len(steps) <= 4
            or any(not step or len(step) > 150 for step in steps)
            or not 1 <= len(deliverable) <= 200
        ):
            raise ValueError("invalid plan")
        text = "\n".join(
            (
                f"计划：{goal}",
                *(f"{index}. {step}" for index, step in enumerate(steps, 1)),
                f"产出：{deliverable}",
            )
        )
        await context.send_group_text(text)
        return ToolResult("PLAN_PUBLISHED")

    async def complete_task(
        self,
        _context: ToolContext,
        _arguments: dict[str, Any],
    ) -> ToolResult:
        return ToolResult("TASK_COMPLETION_RECORDED")

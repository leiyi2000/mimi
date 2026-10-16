from core.run import RunEvent, RunTrace
from core.task_state import TaskPatch, TaskState
from core.tools import Artifact


def test_task_state_keeps_user_input_separate_from_evidence():
    task = TaskState.start(
        group_id=1,
        topic_id="topic-1",
        user_id=10,
        user_text="找一张网上的图片，不要自己生成",
    )

    prompt = task.prompt()

    assert "任务目标：找一张网上的图片，不要自己生成" in prompt
    assert "待解析用户输入（不能直接当作事实）" in prompt
    assert "已确认事实" not in prompt
    assert "工具证据：无" in prompt


def test_task_state_reduces_run_trace_and_artifacts():
    task = TaskState.start(
        group_id=1,
        topic_id="topic-1",
        user_id=10,
        user_text="下载并发送图片",
    )
    trace = RunTrace(group_id=1, message_id=2)
    trace.events.extend(
        (
            RunEvent(kind="tool_succeeded", tool_name="search_web"),
            RunEvent(
                kind="tool_failed",
                tool_name="attach_remote_file",
                error_code="REMOTE_FILE_DOWNLOAD_FAILED",
            ),
        )
    )
    trace.finish("partially_completed")

    task.record_run(
        response_text="找到候选页面，但还没有发送图片",
        response_status="partially_completed",
        trace=trace,
        artifact_kinds=(),
    )

    prompt = task.prompt()
    assert "当前阶段：partially_completed" in prompt
    assert "search_web: 成功" in prompt
    assert "attach_remote_file: 失败" in prompt
    assert "REMOTE_FILE_DOWNLOAD_FAILED" in prompt
    assert "根据未完成项继续执行" in prompt


def test_task_state_reopens_after_new_user_input():
    task = TaskState.start(
        group_id=1,
        topic_id="topic-1",
        user_id=10,
        user_text="执行任务",
    )
    task.record_run(
        response_text="部分完成",
        response_status="partially_completed",
        trace=None,
        artifact_kinds=("file",),
    )

    task.record_user_input(11, "继续处理剩余部分")

    assert task.status == "active"
    assert task.last_result == "部分完成"
    assert "用户 11：继续处理剩余部分" in task.prompt()


def test_task_state_does_not_treat_run_text_as_task_completion():
    task = TaskState.start(
        group_id=1,
        topic_id="topic-1",
        user_id=10,
        user_text="生成图片",
    )

    task.record_run(
        response_text="图片已经生成",
        response_status="completed",
        trace=None,
        artifact_kinds=("image",),
    )

    assert task.status == "active"
    assert task.phase == "understanding"


def test_task_state_patch_and_target_level_completion_gate():
    task = TaskState.start(
        group_id=1,
        topic_id="topic-1",
        user_id=10,
        user_text="找网络图片",
    )
    task.apply_patch(
        TaskPatch.from_mapping(
            {
                "deliverable": "QQ 图片",
                "acceptance_criteria": ["必须是网络来源，不得自行生成"],
            }
        )
    )

    error = task.validate_completion(
        status="completed",
        summary="已完成",
        remaining=[],
        artifacts=[],
        required_tools={"search_web": True},
    )
    assert error is not None
    assert error.error_code == "DELIVERABLE_EVIDENCE_MISSING"

    assert (
        task.validate_completion(
            status="completed",
            summary="已完成",
            remaining=[],
            artifacts=[Artifact("image", b"data", "image/png")],
            required_tools={"search_web": True},
            accepted_criteria=["必须是网络来源，不得自行生成"],
        )
        is None
    )

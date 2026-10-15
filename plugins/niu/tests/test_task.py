from core.task import TaskFrame
from core.tools import ToolResult


def test_new_delivery_reopens_completed_task_and_uses_latest_result():
    task = TaskFrame()
    task.require("attach_remote_file")
    task.record("attach_remote_file", ToolResult("IMAGE_ATTACHED"))

    assert task.complete(
        {"status": "completed", "summary": "图片已发送"}
    ) is None
    assert task.finished

    task.require("attach_remote_file")
    task.record(
        "attach_remote_file",
        ToolResult(
            "download failed",
            is_error=True,
            error_code="REMOTE_FILE_DOWNLOAD_FAILED",
        ),
    )

    assert not task.finished
    error = task.complete(
        {"status": "completed", "summary": "图片已发送"}
    )
    assert error is not None
    assert error.error_code == "COMPLETION_EVIDENCE_MISSING"

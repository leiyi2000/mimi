import base64
import json

import httpx

from core.tools import ToolContext
from providers.sandbox import SandboxProvider, SandboxTools, WorkspaceTools


def provider(response):
    async def handler(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        return response

    return SandboxProvider(
        base_url="http://sandbox",
        token="test-token",
        request_timeout=5,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ),
    )


async def execute(sandbox, **arguments):
    return await sandbox.execute(
        ToolContext(1, 2, 3, "tester"),
        arguments,
    )


async def test_sandbox_returns_output_and_artifacts():
    response = httpx.Response(
        200,
        json={
            "exit_code": 0,
            "stdout": "done\n",
            "stderr": "",
            "timed_out": False,
            "truncated": False,
            "files": [
                {
                    "path": "result.txt",
                    "mime_type": "text/plain",
                    "data": base64.b64encode(b"result").decode(),
                    "size": 6,
                }
            ],
        },
    )
    sandbox = SandboxTools(provider(response), max_timeout=30)

    result = await execute(
        sandbox,
        script="printf done",
        files={"input.txt": "input"},
        timeout=10,
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["exit_code"] == 0
    assert payload["stdout"] == "done\n"
    assert payload["files"][0]["path"] == "result.txt"
    assert result.artifacts[0].kind == "file"
    assert result.artifacts[0].data == b"result"


async def test_sandbox_marks_nonzero_exit_as_error():
    response = httpx.Response(
        200,
        json={
            "exit_code": 2,
            "stdout": "",
            "stderr": "failed",
            "timed_out": False,
            "truncated": False,
            "files": [],
        },
    )

    result = await execute(
        SandboxTools(provider(response), max_timeout=30),
        script="exit 2",
    )

    assert result.is_error
    assert result.error_code == "CODE_EXIT_NONZERO"


async def test_sandbox_surfaces_runner_rejection():
    response = httpx.Response(
        429,
        json={
            "error": {
                "code": "SANDBOX_BUSY",
                "message": "sandbox concurrency limit reached",
            }
        },
    )

    result = await execute(
        SandboxTools(provider(response), max_timeout=30),
        script="true",
    )

    assert result.is_error
    assert result.error_code == "SANDBOX_BUSY"
    assert json.loads(result.content)["retryable"] is True


async def test_sandbox_rejects_invalid_artifact_path():
    response = httpx.Response(
        200,
        json={
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "truncated": False,
            "files": [
                {
                    "path": "../secret",
                    "mime_type": "text/plain",
                    "data": base64.b64encode(b"x").decode(),
                    "size": 1,
                }
            ],
        },
    )

    result = await execute(
        SandboxTools(provider(response), max_timeout=30),
        script="true",
    )

    assert result.is_error
    assert result.error_code == "SANDBOX_INVALID_RESPONSE"


async def test_workspace_tools_share_run_and_cleanup():
    run_id = "a" * 32
    requests = []

    async def handler(request):
        requests.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path.endswith("/write"):
            assert json.loads(request.content) == {
                "path": "note.txt",
                "content": "hello",
            }
            return httpx.Response(
                200, json={"path": "note.txt", "size": 5}
            )
        if request.url.path.endswith("/read"):
            return httpx.Response(
                200,
                json={
                    "path": "note.txt",
                    "content": "hello",
                    "size": 5,
                    "truncated": False,
                },
            )
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        raise AssertionError(request.url.path)

    sandbox_provider = SandboxProvider(
        base_url="http://sandbox",
        token="test-token",
        request_timeout=5,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    tools = WorkspaceTools(sandbox_provider, max_timeout=30)
    context = ToolContext(1, 2, 3, "tester", run_id=run_id)

    written = await tools.write_workspace_file(
        context,
        {"path": "note.txt", "content": "hello"},
    )
    read = await tools.read_workspace_file(
        context,
        {"path": "note.txt"},
    )
    await tools.cleanup(context)

    assert not written.is_error
    assert json.loads(read.content)["content"] == "hello"
    assert requests == [
        ("POST", f"/v1/workspaces/{run_id}/write"),
        ("POST", f"/v1/workspaces/{run_id}/read"),
        ("DELETE", f"/v1/workspaces/{run_id}"),
    ]


async def test_workspace_exports_artifacts():
    run_id = "b" * 32

    async def handler(request):
        assert request.url.path == f"/v1/workspaces/{run_id}/export"
        return httpx.Response(
            200,
            json={
                "files": [
                    {
                        "path": "result.txt",
                        "mime_type": "text/plain",
                        "data": base64.b64encode(b"done").decode(),
                        "size": 4,
                    }
                ]
            },
        )

    sandbox_provider = SandboxProvider(
        base_url="http://sandbox",
        token="test-token",
        request_timeout=5,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    tools = WorkspaceTools(sandbox_provider, max_timeout=30)

    result = await tools.export_workspace(
        ToolContext(1, 2, 3, "tester", run_id=run_id),
        {"paths": ["result.txt"]},
    )

    assert not result.is_error
    assert result.artifacts[0].data == b"done"
    assert result.artifacts[0].title == "result.txt"

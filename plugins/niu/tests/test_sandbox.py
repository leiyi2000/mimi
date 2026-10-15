import base64
import io
import json

import httpx
import pytest
from PIL import Image

from core.tools import ToolContext, ToolRegistry
from providers.sandbox import (
    RemoteFileTools,
    SandboxFile,
    SandboxProvider,
    SandboxResult,
    SandboxTools,
    WorkspaceTools,
)


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


@pytest.mark.parametrize("delivery", ["code", "workspace", "remote", "file"])
@pytest.mark.parametrize("format", ["PNG", "JPEG", "GIF", "WEBP"])
async def test_image_bytes_are_delivered_as_images_with_generic_metadata(
    delivery, format
):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(buffer, format=format)
    data = buffer.getvalue()
    image_file = SandboxFile("download", "application/octet-stream", data)

    class FakeProvider:
        async def execute(self, **kwargs):
            return SandboxResult(0, "", "", False, False, (image_file,))

        async def export_workspace(self, **kwargs):
            return (image_file,)

    context = ToolContext(1, 2, 3, "tester", run_id="c" * 32)
    if delivery == "code":
        result = await SandboxTools(FakeProvider(), max_timeout=30).execute(
            context, {"script": "download"}
        )
    elif delivery == "workspace":
        result = await WorkspaceTools(
            FakeProvider(), max_timeout=30
        ).export_workspace(context, {"paths": ["download"]})
    else:
        result = await RemoteFileTools(
            FakeProvider(), max_timeout=30
        ).attach_remote_file(
            context,
            {
                "url": "https://cdn.example.com/download?id=1",
                "send_as": "file" if delivery == "file" else "auto",
            },
        )

    assert not result.is_error
    assert result.artifacts[0].kind == (
        "file" if delivery == "file" else "image"
    )
    assert result.artifacts[0].mime_type == Image.MIME[format]
    assert result.artifacts[0].data == data
    assert result.suppress_final_text == (delivery != "file")


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


async def test_remote_file_tool_attaches_arbitrary_binary_file():
    class FakeProvider:
        def __init__(self):
            self.calls = []

        async def execute(self, **kwargs):
            self.calls.append(kwargs)
            return SandboxResult(
                exit_code=0,
                stdout="",
                stderr="",
                timed_out=False,
                truncated=False,
                files=(
                    SandboxFile(
                        "download",
                        "application/octet-stream",
                        b"PK\x03\x04archive",
                    ),
                ),
            )

    fake_provider = FakeProvider()
    tools = RemoteFileTools(fake_provider, max_timeout=30)

    result = await tools.attach_remote_file(
        ToolContext(1, 2, 3, "tester"),
        {
            "url": "https://downloads.example.com/build?id=1",
            "filename": "release.zip",
        },
    )

    assert result.content == "FILE_ATTACHED"
    assert result.artifacts[0].kind == "file"
    assert result.artifacts[0].title == "release.zip"
    assert result.artifacts[0].mime_type == "application/zip"
    assert result.artifacts[0].data == b"PK\x03\x04archive"
    assert fake_provider.calls[0]["files"] == {}
    assert fake_provider.calls[0]["timeout"] == 30
    assert "--proto '=https'" in fake_provider.calls[0]["script"]


async def test_remote_file_tool_sends_images_directly_or_as_files():
    class FakeProvider:
        async def execute(self, **kwargs):
            return SandboxResult(
                exit_code=0,
                stdout="",
                stderr="",
                timed_out=False,
                truncated=False,
                files=(
                    SandboxFile(
                        "download",
                        "application/octet-stream",
                        b"original-webp-bytes",
                    ),
                ),
            )

    tools = RemoteFileTools(FakeProvider(), max_timeout=30)
    context = ToolContext(1, 2, 3, "tester")

    image_result = await tools.attach_remote_file(
        context,
        {
            "url": "https://cdn.example.com/render.webp",
            "filename": "render.webp",
        },
    )
    file_result = await tools.attach_remote_file(
        context,
        {
            "url": "https://cdn.example.com/render.webp",
            "filename": "render.webp",
            "send_as": "file",
        },
    )

    assert image_result.content == "IMAGE_ATTACHED"
    assert image_result.artifacts[0].kind == "image"
    assert image_result.artifacts[0].mime_type == "image/webp"
    assert image_result.artifacts[0].data == b"original-webp-bytes"
    assert file_result.content == "FILE_ATTACHED"
    assert file_result.artifacts[0].kind == "file"
    assert file_result.artifacts[0].data == b"original-webp-bytes"


async def test_remote_file_tool_rejects_non_https_url():
    class FakeProvider:
        async def execute(self, **kwargs):
            raise AssertionError("invalid URL must not reach sandbox")

    registry = ToolRegistry()
    RemoteFileTools(FakeProvider(), max_timeout=30).register(registry)

    result = await registry.execute(
        tool_use_id="file-1",
        name="attach_remote_file",
        arguments={
            "url": "http://127.0.0.1/private.zip",
            "filename": "private.zip",
        },
        context=ToolContext(1, 2, 3, "tester"),
    )

    assert result.is_error
    assert result.error_code == "INVALID_TOOL_INPUT"

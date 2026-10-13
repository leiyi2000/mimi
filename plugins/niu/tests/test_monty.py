import json

import pytest

from core.config import Config
from core.monty import MontyRunner
from core.tools import Tool, ToolContext, ToolRegistry, ToolResult


@pytest.fixture
async def runner(configured_env, monkeypatch):
    monkeypatch.setenv("NIU_MONTY_FEED_TIMEOUT", "0.2")
    monkeypatch.setenv("NIU_MONTY_REQUEST_TIMEOUT", "1")
    monkeypatch.setenv("NIU_MONTY_MEMORY_MB", "8")
    monkeypatch.setenv("NIU_MONTY_OUTPUT_KB", "1")
    registry = ToolRegistry()

    async def echo(context, arguments):
        return ToolResult(json.dumps(arguments))

    registry.register(
        Tool("echo", "echo", {"type": "object"}, echo, read_only=True)
    )
    monty = MontyRunner(Config.from_env(), registry)
    monty.register()
    try:
        yield monty
    finally:
        await monty.close()


async def execute(runner, code, **arguments):
    result = await runner.execute(
        ToolContext(1, 2, 3, "tester"),
        {"code": code, **arguments},
    )
    return result, json.loads(result.content)


async def test_monty_executes_pure_calculation(runner):
    result, payload = await execute(
        runner,
        "values = [x * 2 for x in numbers]\nvalues",
        inputs={"numbers": [1, 2, 3]},
    )

    assert not result.is_error
    assert payload["value"] == [2, 4, 6]


async def test_monty_can_call_explicit_read_only_tool(runner):
    result, payload = await execute(
        runner,
        'await call_tool("echo", {"value": 7})',
        allowed_tools=["echo"],
    )

    assert not result.is_error
    assert payload["value"]["is_error"] is False


@pytest.mark.parametrize(
    "code",
    [
        "open('/etc/passwd').read()",
        "import subprocess\nsubprocess.run(['id'])",
        "import os\nos.environ",
    ],
)
async def test_monty_cannot_access_host_resources(runner, code):
    result, payload = await execute(runner, code)

    assert result.is_error
    assert payload["value"] is None


async def test_monty_enforces_timeout(runner):
    result, payload = await execute(runner, "while True:\n    pass")

    assert result.is_error
    assert payload["error_type"] == "CODE_TIMEOUT"
    assert payload["timed_out"]


async def test_monty_truncates_printed_output(runner):
    result, payload = await execute(runner, "print('x' * 5000)\n'ok'")

    assert not result.is_error
    assert payload["value"] == "ok"
    assert payload["truncated"]
    assert len(payload["stdout"].encode()) <= 1024

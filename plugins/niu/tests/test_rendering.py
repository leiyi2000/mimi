from io import BytesIO
from pathlib import Path

from PIL import Image

from core.rendering import RenderTools, RichReplyRenderer
from core.tools import ToolContext, ToolRegistry


OUTPUT = Path(__file__).parent / "output" / "rich-reply.png"

FIXTURE = {
    "title": "MiniMax 模型能力对比",
    "subtitle": "按稳定性、响应速度和适用任务整理",
    "sections": [
        {
            "heading": "结论",
            "body": "生产环境优先选择稳定模型；预览模型用于验证新能力。",
            "bullets": [
                "复杂任务先确认输入和输出边界",
                "实时事实需要联网查询并保留来源",
            ],
        },
        {
            "heading": "选择建议",
            "table": {
                "headers": ["场景", "建议", "原因"],
                "rows": [
                    ["群聊问答", "稳定模型", "可预测性更高"],
                    ["功能试验", "预览模型", "优先体验新能力"],
                ],
            },
        },
    ],
    "footer": "由 Mimi 整理",
}


def test_renderer_escapes_model_content():
    renderer = RichReplyRenderer()
    reply = renderer.normalize(
        {
            "title": "安全 <检查>",
            "sections": [
                {
                    "body": "<script>alert('x')</script>",
                    "bullets": ["A & B"],
                }
            ],
        }
    )

    html = renderer.build_html(reply)

    assert "安全 &lt;检查&gt;" in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "A &amp; B" in html


async def test_render_tool_returns_real_png():
    registry = ToolRegistry()
    RenderTools().register(registry)

    result = await registry.execute(
        tool_use_id="render-1",
        name="render_rich_reply",
        arguments=FIXTURE,
        context=ToolContext(1, 2, 3, "tester"),
    )

    assert result.content == "RICH_REPLY_RENDERED"
    assert not result.is_error
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.kind == "image"
    assert artifact.mime_type == "image/png"
    assert artifact.data.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(BytesIO(artifact.data)) as image:
        assert image.width == 1400
        assert image.height > 500
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(artifact.data)


async def test_render_tool_rejects_mismatched_table_rows():
    registry = ToolRegistry()
    RenderTools().register(registry)

    result = await registry.execute(
        tool_use_id="render-invalid",
        name="render_rich_reply",
        arguments={
            "title": "错误表格",
            "sections": [
                {
                    "table": {
                        "headers": ["A", "B"],
                        "rows": [["只有一列"]],
                    }
                }
            ],
        },
        context=ToolContext(1, 2, 3, "tester"),
    )

    assert result.error_code == "INVALID_TOOL_INPUT"
    assert '"retryable":true' in result.content
    assert result.is_error

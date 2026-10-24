from __future__ import annotations

import base64
from pathlib import Path

import pytest

from providers.markdown import MarkdownRenderError, MarkdownRenderer


OUTPUT_DIR = Path(__file__).parent / "output"
SOURCE_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_markdown_renderer_returns_rich_png():
    image = MarkdownRenderer().render(
        """
# 三日旅行攻略

> 周末轻量路线，适合第一次到访。

1. 上午：博物馆
2. 下午：老城区

| 日期 | 重点 | 预算 |
| --- | --- | ---: |
| D1 | 城市漫步 | ¥200 |
| D2 | 自然风景 | ¥350 |

```text
集合时间：09:30
```

[资料来源](https://example.com/guide)
"""
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "markdown-render.png").write_bytes(image)


@pytest.mark.parametrize(
    "markdown",
    [
        "![外链](https://example.com/image.png)",
        "![本地](file:///tmp/image.png)",
        "![相对路径](./image.png)",
        "![数据](data:image/png;base64,AAAA)",
    ],
)
def test_markdown_renderer_rejects_non_artifact_images(markdown):
    with pytest.raises(MarkdownRenderError, match="Artifact"):
        MarkdownRenderer().render(markdown)


def test_markdown_renderer_allows_task_artifact_references():
    assert MarkdownRenderer.artifact_ids(
        "![路线图](artifact://artifact-12345678)"
    ) == ("artifact-12345678",)


def test_markdown_renderer_allows_image_syntax_inside_code_block():
    image = MarkdownRenderer().render(
        "```markdown\n![示例](https://example.com/image.png)\n```"
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")


def test_markdown_renderer_resolves_task_artifact_bytes():
    source = "artifact://artifact-12345678"
    image = MarkdownRenderer().render(
        f"# 路线图\n\n![路线图]({source})",
        images={source: SOURCE_IMAGE},
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")

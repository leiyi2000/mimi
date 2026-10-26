import base64

import pytest

from providers.html import (
    DEFAULT_HTML_RENDER_DEVICE_PIXEL_RATIO,
    HtmlRenderError,
    HtmlRenderer,
)
from providers.fonts import (
    EMOJI_FONT_NAME,
    EMOJI_FONT_PATH,
    FONT_NAME,
    FONT_PATH,
    load_cjk_fonts,
)


def test_html_renderer_returns_png():
    image = HtmlRenderer().render(
        """
<style>
  .card { padding: 16px; color: #fff; background: #111; }
</style>
<div class="card"><h1>任务报告</h1><p>已整理完成</p></div>
""",
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")


def test_html_renderer_default_width_matches_css_viewport():
    assert DEFAULT_HTML_RENDER_DEVICE_PIXEL_RATIO == 1


def test_cjk_font_asset_is_available():
    fonts = load_cjk_fonts()

    assert FONT_PATH.is_file()
    assert fonts
    assert fonts[0]["name"] == FONT_NAME
    assert isinstance(fonts[0]["data"], bytes)
    assert any(item["name"] == EMOJI_FONT_NAME for item in fonts)
    assert EMOJI_FONT_PATH.stat().st_size > 100_000


@pytest.mark.parametrize(
    "html",
    [
        "<script>alert(1)</script><div>unsafe</div>",
        '<img src="https://example.com/image.png">',
        '<div onclick="alert(1)">unsafe</div>',
        '<iframe src="artifact://artifact-1"></iframe>',
    ],
)
def test_html_renderer_rejects_active_or_external_content(html):
    with pytest.raises(HtmlRenderError):
        HtmlRenderer().render(html)


def test_html_renderer_allows_task_artifact_references():
    assert HtmlRenderer.artifact_ids(
        '<img src="artifact://artifact-12345678" alt="result">'
    ) == ("artifact-12345678",)


def test_html_renderer_resolves_task_artifact_bytes():
    source = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    image = HtmlRenderer().render(
        '<div><img src="artifact://artifact-12345678"></div>',
        images={"artifact://artifact-12345678": source},
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")

from __future__ import annotations

from html import escape

from pytakumi import html_to_pic

from providers.fonts import FONT_FAMILY_NAMES, FONT_NAME, load_cjk_fonts


LONG_REPLY_TEXT_LIMIT = 80
RENDER_WIDTH = 1000
DEVICE_PIXEL_RATIO = 2


class TextImageRenderer:
    """Render long user-facing text into a readable PNG."""

    def render(self, text: str, *, label: str = "回复内容") -> bytes:
        normalized = text.strip()
        if not normalized:
            raise ValueError("text image content is empty")
        html = f"""
<style>
  * {{ box-sizing: border-box; }}
  .page {{
    width: 500px;
    padding: 28px 32px 30px;
    background: #101214;
    color: #f2f4f5;
    font-family: "{FONT_NAME}", "PumpkinEmoji", "Noto Sans CJK SC",
      "PingFang SC", sans-serif;
  }}
  .label {{
    margin-bottom: 18px;
    color: #45c2c9;
    font-size: 15px;
    font-weight: 700;
  }}
  .content {{
    white-space: pre-wrap;
    overflow-wrap: break-word;
    word-break: normal;
    text-align: left;
    letter-spacing: 0;
    font-size: 21px;
    line-height: 1.65;
  }}
</style>
<div class="page">
  <div class="label">{escape(label)}</div>
  <div class="content">{escape(normalized)}</div>
</div>
"""
        return html_to_pic(
            html,
            width=RENDER_WIDTH,
            device_pixel_ratio=DEVICE_PIXEL_RATIO,
            fonts=load_cjk_fonts() or None,
            font_families=FONT_FAMILY_NAMES,
        )

from __future__ import annotations

import re
from collections.abc import Mapping

from pytakumi import html_to_pic

from providers.fonts import FONT_FAMILY_NAMES, load_cjk_fonts


DEFAULT_HTML_RENDER_WIDTH = 1600
DEFAULT_HTML_RENDER_DEVICE_PIXEL_RATIO = 2
MAX_HTML_CHARS = 120_000
MAX_HTML_IMAGES = 8

_BANNED_TAG = re.compile(
    r"<\s*(?:script|iframe|object|embed|link|meta)\b",
    re.IGNORECASE,
)
_BANNED_ATTRIBUTE = re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE)
_BANNED_URL = re.compile(
    r"(?:javascript:|file:|https?://|(?<!artifact:)//)",
    re.IGNORECASE,
)
_ARTIFACT_REFERENCE = re.compile(r"artifact://([A-Za-z0-9_-]{8,128})")


class HtmlRenderError(ValueError):
    """The submitted HTML cannot be rendered safely."""


class HtmlRenderer:
    def render(
        self,
        html: str,
        *,
        images: Mapping[str, bytes] | None = None,
        width: int = DEFAULT_HTML_RENDER_WIDTH,
        device_pixel_ratio: int = DEFAULT_HTML_RENDER_DEVICE_PIXEL_RATIO,
    ) -> bytes:
        self.validate(html, width=width, device_pixel_ratio=device_pixel_ratio)
        return html_to_pic(
            html,
            width=width,
            device_pixel_ratio=device_pixel_ratio,
            images=images,
            fonts=load_cjk_fonts() or None,
            font_families=FONT_FAMILY_NAMES,
            overflow="visible",
        )

    @staticmethod
    def validate(
        html: str,
        *,
        width: int,
        device_pixel_ratio: int,
    ) -> None:
        if not isinstance(html, str) or not html.strip():
            raise HtmlRenderError("HTML 内容不能为空")
        if len(html) > MAX_HTML_CHARS:
            raise HtmlRenderError("HTML 内容超过长度限制")
        if _BANNED_TAG.search(html):
            raise HtmlRenderError("HTML 不允许包含脚本或外部嵌入标签")
        if _BANNED_ATTRIBUTE.search(html):
            raise HtmlRenderError("HTML 不允许包含事件处理属性")
        if _BANNED_URL.search(_resource_attributes(html)):
            raise HtmlRenderError("HTML 不允许引用外部或本地资源")
        if not 600 <= width <= 2400:
            raise HtmlRenderError("渲染宽度必须在 600 到 2400 之间")
        if device_pixel_ratio not in {1, 2}:
            raise HtmlRenderError("设备像素比只能是 1 或 2")

    @staticmethod
    def artifact_ids(html: str) -> tuple[str, ...]:
        values = tuple(dict.fromkeys(_ARTIFACT_REFERENCE.findall(html)))
        if len(values) > MAX_HTML_IMAGES:
            raise HtmlRenderError("HTML 引用的图片 Artifact 过多")
        return values


def _resource_attributes(html: str) -> str:
    return " ".join(
        match.group(0)
        for match in re.finditer(
            r"(?:src|href|action|poster|data)\s*=\s*([\"'])(.*?)\1"
            r"|url\s*\(\s*([\"']?)(.*?)\3\s*\)",
            html,
            re.IGNORECASE | re.DOTALL,
        )
    )

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum

from markdown_it import MarkdownIt
from markdown_it.token import Token
from pytakumi import md_to_pic

from providers.fonts import FONT_FAMILY_NAMES, load_cjk_fonts


DEFAULT_MARKDOWN_RENDER_WIDTH = 1600
DEFAULT_MARKDOWN_RENDER_DEVICE_PIXEL_RATIO = 2
MAX_MARKDOWN_CHARS = 120_000
MAX_MARKDOWN_IMAGES = 8
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_FILE_IMAGE = re.compile(
    r"!\[[^\]]*\]\(\s*<?(file:[^\s)>]+)",
    re.IGNORECASE,
)


class MarkdownTheme(StrEnum):
    LIGHT = "light"
    DARK = "dark"


class MarkdownRenderError(ValueError):
    """The submitted Markdown cannot be rendered safely."""


class MarkdownRenderer:
    def render(
        self,
        markdown: str,
        *,
        images: Mapping[str, bytes] | None = None,
        width: int = DEFAULT_MARKDOWN_RENDER_WIDTH,
        device_pixel_ratio: int = DEFAULT_MARKDOWN_RENDER_DEVICE_PIXEL_RATIO,
        theme: MarkdownTheme = MarkdownTheme.DARK,
    ) -> bytes:
        self.validate(
            markdown,
            width=width,
            device_pixel_ratio=device_pixel_ratio,
        )
        return md_to_pic(
            markdown,
            width=width,
            dark=theme is MarkdownTheme.DARK,
            images=images,
            fonts=load_cjk_fonts() or None,
            font_families=FONT_FAMILY_NAMES,
            lang="zh-CN",
            device_pixel_ratio=device_pixel_ratio,
            overflow="visible",
        )

    @classmethod
    def validate(
        cls,
        markdown: str,
        *,
        width: int,
        device_pixel_ratio: int,
    ) -> None:
        if not isinstance(markdown, str) or not markdown.strip():
            raise MarkdownRenderError("Markdown 内容不能为空")
        if len(markdown) > MAX_MARKDOWN_CHARS:
            raise MarkdownRenderError("Markdown 内容超过长度限制")
        cls.artifact_ids(markdown)
        if not 600 <= width <= 2400:
            raise MarkdownRenderError("渲染宽度必须在 600 到 2400 之间")
        if device_pixel_ratio not in {1, 2}:
            raise MarkdownRenderError("设备像素比只能是 1 或 2")

    @staticmethod
    def artifact_ids(markdown: str) -> tuple[str, ...]:
        sources = tuple(dict.fromkeys(_image_sources(markdown)))
        invalid = tuple(
            source
            for source in sources
            if not source.startswith("artifact://")
        )
        if invalid:
            raise MarkdownRenderError(
                "Markdown 图片只能引用当前任务的 Artifact"
            )
        artifact_ids = tuple(
            source.removeprefix("artifact://") for source in sources
        )
        if any(not _ARTIFACT_ID.fullmatch(item) for item in artifact_ids):
            raise MarkdownRenderError("Markdown 图片 Artifact ID 无效")
        if len(artifact_ids) > MAX_MARKDOWN_IMAGES:
            raise MarkdownRenderError("Markdown 引用的图片 Artifact 过多")
        return artifact_ids


def _image_sources(markdown: str) -> tuple[str, ...]:
    parser = MarkdownIt(
        "commonmark",
        {"html": False, "linkify": False},
    )
    for plugin in ("strikethrough", "table"):
        parser.enable(plugin)
    sources: list[str] = []

    def collect(tokens: list[Token] | None) -> None:
        for token in tokens or ():
            if token.type == "image":
                source = token.attrGet("src")
                if source:
                    sources.append(source)
            elif token.type == "text":
                sources.extend(
                    match.group(1)
                    for match in _FILE_IMAGE.finditer(token.content)
                )
            collect(token.children)

    collect(parser.parse(markdown))
    return tuple(dict.fromkeys(sources))

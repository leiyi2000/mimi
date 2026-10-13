from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

from pytakumi import html_to_pic

from .tools import Artifact, Tool, ToolContext, ToolRegistry, ToolResult


RENDER_WIDTH = 1400
DEVICE_PIXEL_RATIO = 2
PAGE_WIDTH = RENDER_WIDTH // DEVICE_PIXEL_RATIO
FONT_NAME = "NiuCJK"
MAX_CONTENT_CHARS = 12_000
MAX_RENDER_HEIGHT = 16_000
MAX_RENDER_BYTES = 12 * 1024 * 1024
FONT_PATH = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "fonts"
    / "NotoSansSC.ttf"
)


@dataclass(frozen=True, slots=True)
class ReplyTable:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class ReplySection:
    heading: str
    body: str
    bullets: tuple[str, ...]
    table: ReplyTable | None


@dataclass(frozen=True, slots=True)
class RichReply:
    title: str
    subtitle: str
    sections: tuple[ReplySection, ...]
    footer: str


class RichReplyRenderer:
    def normalize(self, arguments: dict[str, Any]) -> RichReply:
        title = self._text(arguments["title"], 100, required=True)
        subtitle = self._text(arguments.get("subtitle", ""), 240)
        footer = self._text(arguments.get("footer", ""), 120)
        raw_sections = arguments["sections"]
        if not isinstance(raw_sections, list) or not 1 <= len(raw_sections) <= 12:
            raise ValueError("invalid sections")
        sections = tuple(self._section(item) for item in raw_sections)
        content_size = sum(
            len(value)
            for section in sections
            for value in (
                section.heading,
                section.body,
                *section.bullets,
                *(
                    cell
                    for row in (
                        (section.table.headers, *section.table.rows)
                        if section.table is not None
                        else ()
                    )
                    for cell in row
                ),
            )
        )
        if len(title) + len(subtitle) + len(footer) + content_size > MAX_CONTENT_CHARS:
            raise ValueError("reply content is too large")
        return RichReply(title, subtitle, sections, footer)

    def render(self, reply: RichReply) -> bytes:
        png = html_to_pic(
            self.build_html(reply),
            width=RENDER_WIDTH,
            device_pixel_ratio=DEVICE_PIXEL_RATIO,
            fonts=self._fonts(),
        )
        if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > MAX_RENDER_BYTES:
            raise RuntimeError("invalid render output")
        width, height = struct.unpack(">II", png[16:24])
        if width != RENDER_WIDTH or height > MAX_RENDER_HEIGHT:
            raise RuntimeError("render dimensions exceeded")
        return png

    def build_html(self, reply: RichReply) -> str:
        sections = "".join(self._section_html(section) for section in reply.sections)
        subtitle = (
            f'<div class="subtitle">{self._multiline(reply.subtitle)}</div>'
            if reply.subtitle
            else ""
        )
        footer = (
            f'<div class="footer">{escape(reply.footer)}</div>'
            if reply.footer
            else ""
        )
        return f"""
<style>
  * {{ box-sizing: border-box; }}
  .page {{
    width: {PAGE_WIDTH}px;
    padding: 38px 42px 30px;
    background: #111315;
    color: #f1f3f4;
    font-family: "{FONT_NAME}", sans-serif;
  }}
  .header {{
    padding-bottom: 24px;
    border-bottom: 3px solid #e8ecee;
  }}
  .title {{
    font-size: 34px;
    font-weight: 800;
    line-height: 1.25;
    overflow-wrap: anywhere;
  }}
  .subtitle {{
    margin-top: 10px;
    color: #aeb7bd;
    font-size: 16px;
    line-height: 1.65;
    overflow-wrap: anywhere;
  }}
  .section {{
    padding: 24px 0 22px;
    border-bottom: 1px solid #34393d;
  }}
  .section:last-of-type {{ border-bottom: 0; }}
  .heading {{
    margin-bottom: 12px;
    color: #58c8cf;
    font-size: 21px;
    font-weight: 800;
    overflow-wrap: anywhere;
  }}
  .body {{
    color: #d9dde0;
    font-size: 16px;
    line-height: 1.7;
    overflow-wrap: anywhere;
  }}
  .bullets {{ margin-top: 11px; }}
  .bullet {{
    display: flex;
    align-items: flex-start;
    margin-top: 7px;
    color: #d9dde0;
    font-size: 15px;
    line-height: 1.6;
  }}
  .marker {{
    flex: 0 0 14px;
    color: #ff7a86;
    font-weight: 800;
  }}
  .bullet-text {{ min-width: 0; overflow-wrap: anywhere; }}
  table {{
    width: 100%;
    margin-top: 15px;
    border-collapse: collapse;
    table-layout: fixed;
  }}
  th, td {{
    padding: 10px 11px;
    border: 1px solid #3c4247;
    text-align: left;
    vertical-align: top;
    overflow-wrap: anywhere;
  }}
  th {{
    background: #202529;
    color: #f4f6f7;
    font-size: 14px;
    font-weight: 800;
  }}
  td {{
    color: #cfd5d9;
    font-size: 14px;
    line-height: 1.5;
  }}
  tr:nth-child(even) td {{ background: #171a1d; }}
  .footer {{
    padding-top: 18px;
    color: #7f898f;
    font-size: 12px;
    text-align: right;
    overflow-wrap: anywhere;
  }}
</style>
<div class="page">
  <div class="header">
    <div class="title">{escape(reply.title)}</div>
    {subtitle}
  </div>
  {sections}
  {footer}
</div>
"""

    def _section(self, value: Any) -> ReplySection:
        if not isinstance(value, dict):
            raise TypeError("section must be an object")
        heading = self._text(value.get("heading", ""), 100)
        body = self._text(value.get("body", ""), 3_000)
        raw_bullets = value.get("bullets", [])
        if not isinstance(raw_bullets, list):
            raise TypeError("bullets must be an array")
        if len(raw_bullets) > 20:
            raise ValueError("invalid bullets")
        bullets = tuple(self._text(item, 400, required=True) for item in raw_bullets)
        table = self._table(value.get("table"))
        if not heading and not body and not bullets and table is None:
            raise ValueError("empty section")
        return ReplySection(heading, body, bullets, table)

    def _table(self, value: Any) -> ReplyTable | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise TypeError("table must be an object")
        raw_headers = value.get("headers")
        raw_rows = value.get("rows")
        if (
            not isinstance(raw_headers, list)
            or not 1 <= len(raw_headers) <= 6
            or not isinstance(raw_rows, list)
            or not 1 <= len(raw_rows) <= 30
        ):
            raise ValueError("invalid table")
        headers = tuple(
            self._text(item, 120, required=True) for item in raw_headers
        )
        rows: list[tuple[str, ...]] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, list) or len(raw_row) != len(headers):
                raise ValueError("invalid table row")
            rows.append(
                tuple(self._text(item, 300, required=True) for item in raw_row)
            )
        return ReplyTable(headers, tuple(rows))

    @staticmethod
    def _text(value: Any, maximum: int, *, required: bool = False) -> str:
        if not isinstance(value, str):
            raise TypeError("text must be a string")
        normalized = value.strip()
        if len(normalized) > maximum or (required and not normalized):
            raise ValueError("invalid text length")
        return normalized

    @staticmethod
    def _multiline(value: str) -> str:
        return "<br>".join(escape(line) for line in value.splitlines())

    def _section_html(self, section: ReplySection) -> str:
        heading = (
            f'<div class="heading">{escape(section.heading)}</div>'
            if section.heading
            else ""
        )
        body = (
            f'<div class="body">{self._multiline(section.body)}</div>'
            if section.body
            else ""
        )
        bullets = ""
        if section.bullets:
            items = "".join(
                '<div class="bullet"><span class="marker">•</span>'
                f'<span class="bullet-text">{self._multiline(item)}</span></div>'
                for item in section.bullets
            )
            bullets = f'<div class="bullets">{items}</div>'
        table = self._table_html(section.table) if section.table else ""
        return f"""
  <section class="section">
    {heading}
    {body}
    {bullets}
    {table}
  </section>
"""

    @staticmethod
    def _table_html(table: ReplyTable) -> str:
        headers = "".join(f"<th>{escape(item)}</th>" for item in table.headers)
        rows = "".join(
            "<tr>"
            + "".join(
                f"<td>{RichReplyRenderer._multiline(item)}</td>" for item in row
            )
            + "</tr>"
            for row in table.rows
        )
        return f"<table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table>"

    @staticmethod
    @lru_cache(maxsize=1)
    def _fonts() -> list[dict[str, Any]]:
        if not FONT_PATH.is_file():
            raise RuntimeError(f"render font missing: {FONT_PATH}")
        return [{"data": FONT_PATH.read_bytes(), "name": FONT_NAME}]


class RenderTools:
    def __init__(self, renderer: RichReplyRenderer | None = None) -> None:
        self.renderer = renderer or RichReplyRenderer()

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            Tool(
                name="render_rich_reply",
                description=(
                    "Render a long structured answer as a readable image with "
                    "sections, bullets, and optional tables."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "maxLength": 100},
                        "subtitle": {"type": "string", "maxLength": 240},
                        "sections": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 12,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "heading": {
                                        "type": "string",
                                        "maxLength": 100,
                                    },
                                    "body": {
                                        "type": "string",
                                        "maxLength": 3000,
                                    },
                                    "bullets": {
                                        "type": "array",
                                        "maxItems": 20,
                                        "items": {
                                            "type": "string",
                                            "maxLength": 400,
                                        },
                                    },
                                    "table": {
                                        "type": "object",
                                        "properties": {
                                            "headers": {
                                                "type": "array",
                                                "minItems": 1,
                                                "maxItems": 6,
                                                "items": {
                                                    "type": "string",
                                                    "maxLength": 120,
                                                },
                                            },
                                            "rows": {
                                                "type": "array",
                                                "minItems": 1,
                                                "maxItems": 30,
                                                "items": {
                                                    "type": "array",
                                                    "items": {
                                                        "type": "string",
                                                        "maxLength": 300,
                                                    },
                                                },
                                            },
                                        },
                                        "required": ["headers", "rows"],
                                    },
                                },
                            },
                        },
                        "footer": {"type": "string", "maxLength": 120},
                    },
                    "required": ["title", "sections"],
                },
                handler=self.render,
                timeout=30,
                read_only=False,
                skill_name="render",
            )
        )

    async def render(
        self, _context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        reply = self.renderer.normalize(arguments)
        png = await asyncio.to_thread(self.renderer.render, reply)
        return ToolResult(
            "RICH_REPLY_RENDERED",
            artifacts=(
                Artifact(
                    kind="image",
                    data=png,
                    mime_type="image/png",
                    title=reply.title,
                ),
            ),
        )

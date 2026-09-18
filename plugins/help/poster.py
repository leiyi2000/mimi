from html import escape
from pathlib import Path

from pytakumi import html_to_pic

from catalog import Plugin


RENDER_WIDTH = 1280
DEVICE_PIXEL_RATIO = 2
FONT_NAME = "HelpCJK"

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
FONT_CANDIDATES = (
    PLUGIN_ROOT / "lol/features/rendering/fonts/NotoSansSC.ttf",
    PLUGIN_ROOT / "apex/features/rendering/fonts/NotoSansSC-Regular.subset.otf",
)


def _fonts() -> list[dict]:
    for path in FONT_CANDIDATES:
        if path.exists():
            return [{"data": path.read_bytes(), "name": FONT_NAME}]
    return []


def build_html(plugins: tuple[Plugin, ...]) -> str:
    sections = "".join(_plugin_html(plugin) for plugin in plugins)
    command_count = sum(len(plugin.commands) for plugin in plugins)
    return f"""
<style>
  * {{ box-sizing: border-box; }}
  .page {{
    width: 640px;
    padding: 40px 42px 32px;
    background: #151719;
    color: #f2f4f5;
    font-family: "{FONT_NAME}", sans-serif;
  }}
  .header {{
    padding-bottom: 26px;
    border-bottom: 3px solid #f2f4f5;
  }}
  .title {{ font-size: 38px; font-weight: 800; }}
  .meta {{ margin-top: 7px; color: #929ba3; font-size: 15px; }}
  .plugin {{ padding: 27px 0 21px; border-bottom: 1px solid #353a3e; }}
  .plugin:last-of-type {{ border-bottom: 0; }}
  .plugin-head {{ display: flex; align-items: baseline; margin-bottom: 16px; }}
  .plugin-name {{ font-size: 23px; font-weight: 800; }}
  .plugin-description {{ margin-left: 12px; color: #8f989f; font-size: 14px; }}
  .command {{
    display: flex;
    align-items: flex-start;
    padding: 12px 0;
  }}
  .command + .command {{ border-top: 1px solid #292d30; }}
  .usage {{
    width: 290px;
    flex: 0 0 290px;
    color: #45c2c9;
    font-size: 16px;
    font-weight: 700;
    overflow-wrap: anywhere;
  }}
  .details {{ flex: 1 1 0; min-width: 0; }}
  .description {{ color: #c9ced2; font-size: 15px; line-height: 1.45; }}
  .badge {{
    display: inline-block;
    margin-top: 5px;
    padding: 2px 7px;
    border: 1px solid #ff6878;
    color: #ff8290;
    font-size: 11px;
    font-weight: 700;
  }}
  .footer {{
    padding-top: 18px;
    color: #7f898f;
    font-size: 13px;
    text-align: right;
  }}
</style>
<div class="page">
  <div class="header">
    <div class="title">指令帮助</div>
    <div class="meta">{len(plugins)} 个功能 · {command_count} 条指令</div>
  </div>
  {sections}
  <div class="footer">发送“帮助 插件名”可查看单个分类</div>
</div>
"""


def _plugin_html(plugin: Plugin) -> str:
    rows = "".join(
        f"""
    <div class="command">
      <div class="usage">{escape(command.usage)}</div>
      <div class="details">
        <div class="description">{escape(command.description)}</div>
        {f'<span class="badge">{escape(command.badge)}</span>' if command.badge else ''}
      </div>
    </div>
"""
        for command in plugin.commands
    )
    return f"""
  <section class="plugin">
    <div class="plugin-head">
      <span class="plugin-name">{escape(plugin.name)}</span>
      <span class="plugin-description">{escape(plugin.description)}</span>
    </div>
    {rows}
  </section>
"""


def render_help(plugins: tuple[Plugin, ...]) -> bytes:
    return html_to_pic(
        build_html(plugins),
        width=RENDER_WIDTH,
        device_pixel_ratio=DEVICE_PIXEL_RATIO,
        fonts=_fonts() or None,
    )

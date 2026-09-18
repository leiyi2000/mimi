import logging
from pathlib import Path

from pytakumi import html_to_pic


log = logging.getLogger(__name__)

RENDER_WIDTH = 900
DEVICE_PIXEL_RATIO = 2
FONT_NAME = "LolCJK"
PAGE_WIDTH = RENDER_WIDTH // DEVICE_PIXEL_RATIO

FONT_DIR = Path(__file__).resolve().parent / "fonts"


def _load_fonts() -> list[dict]:
    path = FONT_DIR / "NotoSansSC.ttf"
    if not path.exists():
        return []
    return [{"data": path.read_bytes(), "name": FONT_NAME}]


_fonts = _load_fonts()


def render_image(html: str, images: dict[str, bytes], width: int = RENDER_WIDTH) -> bytes:
    return html_to_pic(
        html,
        width=width,
        device_pixel_ratio=DEVICE_PIXEL_RATIO,
        images=images,
        fonts=_fonts or None,
    )

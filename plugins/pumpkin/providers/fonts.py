from __future__ import annotations

from pathlib import Path


FONT_NAME = "PumpkinCJK"
EMOJI_FONT_NAME = "PumpkinEmoji"
FONT_PATH = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "fonts"
    / "NotoSansSC.ttf"
)
EMOJI_FONT_PATH = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "fonts"
    / "NotoEmoji.ttf"
)
FONT_FAMILY_NAMES = [FONT_NAME, EMOJI_FONT_NAME]


def load_cjk_fonts() -> list[dict[str, bytes | str]]:
    fonts: list[dict[str, bytes | str]] = []
    for path, name in (
        (FONT_PATH, FONT_NAME),
        (EMOJI_FONT_PATH, EMOJI_FONT_NAME),
    ):
        if path.is_file():
            fonts.append({"data": path.read_bytes(), "name": name})
    return fonts

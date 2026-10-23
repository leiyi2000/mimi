from providers.fonts import EMOJI_FONT_PATH, FONT_PATH
from providers.text_image import TextImageRenderer


def test_text_image_renderer_returns_png():
    image = TextImageRenderer().render(
        "验收更严，必须通过验收才能交付；失败任务不再发送中间产物。"
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert FONT_PATH.stat().st_size > 10_000_000


def test_text_image_renderer_supports_punctuation_and_emoji():
    image = TextImageRenderer().render(
        "步骤数用超了——这回先收敛 😣 南瓜在呢😌 😂 ❤️ 🌍"
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert EMOJI_FONT_PATH.stat().st_size > 100_000

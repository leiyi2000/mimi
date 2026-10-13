from pathlib import Path

import pytest

from core.prompts import PromptCatalog, PromptError


def test_prompt_catalog_loads_layers_in_order():
    root = Path(__file__).resolve().parent.parent / "prompts"

    catalog = PromptCatalog.load(root)
    prompt = catalog.fixed_prompt()

    assert prompt.index("系统安全规则") < prompt.index("你叫“南瓜”")
    assert prompt.index("你叫“南瓜”") < prompt.index("通过 NapCat")


@pytest.mark.parametrize("content", [None, " \n"])
def test_prompt_catalog_rejects_missing_or_empty_files(tmp_path, content):
    for name in ("system.md", "personality.md", "napcat.md"):
        (tmp_path / name).write_text("valid", encoding="utf-8")
    path = tmp_path / "personality.md"
    if content is None:
        path.unlink()
    else:
        path.write_text(content, encoding="utf-8")

    with pytest.raises(PromptError, match="personality.md"):
        PromptCatalog.load(tmp_path)

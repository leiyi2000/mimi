from pathlib import Path

import pytest

from catalog import CatalogError, CommandCatalog
from main import help_query
from poster import build_html, render_help


PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def test_catalog_loads_all_visible_plugins():
    plugins = CommandCatalog(PLUGIN_ROOT).load()

    assert [plugin.key for plugin in plugins] == [
        "help",
        "lol",
        "apex",
        "music",
        "chat",
    ]
    assert sum(len(plugin.commands) for plugin in plugins) == 15


def test_catalog_filters_by_key_or_display_name():
    catalog = CommandCatalog(PLUGIN_ROOT)

    assert [plugin.key for plugin in catalog.find("LOL")] == ["lol"]
    assert [plugin.key for plugin in catalog.find("英雄联盟")] == ["lol"]


def test_catalog_rejects_duplicate_commands(tmp_path):
    manifest = """
[plugin]
key = "{key}"
name = "{key}"

[[commands]]
name = "重复"
usage = "重复"
description = "test"
"""
    for key in ("one", "two"):
        directory = tmp_path / key
        directory.mkdir()
        (directory / "commands.toml").write_text(
            manifest.format(key=key),
            encoding="utf-8",
        )

    with pytest.raises(CatalogError, match="duplicate command"):
        CommandCatalog(tmp_path).load()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("帮助", ""),
        ("  帮助  ", ""),
        ("帮助 LOL", "LOL"),
        ("帮助　英雄联盟", "英雄联盟"),
        ("帮助LOL", "LOL"),
        ("帮助中心", "中心"),
    ],
)
def test_help_query_accepts_optional_argument_separator(text, expected):
    assert help_query(text) == expected


def test_help_poster_renders_png():
    plugins = CommandCatalog(PLUGIN_ROOT).find("lol")

    html = build_html(plugins)
    png = render_help(plugins)

    assert "LOL战绩" in html
    assert png.startswith(b"\x89PNG\r\n\x1a\n")

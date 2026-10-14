import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


@pytest.fixture
def configured_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANYSEARCH_API_KEY", "test-search-key")
    monkeypatch.setenv("NIU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NIU_GROUP_HISTORY_ENABLED", "false")
    return tmp_path

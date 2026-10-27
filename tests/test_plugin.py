import logging

from mimi.plugin import PluginManager


def test_child_python_log_level_is_preserved_from_stderr() -> None:
    assert (
        PluginManager._resolve_log_level(
            "INFO:runtime.ingress:pumpkin ingress decision",
            is_stderr=True,
        )
        == logging.INFO
    )
    assert (
        PluginManager._resolve_log_level(
            "WARNING:runtime.ingress:temporary issue",
            is_stderr=True,
        )
        == logging.WARNING
    )


def test_unstructured_stderr_remains_error() -> None:
    assert (
        PluginManager._resolve_log_level(
            "Traceback (most recent call last):",
            is_stderr=True,
        )
        == logging.ERROR
    )


def test_unstructured_stdout_defaults_to_info() -> None:
    assert (
        PluginManager._resolve_log_level(
            "plugin started",
            is_stderr=False,
        )
        == logging.INFO
    )

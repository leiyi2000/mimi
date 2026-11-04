import io
import logging
import subprocess
import threading

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


class FakeProcess:
    def __init__(self, pid: int, return_code: int, *, blocked: bool = False):
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self._return_code = return_code
        self._exited = threading.Event()
        if not blocked:
            self._exited.set()

    def poll(self) -> int | None:
        return self._return_code if self._exited.is_set() else None

    def wait(self, timeout: float | None = None) -> int:
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout)
        self.returncode = self._return_code
        return self._return_code

    def terminate(self) -> None:
        self._return_code = -15
        self._exited.set()

    def kill(self) -> None:
        self._return_code = -9
        self._exited.set()


def test_exited_plugin_is_restarted_with_backoff(
    monkeypatch,
    tmp_path,
    caplog,
) -> None:
    plugin = tmp_path / "pumpkin"
    plugin.mkdir()
    first = FakeProcess(10, -9)
    restarted = FakeProcess(11, 0, blocked=True)
    processes = iter((first, restarted))
    restarted_event = threading.Event()

    def popen(*args, **kwargs):
        process = next(processes)
        if process is restarted:
            restarted_event.set()
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(PluginManager, "_RESTART_INITIAL_DELAY_SECONDS", 0.01)
    manager = PluginManager()
    manager.plugins = [plugin]

    with caplog.at_level(logging.ERROR):
        manager.start()
        assert restarted_event.wait(1)
        manager.stop()

    assert "plugin pumpkin exited with code -9" in caplog.text
    assert "restarting in 0.0s" in caplog.text


def test_stopping_manager_does_not_restart_plugin(monkeypatch, tmp_path) -> None:
    plugin = tmp_path / "pumpkin"
    plugin.mkdir()
    process = FakeProcess(10, 0, blocked=True)
    spawn_count = 0

    def popen(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    manager = PluginManager()
    manager.plugins = [plugin]

    manager.start()
    manager.stop()

    assert spawn_count == 1

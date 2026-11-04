import os
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import ClassVar
import tomllib


log = logging.getLogger(__name__)


class PluginManager:
    _RESTART_INITIAL_DELAY_SECONDS = 1.0
    _RESTART_MAX_DELAY_SECONDS = 60.0
    _RESTART_STABLE_AFTER_SECONDS = 300.0
    _CHILD_LOG_LEVELS: ClassVar[dict[str, int]] = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }

    def __init__(self):
        self.plugins: list[Path] = []
        plugins_dir = Path(__file__).parent.parent / "plugins"
        if not plugins_dir.exists():
            plugins_dir.mkdir(parents=True, exist_ok=True)

        for plugin in plugins_dir.iterdir():
            if plugin.is_dir():
                self.plugins.append(plugin)
                log.info(f"load plugin {plugin.name} from {plugin}")

        self._validate_command_manifests()
        self.processes: list[subprocess.Popen[str]] = []
        self._process_lock = threading.Lock()
        self._monitor_threads: list[threading.Thread] = []
        self._stop_event = threading.Event()

    def _validate_command_manifests(self) -> None:
        owners: dict[str, str] = {}
        for plugin in self.plugins:
            path = plugin / "commands.toml"
            if not path.exists():
                continue
            with path.open("rb") as file:
                manifest = tomllib.load(file)
            for command in manifest.get("commands", []):
                name = str(command["name"]).strip()
                normalized = name.casefold()
                owner = owners.get(normalized)
                if owner is not None:
                    raise ValueError(
                        f"duplicate command {name!r} in plugins {owner!r} "
                        f"and {plugin.name!r}"
                    )
                owners[normalized] = plugin.name

    @classmethod
    def _resolve_log_level(cls, line: str, *, is_stderr: bool) -> int:
        level_name, separator, _ = line.partition(":")
        if separator:
            level = cls._CHILD_LOG_LEVELS.get(level_name)
            if level is not None:
                return level
        return logging.ERROR if is_stderr else logging.INFO

    def _stream_log(self, stream, prefix: str, *, is_stderr: bool) -> None:
        with stream:
            for line in stream:
                line = line.strip()
                if line:
                    level = self._resolve_log_level(
                        line,
                        is_stderr=is_stderr,
                    )
                    log.log(level, f"[{prefix}] {line}")

    def _spawn_plugin(self, plugin: Path) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            ["uv", "run", "main.py"],
            cwd=plugin,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            process.wait()
            raise RuntimeError(f"plugin {plugin.name} log pipes are unavailable")

        threading.Thread(
            target=self._stream_log,
            args=(process.stdout, plugin.name),
            kwargs={"is_stderr": False},
            daemon=True,
        ).start()
        threading.Thread(
            target=self._stream_log,
            args=(process.stderr, plugin.name),
            kwargs={"is_stderr": True},
            daemon=True,
        ).start()

        with self._process_lock:
            self.processes.append(process)
        log.info("plugin %s run with pid %s", plugin.name, process.pid)
        return process

    def _monitor_plugin(
        self,
        plugin: Path,
        process: subprocess.Popen[str],
    ) -> None:
        restart_delay = self._RESTART_INITIAL_DELAY_SECONDS
        started_at = time.monotonic()

        while True:
            return_code = process.wait()
            with self._process_lock:
                if process in self.processes:
                    self.processes.remove(process)

            if self._stop_event.is_set():
                return

            running_seconds = time.monotonic() - started_at
            if running_seconds >= self._RESTART_STABLE_AFTER_SECONDS:
                restart_delay = self._RESTART_INITIAL_DELAY_SECONDS

            log.error(
                "plugin %s exited with code %s after %.1fs; restarting in %.1fs",
                plugin.name,
                return_code,
                running_seconds,
                restart_delay,
            )
            if self._stop_event.wait(restart_delay):
                return

            try:
                process = self._spawn_plugin(plugin)
            except OSError:
                log.exception(
                    "plugin %s restart failed; retrying in %.1fs",
                    plugin.name,
                    restart_delay,
                )
                restart_delay = min(
                    restart_delay * 2,
                    self._RESTART_MAX_DELAY_SECONDS,
                )
                continue

            if self._stop_event.is_set():
                process.terminate()
                process.wait()
                with self._process_lock:
                    if process in self.processes:
                        self.processes.remove(process)
                return

            started_at = time.monotonic()
            restart_delay = min(
                restart_delay * 2,
                self._RESTART_MAX_DELAY_SECONDS,
            )

    def start(self) -> None:
        if self._monitor_threads:
            raise RuntimeError("plugin manager is already started")

        self._stop_event.clear()
        for plugin in self.plugins:
            process = self._spawn_plugin(plugin)
            monitor = threading.Thread(
                target=self._monitor_plugin,
                args=(plugin, process),
                name=f"plugin-monitor-{plugin.name}",
                daemon=True,
            )
            monitor.start()
            self._monitor_threads.append(monitor)

    def stop(self) -> None:
        self._stop_event.set()
        with self._process_lock:
            processes = list(self.processes)

        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        for monitor in self._monitor_threads:
            monitor.join(timeout=10)

        with self._process_lock:
            self.processes.clear()
        self._monitor_threads.clear()
        log.info("all plugins stopped")

    def restart(self) -> None:
        self.stop()
        self.start()
        log.info("all plugins restarted")

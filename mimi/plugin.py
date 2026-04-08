import os
import logging
import threading
import subprocess
from pathlib import Path


log = logging.getLogger(__name__)


class PluginManager:
    def __init__(self):
        self.plugins: list[Path] = []
        plugins_dir = Path(__file__).parent.parent / "plugins"
        if not plugins_dir.exists():
            plugins_dir.mkdir(parents=True, exist_ok=True)

        for plugin in plugins_dir.iterdir():
            if plugin.is_dir():
                self.plugins.append(plugin)
                log.info(f"load plugin {plugin.name} from {plugin}")

        self.processes: list[subprocess.Popen] = []

    def start(self):
        def stream_log(stream, prefix, is_error=False):
            log_func = log.error if is_error else log.info
            with stream:
                for line in stream:
                    line = line.strip()
                    if line:
                        log_func(f"[{prefix}] {line}")

        for plugin in self.plugins:
            # hardcode run main.py
            cmd = ["uv", "run", "main.py"]
            process = subprocess.Popen(
                cmd,
                cwd=plugin,
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )

            threading.Thread(
                target=stream_log,
                args=(process.stdout, plugin.name, False),
                daemon=True,
            ).start()

            threading.Thread(
                target=stream_log,
                args=(process.stderr, plugin.name, True),
                daemon=True,
            ).start()

            log.info(f"plugin {plugin.name} run with pid {process.pid}")
            self.processes.append(process)

    def stop(self):
        for process in self.processes:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self.processes.clear()
        log.info("all plugins stopped")

    def restart(self):
        self.stop()
        self.start()
        log.info("all plugins restarted")

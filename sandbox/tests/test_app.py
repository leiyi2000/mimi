from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from app import RequestError, SandboxExecutor, Settings


class SandboxExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            token="test-token",
            workspace_root=Path(self.temp_dir.name),
            max_request_bytes=1024,
            max_script_bytes=128,
            max_input_bytes=32,
            max_input_files=2,
            max_output_bytes=128,
            max_artifact_bytes=16,
            max_artifacts=2,
            max_timeout=5,
            max_concurrency=1,
            memory_bytes=128 * 1024 * 1024,
            max_processes=8,
            proxy_url="http://proxy:3128",
        )
        self.executor = SandboxExecutor(self.settings)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_rejects_unsafe_input_paths(self) -> None:
        for path in (
            ".",
            "/etc/passwd",
            "../secret",
            "a/../../secret",
            "output/x",
        ):
            with self.subTest(path=path):
                with self.assertRaises(RequestError):
                    self.executor._validate_files({path: "x"})

    def test_rejects_oversized_inputs(self) -> None:
        with self.assertRaisesRegex(RequestError, "size limit"):
            self.executor._validate_files({"input.txt": "x" * 33})

    def test_rejects_too_many_input_files(self) -> None:
        with self.assertRaisesRegex(RequestError, "file count"):
            self.executor._validate_files({"a": "", "b": "", "c": ""})

    def test_collects_only_regular_output_files(self) -> None:
        output = Path(self.temp_dir.name) / "output"
        output.mkdir()
        (output / "result.txt").write_text("result", encoding="utf-8")
        (output / "passwd").symlink_to("/etc/passwd")

        files, truncated = self.executor._collect_artifacts(output)

        self.assertTrue(truncated)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].path, "result.txt")
        self.assertEqual(base64.b64decode(files[0].data), b"result")

    def test_marks_artifact_limit_as_truncated(self) -> None:
        output = Path(self.temp_dir.name) / "output"
        output.mkdir()
        (output / "large.bin").write_bytes(b"x" * 17)

        files, truncated = self.executor._collect_artifacts(output)

        self.assertEqual(files, ())
        self.assertTrue(truncated)


if __name__ == "__main__":
    unittest.main()

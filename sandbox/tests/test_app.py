from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.chown = patch("app.os.chown")
        self.chown.start()
        self.executor = SandboxExecutor(self.settings)

    def tearDown(self) -> None:
        self.chown.stop()
        self.temp_dir.cleanup()

    def test_rejects_unsafe_input_paths(self) -> None:
        for path in (
            ".",
            "/etc/passwd",
            "../secret",
            "a/../../secret",
            "output/x",
        ):
            with self.subTest(path=path), self.assertRaises(RequestError):
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

    def test_workspace_files_persist_within_one_run(self) -> None:
        workspace_id = "a" * 32

        written = self.executor.write_workspace(
            workspace_id,
            {"path": "data/input.txt", "content": "hello"},
        )
        read = self.executor.read_workspace(
            workspace_id,
            {"path": "data/input.txt"},
        )
        listed = self.executor.list_workspace(
            workspace_id,
            {"path": "", "recursive": True},
        )

        self.assertEqual(written["size"], 5)
        self.assertEqual(read["content"], "hello")
        self.assertIn(
            "data/input.txt",
            {entry["path"] for entry in listed["entries"]},
        )

    def test_workspaces_are_isolated(self) -> None:
        self.executor.write_workspace(
            "a" * 32,
            {"path": "secret.txt", "content": "one"},
        )

        with self.assertRaisesRegex(RequestError, "not found"):
            self.executor.read_workspace(
                "b" * 32,
                {"path": "secret.txt"},
            )

    def test_workspace_rejects_traversal_and_symlinks(self) -> None:
        workspace_id = "c" * 32
        with self.assertRaises(RequestError):
            self.executor.write_workspace(
                workspace_id,
                {"path": "../secret", "content": "x"},
            )

        state = self.executor._get_workspace(workspace_id)
        (state.path / "passwd").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(RequestError, "symbolic links"):
            self.executor.read_workspace(
                workspace_id,
                {"path": "passwd"},
            )

    def test_deleted_workspace_cannot_be_reopened(self) -> None:
        workspace_id = "d" * 32
        self.executor.write_workspace(
            workspace_id,
            {"path": "result.txt", "content": "done"},
        )

        self.executor.delete_workspace(workspace_id)

        self.assertFalse((Path(self.temp_dir.name) / workspace_id).exists())
        with self.assertRaisesRegex(RequestError, "already been deleted"):
            self.executor.list_workspace(workspace_id, {})


if __name__ == "__main__":
    unittest.main()

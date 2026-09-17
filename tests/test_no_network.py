import ast
import io
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from papercuts import cli


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
EXPECTED_GIT_ARGV = [
    "git",
    "rev-parse",
    "--path-format=absolute",
    "--show-toplevel",
    "--absolute-git-dir",
    "--git-common-dir",
]
EXPECTED_HEAD_ARGV = ["git", "rev-parse", "--verify", "--quiet", "HEAD^{commit}"]
EMPTY_MARKDOWN = "# Papercuts\n\n_No records._\n"


class NoNetworkBoundaryTests(unittest.TestCase):
    def _guarded_cli(self, repo, *argv):
        old_cwd = os.getcwd()
        real_run = subprocess.run
        expected_env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        expected_calls = (
            (
                EXPECTED_GIT_ARGV,
                ".",
            ),
        )
        if argv[0] == "packet":
            expected_calls = (
                (EXPECTED_GIT_ARGV, "."),
                (EXPECTED_GIT_ARGV, str(repo)),
                (EXPECTED_HEAD_ARGV, str(repo)),
                (EXPECTED_GIT_ARGV, str(repo)),
                (EXPECTED_HEAD_ARGV, str(repo)),
            )
        calls = []
        stdout = io.StringIO()
        stderr = io.StringIO()

        def guarded_run(command, **kwargs):
            call_number = len(calls)
            self.assertLess(call_number, len(expected_calls))
            expected_command, expected_cwd = expected_calls[call_number]
            self.assertEqual(command, expected_command)
            self.assertEqual(
                kwargs,
                {
                    "cwd": expected_cwd,
                    "env": expected_env,
                    "shell": False,
                    "capture_output": True,
                    "check": False,
                },
            )
            calls.append((command, kwargs["cwd"]))
            return real_run(command, **kwargs)

        try:
            os.chdir(repo)
            with mock.patch.object(
                subprocess, "run", side_effect=guarded_run
            ), mock.patch.object(
                socket, "socket", side_effect=AssertionError("network access")
            ), mock.patch.object(
                cli.sys, "stdout", stdout
            ), mock.patch.object(
                cli.sys, "stderr", stderr
            ):
                code = cli.main(argv)
        finally:
            os.chdir(old_cwd)
        self.assertEqual(calls, list(expected_calls))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_socket_and_subprocess_denial_wrap_all_six_commands(self):
        cases = (
            (
                "record",
                (
                    "record",
                    "--category",
                    "docs",
                    "--summary",
                    "summary",
                    "--expected",
                    "expected",
                    "--observed",
                    "observed",
                    "--evidence-basis",
                    "test",
                ),
                None,
            ),
            (
                "resolve",
                (
                    "resolve",
                    "00000000-0000-4000-8000-000000000099",
                    "--resolution",
                    "fixed",
                ),
                (2, "", "papercuts: E_UNKNOWN_RECORD field=record_id\n"),
            ),
            ("list", ("list",), (0, "", "")),
            ("render", ("render",), (0, EMPTY_MARKDOWN, "")),
            ("check", ("check",), (0, "ok: 0 events, 0 records\n", "")),
            ("packet", ("packet", "--expect-head", "unborn"), None),
        )
        for name, argv, expected in cases:
            with self.subTest(command=name), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory)
                git_env = dict(os.environ)
                git_env.update(
                    {
                        "LC_ALL": "C",
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": "/dev/null",
                        "GIT_AUTHOR_NAME": "Papercuts Test",
                        "GIT_AUTHOR_EMAIL": "papercuts-test@example.invalid",
                        "GIT_COMMITTER_NAME": "Papercuts Test",
                        "GIT_COMMITTER_EMAIL": "papercuts-test@example.invalid",
                    }
                )
                subprocess.run(
                    ["git", "init", "-q"], cwd=repo, env=git_env, check=True
                )
                result = self._guarded_cli(repo, *argv)
                if name == "record":
                    self.assertEqual(result[0], 0)
                    self.assertEqual(result[2], "")
                    self.assertIsNotNone(
                        re.fullmatch(
                            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
                            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\n",
                            result[1],
                        )
                    )
                elif name == "packet":
                    self.assertEqual(result[0], 0)
                    self.assertTrue(result[1].endswith("\n"))
                    self.assertEqual(result[2], "")
                else:
                    self.assertEqual(result, expected)

    def test_static_import_boundary_is_narrow_and_bounded(self):
        forbidden = {
            "socket",
            "urllib",
            "requests",
            "httpx",
            "selenium",
            "playwright",
            "browser",
        }
        for path in sorted((SOURCE_ROOT / "papercuts").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module.split(".")[0])
            self.assertFalse(
                forbidden.intersection(imports),
                f"network/browser import in {path.name}",
            )
            if path.name != "repo.py":
                self.assertNotIn(
                    "subprocess", imports, f"subprocess import in {path.name}"
                )


if __name__ == "__main__":
    unittest.main()

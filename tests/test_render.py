import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from papercuts import cli


FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXPECTED_GIT_ARGV = [
    "git",
    "rev-parse",
    "--path-format=absolute",
    "--show-toplevel",
    "--absolute-git-dir",
    "--git-common-dir",
]
EMPTY_MARKDOWN = b"# Papercuts\n\n_No records._\n"

HOSTILE_SUMMARY = (
    "summary <b>x</b> [link](https://example.test) "
    "<https://example.test> `tick` \\ | pipe\nnext"
)
HOSTILE_EXPECTED = (
    "expected <i>x</i> [link](https://example.test) "
    "<https://example.test> `tick` \\ | pipe\nnext"
)
HOSTILE_OBSERVED = (
    "observed <u>x</u> [link](https://example.test) "
    "<https://example.test> `tick` \\ | pipe\nnext"
)
HOSTILE_RESOLUTION = (
    "resolution <em>x</em> [link](https://example.test) "
    "<https://example.test> `tick` \\ | pipe\nnext"
)

FIRST_ID = "00000000-0000-4000-8000-000000000101"
SECOND_ID = "00000000-0000-4000-8000-000000000102"
THIRD_ID = "00000000-0000-4000-8000-000000000103"
EVENTS = [
    {
        "schema_version": 1,
        "event_id": "00000000-0000-4000-8000-000000000001",
        "event_type": "recorded",
        "occurred_at": "2026-01-03T03:04:05.000000Z",
        "record_id": FIRST_ID,
        "category": "docs",
        "summary": HOSTILE_SUMMARY,
        "expected": HOSTILE_EXPECTED,
        "observed": HOSTILE_OBSERVED,
        "evidence_basis": "test",
        "recurrence_key": "same-bug",
    },
    {
        "schema_version": 1,
        "event_id": "00000000-0000-4000-8000-000000000002",
        "event_type": "recorded",
        "occurred_at": "2026-01-02T03:04:05.000000Z",
        "record_id": SECOND_ID,
        "category": "tooling",
        "summary": "second",
        "expected": "works",
        "observed": "broken",
        "evidence_basis": "inferred",
        "recurrence_key": "same-bug",
    },
    {
        "schema_version": 1,
        "event_id": "00000000-0000-4000-8000-000000000003",
        "event_type": "recorded",
        "occurred_at": "2026-01-01T03:04:05.000000Z",
        "record_id": THIRD_ID,
        "category": "validation",
        "summary": "third",
        "expected": "a result",
        "observed": "no result",
        "evidence_basis": "user_observation",
        "recurrence_key": None,
    },
    {
        "schema_version": 1,
        "event_id": "00000000-0000-4000-8000-000000000004",
        "event_type": "resolved",
        "occurred_at": "2026-01-04T03:04:05.000000Z",
        "record_id": FIRST_ID,
        "resolution": HOSTILE_RESOLUTION,
    },
]


def canonical_ledger(events):
    return b"".join(
        json.dumps(
            event,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
        for event in events
    )


class FailingStdout:
    def write(self, text):
        raise OSError("stdout closed")

    def flush(self):
        raise OSError("stdout closed")


class RenderCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.storage = self.repo / ".git" / "papercuts"
        self.storage.mkdir(mode=0o700)
        self.lock = self.storage / "write.lock"
        self.lock.touch(mode=0o600)
        self.ledger = self.storage / "events.jsonl"
        self.ledger.write_bytes(canonical_ledger(EVENTS))
        self.ledger.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def _call(self, *argv, locale="C", timezone="UTC", stdout=None):
        old_cwd = os.getcwd()
        real_run = subprocess.run
        expected_env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        captured_stdout = io.StringIO() if stdout is None else stdout
        captured_stderr = io.StringIO()

        def guarded_run(command, **kwargs):
            self.assertEqual(command, EXPECTED_GIT_ARGV)
            self.assertEqual(
                kwargs,
                {
                    "cwd": ".",
                    "env": expected_env,
                    "shell": False,
                    "capture_output": True,
                    "check": False,
                },
            )
            return real_run(command, **kwargs)

        try:
            os.chdir(self.repo)
            with mock.patch.object(
                subprocess, "run", side_effect=guarded_run
            ), mock.patch.dict(
                os.environ,
                {"LC_ALL": locale, "TZ": timezone},
                clear=False,
            ), mock.patch.object(
                cli.sys, "stdout", captured_stdout
            ), mock.patch.object(
                cli.sys, "stderr", captured_stderr
            ):
                code = cli.main(argv)
        finally:
            os.chdir(old_cwd)
        text = captured_stdout.getvalue() if stdout is None else None
        return code, text, captured_stderr.getvalue()

    def assert_exact_render(self, argv, expected):
        code, stdout, stderr = self._call(*argv)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.encode("utf-8"), expected)

    def test_render_default_open_matches_exact_golden(self):
        self.assert_exact_render(
            ("render",), (FIXTURES / "expected-open.md").read_bytes()
        )

    def test_render_explicit_open_matches_exact_golden(self):
        self.assert_exact_render(
            ("render", "--status", "open"),
            (FIXTURES / "expected-open.md").read_bytes(),
        )

    def test_render_all_preserves_first_recorded_order_and_recurrence_counts(self):
        self.assert_exact_render(
            ("render", "--status", "all"),
            (FIXTURES / "expected-all.md").read_bytes(),
        )

    def test_render_resolved_matches_exact_markdown(self):
        expected = (
            "# Papercuts\n\n"
            f"## {FIRST_ID} | resolved | docs | recurrence=2\n"
            f"    summary={json.dumps(HOSTILE_SUMMARY, ensure_ascii=False, separators=(',', ':'))}\n"
            f"    expected={json.dumps(HOSTILE_EXPECTED, ensure_ascii=False, separators=(',', ':'))}\n"
            f"    observed={json.dumps(HOSTILE_OBSERVED, ensure_ascii=False, separators=(',', ':'))}\n"
            "    evidence_basis=test\n"
            f"    resolution={json.dumps(HOSTILE_RESOLUTION, ensure_ascii=False, separators=(',', ':'))}\n"
        ).encode("utf-8")
        self.assert_exact_render(("render", "--status", "resolved"), expected)

    def test_render_nullable_capture_fields_as_visible_null(self):
        compact = {
            "schema_version": 1,
            "event_id": "00000000-0000-4000-8000-000000000010",
            "event_type": "recorded",
            "occurred_at": "2026-01-05T03:04:05.000000Z",
            "record_id": FIRST_ID,
            "category": None,
            "summary": "brief friction",
            "expected": None,
            "observed": None,
            "evidence_basis": None,
            "recurrence_key": None,
        }
        self.ledger.write_bytes(canonical_ledger((compact,)))
        expected = (
            "# Papercuts\n\n"
            f"## {FIRST_ID} | open | null | recurrence=1\n"
            '    summary="brief friction"\n'
            "    expected=null\n"
            "    observed=null\n"
            "    evidence_basis=null\n"
        ).encode("utf-8")

        self.assert_exact_render(("render", "--status", "all"), expected)

    def test_render_nonempty_ledger_with_empty_selection_is_exact(self):
        self.ledger.write_bytes(canonical_ledger((EVENTS[0], EVENTS[3])))
        self.assert_exact_render(("render", "--status", "open"), EMPTY_MARKDOWN)

    def test_render_canonicalizes_all_caller_strings_as_inert_json_code_lines(self):
        code, stdout, stderr = self._call("render", "--status", "all")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        for field, value in (
            ("summary", HOSTILE_SUMMARY),
            ("expected", HOSTILE_EXPECTED),
            ("observed", HOSTILE_OBSERVED),
            ("resolution", HOSTILE_RESOLUTION),
        ):
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            self.assertIn(f"    {field}={encoded}\n", stdout)
        self.assertIn("    evidence_basis=test\n", stdout)
        self.assertNotIn('    evidence_basis="test"\n', stdout)
        self.assertNotIn("\n<b>", stdout)
        self.assertNotIn("\n[link]", stdout)
        self.assertNotIn("\n<https://", stdout)

    def test_render_all_is_exact_under_two_locale_timezone_environments(self):
        expected = (FIXTURES / "expected-all.md").read_bytes()
        first = self._call(
            "render", "--status", "all", locale="C", timezone="UTC"
        )
        second = self._call(
            "render",
            "--status",
            "all",
            locale="C.utf8",
            timezone="Pacific/Honolulu",
        )
        self.assertEqual(first[0], 0)
        self.assertEqual(first[2], "")
        self.assertEqual(first[1].encode("utf-8"), expected)
        self.assertEqual(second[0], 0)
        self.assertEqual(second[2], "")
        self.assertEqual(second[1].encode("utf-8"), expected)
        self.assertEqual(first[1].encode("utf-8"), second[1].encode("utf-8"))

    def test_absent_ledger_render_is_read_only_exact_empty_markdown(self):
        self.ledger.unlink()
        self.lock.unlink()
        self.storage.rmdir()
        code, stdout, stderr = self._call("render")
        self.assertFalse(self.storage.exists())
        self.assertEqual((code, stdout.encode("utf-8"), stderr), (0, EMPTY_MARKDOWN, ""))

    def test_render_malformed_history_reports_line_one_and_preserves_bytes(self):
        before = b"not-json\n"
        self.ledger.write_bytes(before)
        code, stdout, stderr = self._call("render")
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(
            (code, stdout, stderr),
            (1, "", "papercuts: E_INVALID_JSON line=1\n"),
        )

    def test_render_lock_contention_reports_busy_and_preserves_ledger(self):
        before = self.ledger.read_bytes()
        with self.lock.open("r+b", buffering=0) as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, stdout, stderr = self._call("render", "--status", "all")
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(
            (code, stdout, stderr),
            (1, "", "papercuts: E_LOCK_BUSY\n"),
        )

    def test_render_stdout_failure_reports_read_error_and_preserves_ledger(self):
        before = self.ledger.read_bytes()
        code, stdout, stderr = self._call(
            "render", "--status", "all", stdout=FailingStdout()
        )
        self.assertIsNone(stdout)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual((code, stderr), (1, "papercuts: E_OUTPUT\n"))


if __name__ == "__main__":
    unittest.main()

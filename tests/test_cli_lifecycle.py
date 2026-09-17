import fcntl
import importlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
VALID_BYTES = (FIXTURES / "valid-events.jsonl").read_bytes()
EXPECTED_BYTES = (FIXTURES / "expected-packet.json").read_bytes()
COMMITTED_HEAD = "cf2fabd85c9bf73416772e33b3812a6c87074c49"
EVENT_1 = "00000000-0000-4000-8000-000000000001"
EVENT_2 = "00000000-0000-4000-8000-000000000002"
EVENT_3 = "00000000-0000-4000-8000-000000000003"
RECORD_1 = "00000000-0000-4000-8000-000000000101"
RECORD_2 = "00000000-0000-4000-8000-000000000102"
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _fixture_git(root, *args, input_bytes=None):
    env = dict(os.environ)
    env.update(
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
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        env=env,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
    return completed


def _install_fixed_commit(root):
    commit = (
        f"tree {_EMPTY_TREE}\n"
        "author Papercuts Test <papercuts-test@example.invalid> 0 +0000\n"
        "committer Papercuts Test <papercuts-test@example.invalid> 0 +0000\n"
        "\n"
        "synthetic committed HEAD\n"
    ).encode("ascii")
    actual = _fixture_git(
        root,
        "hash-object",
        "-t",
        "commit",
        "-w",
        "--stdin",
        input_bytes=commit,
    ).stdout.decode("ascii").strip()
    if actual != COMMITTED_HEAD:
        raise AssertionError((actual, COMMITTED_HEAD))
    _fixture_git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _fixture_git(root, "update-ref", "refs/heads/main", COMMITTED_HEAD)


class FailingStdout:
    def write(self, text):
        raise OSError("stdout closed")

    def flush(self):
        raise OSError("stdout closed")


class CliLifecycleTests(unittest.TestCase):
    @contextmanager
    def _repo(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            env = dict(os.environ)
            env.update(
                {
                    "LC_ALL": "C",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": str(SOURCE_ROOT),
                }
            )
            yield repo, env

    def _run(self, repo, env, *args):
        return subprocess.run(
            [sys.executable, "-B", "-m", "papercuts", *args],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _record_args(self, summary="first"):
        return (
            "record",
            "--category",
            "docs",
            "--summary",
            summary,
            "--expected",
            "works",
            "--observed",
            "broken",
            "--evidence-basis",
            "test",
        )

    def _record(self, repo, env, summary="first"):
        result = self._run(repo, env, *self._record_args(summary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        record_id = result.stdout.strip()
        self.assertEqual(str(uuid.UUID(record_id)), record_id)
        return record_id

    def _ledger(self, repo):
        return repo / ".git" / "papercuts" / "events.jsonl"

    def _install_packet_fixture(self, repo):
        _install_fixed_commit(repo)
        storage = repo / ".git" / "papercuts"
        storage.mkdir(mode=0o700)
        storage.chmod(0o700)
        lock = storage / "write.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        ledger = storage / "events.jsonl"
        ledger.write_bytes(VALID_BYTES)
        ledger.chmod(0o600)

    def test_packet_is_discoverable_and_requires_expected_head(self):
        with self._repo() as (repo, env):
            help_result = self._run(repo, env, "--help")
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("packet", help_result.stdout)

            missing = self._run(repo, env, "packet")
            self.assertEqual(
                (missing.returncode, missing.stdout, missing.stderr),
                (2, "", "papercuts: E_ARGUMENT\n"),
            )

    def test_packet_emits_exact_fixture_and_leaves_storage_unchanged(self):
        with self._repo() as (repo, env):
            self._install_packet_fixture(repo)
            storage = repo / ".git" / "papercuts"
            before = {
                entry.name: (entry.read_bytes(), stat.S_IMODE(entry.stat().st_mode))
                for entry in storage.iterdir()
            }

            result = self._run(repo, env, "packet", "--expect-head", COMMITTED_HEAD)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.encode("utf-8"), EXPECTED_BYTES)
            self.assertEqual(result.stderr, "")
            after = {
                entry.name: (entry.read_bytes(), stat.S_IMODE(entry.stat().st_mode))
                for entry in storage.iterdir()
            }
            self.assertEqual(after, before)

    def test_packet_without_storage_emits_empty_unborn_packet(self):
        with self._repo() as (repo, env):
            result = self._run(repo, env, "packet", "--expect-head", "unborn")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            packet = json.loads(result.stdout)
            self.assertEqual(packet["events"], [])
            self.assertEqual(packet["records"], [])
            self.assertEqual(packet["read_watermark"]["event_count"], 0)
            self.assertEqual(packet["read_watermark"]["record_count"], 0)
            self.assertEqual(packet["source_repository"]["head_state"], "unborn")
            self.assertIsNone(packet["source_repository"]["head_commit"])
            self.assertFalse((repo / ".git" / "papercuts").exists())

    def test_packet_supports_repeatable_record_and_event_selectors(self):
        cases = (
            (
                ("--record-id", RECORD_2, "--record-id", RECORD_1),
                "record_ids",
                [RECORD_1, RECORD_2],
                [],
                [EVENT_1, EVENT_2, EVENT_3],
                [RECORD_1, RECORD_2],
            ),
            (
                ("--event-id", EVENT_3, "--event-id", EVENT_1),
                "event_ids",
                [],
                [EVENT_1, EVENT_3],
                [EVENT_1, EVENT_3],
                [RECORD_1],
            ),
        )
        with self._repo() as (repo, env):
            self._install_packet_fixture(repo)
            for selector_args, scope, requested_records, requested_events, event_ids, record_ids in cases:
                with self.subTest(scope=scope):
                    result = self._run(
                        repo,
                        env,
                        "packet",
                        "--expect-head",
                        COMMITTED_HEAD,
                        *selector_args,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    packet = json.loads(result.stdout)
                    self.assertEqual(
                        packet["selection"],
                        {
                            "scope_kind": scope,
                            "requested_record_ids": requested_records,
                            "requested_event_ids": requested_events,
                        },
                    )
                    self.assertEqual(
                        [item["event"]["event_id"] for item in packet["events"]],
                        event_ids,
                    )
                    self.assertEqual(
                        [item["record"]["record_id"] for item in packet["records"]],
                        record_ids,
                    )

    def test_packet_failures_are_stable_and_emit_no_stdout(self):
        unknown_record = "00000000-0000-4000-8000-000000000099"
        with self._repo() as (repo, env):
            self._install_packet_fixture(repo)
            cases = (
                (
                    ("--expect-head", "unborn", "--record-id", RECORD_1),
                    "papercuts: E_PACKET_WRONG_HEAD\n",
                ),
                (
                    (
                        "--expect-head",
                        COMMITTED_HEAD,
                        "--record-id",
                        unknown_record,
                    ),
                    "papercuts: E_UNKNOWN_RECORD\n",
                ),
            )
            for args, diagnostic in cases:
                with self.subTest(diagnostic=diagnostic):
                    result = self._run(repo, env, "packet", *args)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (2, "", diagnostic))
                    self.assertNotIn(str(repo), result.stderr)
                    self.assertNotIn(unknown_record, result.stderr)

    def _load_cli(self):
        try:
            return importlib.import_module("papercuts.cli")
        except ModuleNotFoundError:
            self.fail("papercuts.cli is missing")

    def test_absent_list_and_check_are_read_only(self):
        with self._repo() as (repo, env):
            listed = self._run(repo, env, "list")
            checked = self._run(repo, env, "check")
            self.assertEqual((listed.returncode, listed.stdout, listed.stderr), (0, "", ""))
            self.assertEqual(
                (checked.returncode, checked.stdout, checked.stderr),
                (0, "ok: 0 events, 0 records\n", ""),
            )
            self.assertFalse((repo / ".git" / "papercuts").exists())

    def test_empty_and_lock_only_list_and_check_are_read_only(self):
        for state in ("empty", "lock"):
            with self.subTest(state=state), self._repo() as (repo, env):
                storage = repo / ".git" / "papercuts"
                storage.mkdir(mode=0o700)
                if state == "lock":
                    (storage / "write.lock").touch(mode=0o600)
                before = sorted(entry.name for entry in storage.iterdir())
                listed = self._run(repo, env, "list")
                checked = self._run(repo, env, "check")
                self.assertEqual(
                    (listed.returncode, listed.stdout, listed.stderr),
                    (0, "", ""),
                )
                self.assertEqual(
                    (checked.returncode, checked.stdout, checked.stderr),
                    (0, "ok: 0 events, 0 records\n", ""),
                )
                self.assertEqual(
                    sorted(entry.name for entry in storage.iterdir()), before
                )

    def test_record_list_and_check_emit_exact_output(self):
        with self._repo() as (repo, env):
            record_id = self._record(repo, env)
            listed = self._run(repo, env, "list")
            checked = self._run(repo, env, "check")
            self.assertEqual(
                (listed.returncode, listed.stdout, listed.stderr),
                (0, f'{record_id}\topen\tdocs\t"first"\n', ""),
            )
            self.assertEqual(
                (checked.returncode, checked.stdout, checked.stderr),
                (0, "ok: 1 events, 1 records\n", ""),
            )

    def test_resolve_and_status_filters_preserve_recorded_order(self):
        with self._repo() as (repo, env):
            first_id = self._record(repo, env, "first")
            second_id = self._record(repo, env, "second")
            resolved = self._run(
                repo, env, "resolve", first_id, "--resolution", "fixed"
            )
            self.assertEqual(
                (resolved.returncode, resolved.stdout, resolved.stderr),
                (0, f"resolved {first_id}\n", ""),
            )
            open_rows = self._run(repo, env, "list")
            resolved_rows = self._run(repo, env, "list", "--status", "resolved")
            all_rows = self._run(repo, env, "list", "--status", "all")
            self.assertEqual(
                open_rows.stdout, f'{second_id}\topen\tdocs\t"second"\n'
            )
            self.assertEqual(
                resolved_rows.stdout,
                f'{first_id}\tresolved\tdocs\t"first"\n',
            )
            self.assertEqual(
                all_rows.stdout,
                f'{first_id}\tresolved\tdocs\t"first"\n'
                f'{second_id}\topen\tdocs\t"second"\n',
            )
            self.assertEqual(
                (open_rows.returncode, resolved_rows.returncode, all_rows.returncode),
                (0, 0, 0),
            )
            checked = self._run(repo, env, "check")
            self.assertEqual(checked.stdout, "ok: 3 events, 2 records\n")

    def test_unknown_resolution_is_input_rejection_without_ledger(self):
        with self._repo() as (repo, env):
            result = self._run(
                repo,
                env,
                "resolve",
                "00000000-0000-4000-8000-000000000000",
                "--resolution",
                "fixed",
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(
                result.stderr,
                "papercuts: E_UNKNOWN_RECORD field=record_id\n",
            )
            storage = repo / ".git" / "papercuts"
            self.assertTrue(storage.is_dir())
            self.assertFalse((storage / "events.jsonl").exists())

    def test_duplicate_resolution_is_input_rejection_without_append(self):
        with self._repo() as (repo, env):
            record_id = self._record(repo, env)
            first = self._run(
                repo, env, "resolve", record_id, "--resolution", "fixed"
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            ledger = self._ledger(repo)
            before = ledger.read_bytes()
            duplicate = self._run(
                repo, env, "resolve", record_id, "--resolution", "again"
            )
            self.assertEqual(duplicate.returncode, 2)
            self.assertEqual(duplicate.stdout, "")
            self.assertEqual(
                duplicate.stderr,
                "papercuts: E_DUPLICATE_RESOLUTION field=record_id\n",
            )
            self.assertEqual(ledger.read_bytes(), before)

    def test_privacy_rejection_precedes_storage_and_discloses_no_value(self):
        with self._repo() as (repo, env):
            secret = "sk-" + "a" * 24
            result = self._run(repo, env, *self._record_args(secret))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(
                result.stderr,
                "papercuts: E_PRIVACY_SK_TOKEN field=summary\n",
            )
            self.assertNotIn(secret, result.stderr)
            self.assertNotIn(str(len(secret)), result.stderr)
            self.assertFalse((repo / ".git" / "papercuts").exists())

    def test_invalid_argument_is_stable_rejection_without_storage(self):
        with self._repo() as (repo, env):
            result = self._run(repo, env, "list", "--status", "invalid")
            self.assertEqual(
                (result.returncode, result.stdout, result.stderr),
                (2, "", "papercuts: E_ARGUMENT\n"),
            )
            self.assertFalse((repo / ".git" / "papercuts").exists())

    def test_malformed_history_is_operational_failure_without_mutation(self):
        with self._repo() as (repo, env):
            self._record(repo, env)
            ledger = self._ledger(repo)
            ledger.write_bytes(ledger.read_bytes()[:-1])
            before = ledger.read_bytes()
            for args in (("check",), self._record_args("blocked")):
                with self.subTest(command=args[0]):
                    result = self._run(repo, env, *args)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(
                        result.stderr, "papercuts: E_MISSING_FINAL_LF\n"
                    )
                    self.assertEqual(ledger.read_bytes(), before)

    def test_persisted_input_rule_collisions_are_operational_failures(self):
        record_id = "00000000-0000-4000-8000-000000000001"
        unknown_id = "00000000-0000-4000-8000-000000000099"
        first_event_id = "00000000-0000-4000-8000-000000000002"
        second_event_id = "00000000-0000-4000-8000-000000000003"
        third_event_id = "00000000-0000-4000-8000-000000000004"
        occurred_at = "2026-07-16T00:00:00.000000Z"

        def recorded(**overrides):
            event = {
                "schema_version": 1,
                "event_id": first_event_id,
                "event_type": "recorded",
                "occurred_at": occurred_at,
                "record_id": record_id,
                "category": "docs",
                "summary": "first",
                "expected": "works",
                "observed": "broken",
                "evidence_basis": "test",
                "recurrence_key": None,
            }
            event.update(overrides)
            return event

        def resolved(event_id=second_event_id, target_id=record_id):
            return {
                "schema_version": 1,
                "event_id": event_id,
                "event_type": "resolved",
                "occurred_at": occurred_at,
                "record_id": target_id,
                "resolution": "fixed",
            }

        cases = {
            "E_ENUM": (
                [recorded(category="private")],
                "papercuts: E_ENUM line=1 field=category\n",
            ),
            "E_UUID4": (
                [recorded(event_id="00000000-0000-1000-8000-000000000002")],
                "papercuts: E_UUID4 line=1 field=event_id\n",
            ),
            "E_UNKNOWN_RECORD": (
                [resolved(target_id=unknown_id)],
                "papercuts: E_UNKNOWN_RECORD line=1 field=record_id\n",
            ),
            "E_DUPLICATE_RESOLUTION": (
                [
                    recorded(),
                    resolved(),
                    resolved(event_id=third_event_id),
                ],
                "papercuts: E_DUPLICATE_RESOLUTION line=3 field=record_id\n",
            ),
        }

        for rule_id, (events, diagnostic) in cases.items():
            with self.subTest(rule_id=rule_id), self._repo() as (repo, env):
                storage = repo / ".git" / "papercuts"
                storage.mkdir(mode=0o700)
                storage.chmod(0o700)
                lock = storage / "write.lock"
                lock.touch(mode=0o600)
                lock.chmod(0o600)
                ledger = storage / "events.jsonl"
                ledger.write_bytes(
                    b"".join(
                        (
                            json.dumps(
                                event,
                                sort_keys=True,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        ).encode("utf-8")
                        for event in events
                    )
                )
                ledger.chmod(0o600)
                before = ledger.read_bytes()

                result = self._run(repo, env, "check")

                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr),
                    (1, "", diagnostic),
                )
                self.assertEqual(ledger.read_bytes(), before)

    def test_lock_contention_is_operational_failure_without_mutation(self):
        with self._repo() as (repo, env):
            self._record(repo, env)
            ledger = self._ledger(repo)
            before = ledger.read_bytes()
            lock_path = ledger.with_name("write.lock")
            lock_fd = os.open(lock_path, os.O_RDWR)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for args in (("list",), self._record_args("blocked")):
                    with self.subTest(command=args[0]):
                        result = self._run(repo, env, *args)
                        self.assertEqual(result.returncode, 1)
                        self.assertEqual(result.stdout, "")
                        self.assertEqual(result.stderr, "papercuts: E_LOCK_BUSY\n")
                        self.assertEqual(ledger.read_bytes(), before)
            finally:
                os.close(lock_fd)

    def test_unsafe_storage_is_non_disclosing_operational_failure(self):
        with self._repo() as (repo, env):
            self._record(repo, env)
            ledger = self._ledger(repo)
            ledger.chmod(0o644)
            result = self._run(repo, env, "list")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "papercuts: E_STORAGE_UNSAFE\n")
            self.assertNotIn(str(repo), result.stderr)
            self.assertNotIn("events.jsonl", result.stderr)

    def test_indeterminate_append_maps_once_without_retry_or_rollback(self):
        with self._repo() as (repo, _env):
            cli = self._load_cli()
            store = importlib.import_module("papercuts.store")
            calls = 0

            def fail_write(_fd, _data):
                nonlocal calls
                calls += 1
                raise OSError("synthetic write failure")

            old_cwd = os.getcwd()
            try:
                os.chdir(repo)
                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch.object(store.os, "write", fail_write), mock.patch.object(
                    cli.sys, "stdout", stdout
                ), mock.patch.object(cli.sys, "stderr", stderr):
                    result = cli.main(list(self._record_args()))
                self.assertEqual(result, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(
                    stderr.getvalue(), "papercuts: E_STORAGE_INDETERMINATE\n"
                )
                self.assertEqual(calls, 1)
                self.assertEqual(self._ledger(repo).read_bytes(), b"")
            finally:
                os.chdir(old_cwd)

    def test_mutation_output_failure_reports_committed_unacknowledged(self):
        with self._repo() as (repo, _env):
            cli = self._load_cli()
            old_cwd = os.getcwd()
            try:
                os.chdir(repo)
                stderr = io.StringIO()
                with mock.patch.object(cli.sys, "stdout", FailingStdout()), mock.patch.object(
                    cli.sys, "stderr", stderr
                ):
                    result = cli.main(list(self._record_args()))
                self.assertEqual(result, 1)
                self.assertEqual(stderr.getvalue(), "papercuts: E_OUTPUT_AFTER_COMMIT\n")
                self.assertEqual(len(self._ledger(repo).read_bytes().splitlines()), 1)
            finally:
                os.chdir(old_cwd)

    def test_read_output_failure_preserves_ledger(self):
        with self._repo() as (repo, env):
            self._record(repo, env)
            ledger = self._ledger(repo)
            before = ledger.read_bytes()
            cli = self._load_cli()
            old_cwd = os.getcwd()
            try:
                os.chdir(repo)
                stderr = io.StringIO()
                with mock.patch.object(cli.sys, "stdout", FailingStdout()), mock.patch.object(
                    cli.sys, "stderr", stderr
                ):
                    result = cli.main(["list"])
                self.assertEqual(result, 1)
                self.assertEqual(stderr.getvalue(), "papercuts: E_OUTPUT\n")
                self.assertEqual(ledger.read_bytes(), before)
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()

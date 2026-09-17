import errno
import fcntl
import json
import multiprocessing
import os
import pathlib
import socket
import stat
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

from papercuts.model import LedgerError
from papercuts import repo as repo_module
from papercuts.repo import (
    GitHead,
    RepoError,
    RepoPaths,
    StorageError,
    capture_source_snapshot,
    discover_repository,
    exclusive_lock,
    inspect_head,
    read_storage_snapshot,
    read_storage,
    shared_lock,
)
from papercuts.store import record


ROOT = pathlib.Path(__file__).resolve().parent
VALID = ROOT / "fixtures" / "valid-events.jsonl"


def _hold_exclusive_lock(path, connection):
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        connection.send("locked")
        if connection.recv() != "release":
            raise RuntimeError("unexpected lock-holder command")
        fcntl.flock(fd, fcntl.LOCK_UN)
        connection.send("released")
    finally:
        os.close(fd)
        connection.close()


class RepoDiscoveryTests(unittest.TestCase):
    def _result(self, stdout=b"/repo\n/repo/.git\n/repo/.git\n", **kwargs):
        return subprocess.CompletedProcess([], kwargs.pop("returncode", 0), stdout=stdout, stderr=kwargs.pop("stderr", b""), **kwargs)

    def test_discovery_uses_fixed_git_call_and_sanitized_environment(self):
        with mock.patch.dict(os.environ, {"PATH": "/safe", "SECRET": "must-not-pass"}, clear=True):
            with mock.patch("papercuts.repo.subprocess.run", return_value=self._result()) as run:
                result = discover_repository("/caller")
        self.assertEqual(result.top_level, "/repo")
        run.assert_called_once_with(
            ["git", "rev-parse", "--path-format=absolute", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"],
            cwd="/caller",
            env={"PATH": "/safe", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
            shell=False,
            capture_output=True,
            check=False,
        )

    def test_discovery_accepts_worktree_relation(self):
        output = b"/repo\n/repo/.git/worktrees/wt\n/repo/.git\n"
        with mock.patch("papercuts.repo.subprocess.run", return_value=self._result(output)):
            result = discover_repository("/caller")
        self.assertEqual(result.git_dir, "/repo/.git/worktrees/wt")

    def test_discovery_rejects_bad_process_output_without_disclosure(self):
        cases = [
            self._result(stdout=b"/repo\n/repo/.git\n"),
            self._result(stdout=b"/repo\n/repo/.git\n/repo/.git\nextra\n"),
            self._result(stdout=b"/repo\r\n/repo/.git\n/repo/.git\n"),
            self._result(stdout=b"/repo\n/repo/.git\nrelative\n"),
            self._result(stdout=b"/repo/a/../b\n/repo/.git\n/repo/.git\n"),
            self._result(stdout=b"/repo\n/repo/.git\n/repo/.git\x00\n"),
            self._result(stdout=b"/repo\n/repo/.git\n\xff\n"),
            self._result(stdout=b"/repo\n/repo/.git\n/repo/.git\n", stderr=b"secret path"),
        ]
        for result in cases:
            with self.subTest(result=result.stdout):
                with mock.patch("papercuts.repo.subprocess.run", return_value=result):
                    with self.assertRaises(RepoError) as raised:
                        discover_repository("/caller")
                self.assertEqual(raised.exception.rule_id, "E_GIT_DISCOVERY")
                self.assertNotIn("secret", str(raised.exception))

    def test_discovery_rejects_nonzero_and_wrong_relation(self):
        bad = [
            self._result(returncode=1),
            self._result(stdout=b"/repo\n/repo/.git/worktrees/wt\n/other/.git\n"),
            self._result(stdout=b"/repo\n/repo/.git/foo\n/repo/.git\n"),
        ]
        for result in bad:
            with mock.patch("papercuts.repo.subprocess.run", return_value=result):
                with self.assertRaisesRegex(RepoError, "E_GIT_DISCOVERY"):
                    discover_repository("/caller")

    def test_discovery_rejects_oversize_output_and_no_network_surface(self):
        result = self._result(stdout=b"/repo\n/repo/.git\n" + b"/repo/.git" + b"x" * 16384)
        with mock.patch("papercuts.repo.subprocess.run", return_value=result):
            with self.assertRaises(RepoError):
                discover_repository("/caller")
        with mock.patch("papercuts.repo.subprocess.run", return_value=self._result()):
            with mock.patch.object(socket, "socket", side_effect=AssertionError("network")):
                discover_repository("/caller")


class GitProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.paths = RepoPaths("/repo", "/repo/.git", "/repo/.git")

    def _result(self, stdout=b"", *, returncode=0, stderr=b""):
        return subprocess.CompletedProcess(
            [], returncode, stdout=stdout, stderr=stderr
        )

    def test_committed_head_uses_exact_bounded_call_and_closed_environment(self):
        oid = b"cf2fabd85c9bf73416772e33b3812a6c87074c49\n"
        with mock.patch.dict(
            os.environ, {"PATH": "/safe", "SECRET": "must-not-pass"}, clear=True
        ):
            with mock.patch(
                "papercuts.repo.subprocess.run",
                return_value=self._result(stdout=oid),
            ) as run:
                head = inspect_head(self.paths)
        self.assertEqual(
            head, GitHead("commit", "cf2fabd85c9bf73416772e33b3812a6c87074c49")
        )
        run.assert_called_once_with(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            cwd="/repo",
            env={
                "PATH": "/safe",
                "LC_ALL": "C",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
            shell=False,
            capture_output=True,
            check=False,
        )

    def test_unborn_head_is_explicit(self):
        with mock.patch(
            "papercuts.repo.subprocess.run",
            return_value=self._result(returncode=1),
        ):
            self.assertEqual(inspect_head(self.paths), GitHead("unborn", None))

    def test_head_rejects_malformed_or_unbounded_process_output(self):
        malformed = (
            self._result(stdout=b"ABCDEF\n"),
            self._result(stdout=b"f" * 39 + b"\n"),
            self._result(stdout=b"f" * 41 + b"\n"),
            self._result(stdout=b"f" * 40),
            self._result(stdout=b"f" * 40 + b"\r\n"),
            self._result(stdout=b"f" * 40 + b"\nextra\n"),
            self._result(stdout=b"f" * 65 + b"\n"),
            self._result(stdout=b"f" * 40 + b"\n", stderr=b"secret"),
            self._result(stdout=b"x", returncode=1),
            self._result(returncode=2),
        )
        for result in malformed:
            with self.subTest(stdout=result.stdout, returncode=result.returncode):
                with mock.patch(
                    "papercuts.repo.subprocess.run", return_value=result
                ):
                    with self.assertRaisesRegex(RepoError, "E_GIT_DISCOVERY"):
                        inspect_head(self.paths)

    def test_capture_compares_roots_worktree_kind_and_head_around_snapshot(self):
        linked = RepoPaths(
            "/repo", "/repo/.git/worktrees/linked", "/repo/.git"
        )
        head = GitHead("commit", "f" * 40)
        storage = repo_module.StorageSnapshot(b"ledger\n", ())
        calls = []

        def discover(cwd):
            calls.append(("discover", cwd))
            return linked

        def inspect(paths):
            calls.append(("head", paths))
            return head

        def read(common):
            calls.append(("storage", common))
            return storage

        with mock.patch.object(repo_module, "discover_repository", discover), mock.patch.object(
            repo_module, "inspect_head", inspect
        ), mock.patch.object(repo_module, "read_storage_snapshot", read):
            captured = capture_source_snapshot(linked)

        self.assertEqual(captured.paths, linked)
        self.assertEqual(captured.worktree_kind, "linked")
        self.assertEqual(captured.head, head)
        self.assertIs(captured.storage, storage)
        self.assertEqual(
            calls,
            [
                ("discover", "/repo"),
                ("head", linked),
                ("storage", "/repo/.git"),
                ("discover", "/repo"),
                ("head", linked),
            ],
        )

    def test_capture_rejects_wrong_initial_relation_and_root_or_head_drift(self):
        head = GitHead("commit", "f" * 40)
        changed_root = RepoPaths("/other", "/other/.git", "/other/.git")
        for discoveries, heads in (
            ([changed_root], [head]),
            ([self.paths, changed_root], [head, head]),
            ([self.paths, self.paths], [head, GitHead("unborn", None)]),
            (
                [self.paths, self.paths],
                [head, GitHead("commit", "e" * 40)],
            ),
        ):
            with self.subTest(discoveries=discoveries, heads=heads):
                with mock.patch.object(
                    repo_module, "discover_repository", side_effect=discoveries
                ), mock.patch.object(
                    repo_module, "inspect_head", side_effect=heads
                ), mock.patch.object(
                    repo_module,
                    "read_storage_snapshot",
                    return_value=repo_module.StorageSnapshot(b"", ()),
                ):
                    with self.assertRaisesRegex(
                        RepoError, "E_PACKET_SOURCE_CHANGED"
                    ):
                        capture_source_snapshot(self.paths)

    def test_capture_preserves_discovery_wrong_relation_failure(self):
        with mock.patch.object(
            repo_module,
            "discover_repository",
            side_effect=RepoError("E_GIT_DISCOVERY"),
        ):
            with self.assertRaisesRegex(RepoError, "E_GIT_DISCOVERY"):
                capture_source_snapshot(self.paths)

        wrong_relation = RepoPaths("/repo", "/repo/.git/not-worktrees/wt", "/repo/.git")
        with mock.patch.object(repo_module, "discover_repository") as discover:
            with self.assertRaisesRegex(RepoError, "E_GIT_DISCOVERY"):
                capture_source_snapshot(wrong_relation)
        discover.assert_not_called()

    def test_capture_uses_real_main_and_linked_worktrees_with_common_storage(self):
        def git(cwd, *args, input_bytes=None):
            env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "LC_ALL": "C",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": "/dev/null",
                "GIT_AUTHOR_NAME": "Papercuts Test",
                "GIT_AUTHOR_EMAIL": "papercuts-test@example.invalid",
                "GIT_COMMITTER_NAME": "Papercuts Test",
                "GIT_COMMITTER_EMAIL": "papercuts-test@example.invalid",
            }
            completed = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=env,
                input=input_bytes,
                shell=False,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode("utf-8", errors="replace"),
            )
            self.assertLessEqual(len(completed.stdout), 16 * 1024)
            self.assertLessEqual(len(completed.stderr), 16 * 1024)
            return completed.stdout

        with tempfile.TemporaryDirectory(
            prefix="papercuts-repo-capture-"
        ) as directory:
            root = pathlib.Path(directory)
            main = root / "main"
            linked = root / "linked"
            main.mkdir()
            git(main, "init", "--quiet", "--initial-branch=main")
            tree = git(
                main, "hash-object", "-t", "tree", "--stdin", input_bytes=b""
            ).decode("ascii").strip()
            oid = git(
                main, "commit-tree", tree, input_bytes=b"capture integration\n"
            ).decode("ascii").strip()
            git(main, "update-ref", "HEAD", oid)

            storage = main / ".git" / "papercuts"
            storage.mkdir(mode=0o700)
            os.chmod(storage, 0o700)
            lock = storage / "write.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
            ledger = storage / "events.jsonl"
            expected = VALID.read_bytes()
            ledger.write_bytes(expected)
            os.chmod(ledger, 0o600)

            git(main, "worktree", "add", "--quiet", "--detach", linked, oid)
            main_source = capture_source_snapshot(discover_repository(main))
            linked_source = capture_source_snapshot(discover_repository(linked))

            self.assertEqual(main_source.worktree_kind, "main")
            self.assertEqual(linked_source.worktree_kind, "linked")
            self.assertEqual(main_source.head, GitHead("commit", oid))
            self.assertEqual(linked_source.head, GitHead("commit", oid))
            self.assertEqual(main_source.storage.raw_bytes, expected)
            self.assertEqual(linked_source.storage.raw_bytes, expected)
            self.assertEqual(main_source.storage.events, linked_source.storage.events)
            self.assertEqual(ledger.read_bytes(), expected)


class StorageStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.common = pathlib.Path(self.temp.name) / "common"
        self.common.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def _papercuts(self, mode=0o700):
        path = self.common / "papercuts"
        path.mkdir(mode=mode, exist_ok=True)
        os.chmod(path, mode)
        return path

    def _lock(self, mode=0o600, content=b""):
        path = self._papercuts() / "write.lock"
        path.write_bytes(content)
        os.chmod(path, mode)
        return path

    def _ledger(self, data=None, mode=0o600):
        path = self._papercuts() / "events.jsonl"
        path.write_bytes(VALID.read_bytes() if data is None else data)
        os.chmod(path, mode)
        return path


    def test_absent_and_empty_are_valid_empty_and_reader_creates_nothing(self):
        before = set(self.common.iterdir())
        self.assertEqual(read_storage(self.common), [])
        self.assertEqual(set(self.common.iterdir()), before)
        self._papercuts()
        before = set(self.common.iterdir())
        self.assertEqual(read_storage(self.common), [])
        self.assertEqual(set(self.common.iterdir()), before)

    def test_snapshot_preserves_exact_bytes_and_immutable_event_sequence(self):
        self._lock()
        ledger = self._ledger()
        expected = ledger.read_bytes()
        snapshot = read_storage_snapshot(self.common)
        self.assertEqual(snapshot.raw_bytes, expected)
        self.assertEqual(ledger.read_bytes(), expected)
        self.assertEqual(len(snapshot.events), 3)
        self.assertIsInstance(snapshot.events, tuple)
        with self.assertRaises((AttributeError, TypeError)):
            snapshot.events = ()
        with self.assertRaises(TypeError):
            snapshot.events[0]["summary"] = "mutated"

        nested = repo_module.StorageSnapshot(
            b"",
            (
                {
                    "mapping": {
                        "sequence": [
                            {"value": "fixed"},
                        ]
                    }
                },
            ),
        )
        with self.assertRaises(TypeError):
            nested.events[0]["mapping"] = {}
        with self.assertRaises(TypeError):
            nested.events[0]["mapping"]["sequence"][0]["value"] = "mutated"
        with self.assertRaises(AttributeError):
            nested.events[0]["mapping"]["sequence"].append("mutated")

        with mock.patch.object(
            repo_module, "read_storage_snapshot", return_value=nested
        ):
            compatible = read_storage(self.common)
        self.assertIs(type(compatible), list)
        self.assertIs(type(compatible[0]), dict)
        self.assertIs(type(compatible[0]["mapping"]), dict)
        self.assertIs(type(compatible[0]["mapping"]["sequence"]), list)
        compatible[0]["mapping"]["sequence"][0]["value"] = "mutable"
        self.assertEqual(
            nested.events[0]["mapping"]["sequence"][0]["value"], "fixed"
        )

    def test_absent_empty_and_lock_only_snapshots_have_exact_empty_bytes(self):
        states = ("absent", "empty-directory", "lock-only", "empty-ledger")
        for state in states:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                common = pathlib.Path(directory) / "common"
                common.mkdir()
                if state != "absent":
                    storage = common / "papercuts"
                    storage.mkdir(mode=0o700)
                    os.chmod(storage, 0o700)
                    if state in {"lock-only", "empty-ledger"}:
                        lock = storage / "write.lock"
                        lock.write_bytes(b"")
                        os.chmod(lock, 0o600)
                    if state == "empty-ledger":
                        ledger = storage / "events.jsonl"
                        ledger.write_bytes(b"")
                        os.chmod(ledger, 0o600)
                snapshot = read_storage_snapshot(common)
                self.assertEqual(snapshot.raw_bytes, b"")
                self.assertEqual(snapshot.events, ())

    def test_snapshot_rejects_unsafe_and_tampered_fixed_entries(self):
        self._lock()
        ledger = self._ledger()
        os.link(ledger, ledger.with_name("events-alias"))
        with self.assertRaisesRegex(StorageError, "E_STORAGE_UNSAFE"):
            read_storage_snapshot(self.common)
        ledger.with_name("events-alias").unlink()

        real_read = repo_module._read_complete

        def tamper(fd, identity):
            os.chmod(ledger, 0o644)
            return real_read(fd, identity)

        with mock.patch.object(repo_module, "_read_complete", side_effect=tamper):
            with self.assertRaisesRegex(StorageError, "E_STORAGE_UNSAFE"):
                read_storage_snapshot(self.common)

    def test_lock_only_is_empty_and_ledger_only_fails_closed(self):
        self._lock()
        self.assertEqual(read_storage(self.common), [])
        (self.common / "papercuts" / "write.lock").unlink()
        self._ledger()
        with self.assertRaisesRegex(StorageError, "E_STORAGE_LEDGER_WITHOUT_LOCK"):
            read_storage(self.common)

    def test_lock_and_ledger_is_validated_under_shared_lock(self):
        self._lock()
        self._ledger()
        self.assertEqual(len(read_storage(self.common)), 3)

    def test_malformed_ledger_fails_without_mutation(self):
        self._lock()
        ledger = self._ledger(b"not-json\n")
        before = ledger.read_bytes(), stat.S_IMODE(ledger.stat().st_mode)
        with self.assertRaises(LedgerError):
            read_storage(self.common)
        self.assertEqual((ledger.read_bytes(), stat.S_IMODE(ledger.stat().st_mode)), before)

    def test_fixed_entries_wrong_mode_symlink_special_and_hardlink_fail_closed(self):
        self._papercuts(mode=0o755)
        with self.assertRaisesRegex(StorageError, "E_STORAGE_UNSAFE"):
            read_storage(self.common)
        (self.common / "papercuts").rmdir()
        self._lock()
        os.chmod(self.common / "papercuts" / "write.lock", 0o644)
        with self.assertRaisesRegex(StorageError, "E_STORAGE_UNSAFE"):
            read_storage(self.common)
        os.chmod(self.common / "papercuts" / "write.lock", 0o600)
        os.link(self.common / "papercuts" / "write.lock", self.common / "papercuts" / "lock-alias")
        with self.assertRaisesRegex(StorageError, "E_STORAGE_UNSAFE"):
            read_storage(self.common)
        (self.common / "papercuts" / "lock-alias").unlink()
        (self.common / "papercuts" / "write.lock").unlink()
        os.symlink(self.common / "target", self.common / "papercuts" / "write.lock")
        with self.assertRaisesRegex(StorageError, "E_STORAGE_UNSAFE"):
            read_storage(self.common)
        (self.common / "papercuts" / "write.lock").unlink()
        os.mkfifo(self.common / "papercuts" / "write.lock")
        with self.assertRaisesRegex(StorageError, "E_STORAGE_UNSAFE"):
            read_storage(self.common)

    def test_writer_creates_exact_scaffolding_but_never_ledger(self):
        with exclusive_lock(self.common):
            self.assertTrue((self.common / "papercuts" / "write.lock").is_file())
            self.assertFalse((self.common / "papercuts" / "events.jsonl").exists())
        self.assertEqual(stat.S_IMODE((self.common / "papercuts").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.common / "papercuts" / "write.lock").stat().st_mode), 0o600)

    def test_shared_and_exclusive_locks_are_nonblocking(self):
        self._lock()
        with shared_lock(self.common):
            with self.assertRaisesRegex(StorageError, "E_LOCK_BUSY"):
                with exclusive_lock(self.common):
                    pass
        with exclusive_lock(self.common):
            with self.assertRaisesRegex(StorageError, "E_LOCK_BUSY"):
                with shared_lock(self.common):
                    pass

    def test_reader_never_creates_storage_and_writer_does_not_create_ledger(self):
        self.assertFalse((self.common / "papercuts").exists())
        read_storage(self.common)
        self.assertFalse((self.common / "papercuts").exists())
        with exclusive_lock(self.common):
            pass
        self.assertFalse((self.common / "papercuts" / "events.jsonl").exists())


class AppendContentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.common = pathlib.Path(self.temp.name) / "common"
        self.common.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def _record(self, summary):
        return record(
            str(self.common),
            category="docs",
            summary=summary,
            expected="works",
            observed="blocked",
            evidence_basis="test",
        )

    def _start_lock_holder(self):
        lock_path = self.common / "papercuts" / "write.lock"
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=_hold_exclusive_lock, args=(lock_path, child))
        process.start()
        child.close()
        self.assertTrue(parent.poll(3), "lock holder did not become ready")
        self.assertEqual(parent.recv(), "locked")
        return process, parent

    def _stop_lock_holder(self, process, connection):
        if process.is_alive():
            connection.send("release")
            self.assertTrue(connection.poll(3), "lock holder did not release")
            self.assertEqual(connection.recv(), "released")
        process.join(3)
        if process.is_alive():
            process.terminate()
            process.join(3)
        connection.close()
        self.assertEqual(process.exitcode, 0)

    def test_busy_append_reopens_after_confirmed_release_and_appends_once(self):
        self._record("before")
        process, connection = self._start_lock_holder()
        monotonic_calls = []
        sleeper_calls = []
        released = False

        def monotonic():
            monotonic_calls.append(100.0)
            return 100.0

        def sleeper(delay):
            nonlocal released
            sleeper_calls.append(delay)
            self.assertFalse(released)
            released = True
            connection.send("release")
            self.assertTrue(connection.poll(3), "lock holder did not confirm release")
            self.assertEqual(connection.recv(), "released")
            process.join(3)
            self.assertFalse(process.is_alive())

        try:
            with mock.patch.object(repo_module, "_monotonic", monotonic, create=True), mock.patch.object(
                repo_module, "_sleep", sleeper, create=True
            ), mock.patch.object(
                repo_module,
                "_open_common_and_storage",
                wraps=repo_module._open_common_and_storage,
            ) as opened:
                self._record("after")
            self.assertEqual(opened.call_count, 2)
            self.assertEqual(monotonic_calls, [100.0, 100.0])
            self.assertEqual(sleeper_calls, [0.010])
            events = read_storage(self.common)
            self.assertEqual([event["summary"] for event in events], ["before", "after"])
        finally:
            if process.is_alive():
                self._stop_lock_holder(process, connection)
            else:
                connection.close()

    def test_busy_append_uses_one_deadline_and_preserves_ledger_at_expiry(self):
        self._record("before")
        ledger = self.common / "papercuts" / "events.jsonl"
        before = ledger.read_bytes()
        process, connection = self._start_lock_holder()

        class Clock:
            def __init__(self):
                self.now = 100.0
                self.calls = []
                self.sleeps = []
                self.advances = iter((0.75, 1.249, 0.001))

            def monotonic(self):
                self.calls.append(self.now)
                return self.now

            def sleep(self, delay):
                remaining = 102.0 - self.now
                self.sleeps.append((delay, remaining))
                self.assert_delay(delay, remaining)
                self.now += next(self.advances)

            @staticmethod
            def assert_delay(delay, remaining):
                if not (0 < delay <= 0.010 and delay <= remaining):
                    raise AssertionError((delay, remaining))

        clock = Clock()
        try:
            with mock.patch.object(repo_module, "_monotonic", clock.monotonic, create=True), mock.patch.object(
                repo_module, "_sleep", clock.sleep, create=True
            ), mock.patch.object(
                repo_module,
                "_open_common_and_storage",
                wraps=repo_module._open_common_and_storage,
            ) as opened:
                with self.assertRaisesRegex(StorageError, r"^papercuts: E_LOCK_BUSY$"):
                    self._record("blocked")
            self.assertEqual(opened.call_count, 4)
            self.assertEqual(clock.calls, [100.0, 100.0, 100.75, 101.999, 102.0])
            self.assertEqual([item[0] for item in clock.sleeps[:2]], [0.010, 0.010])
            self.assertAlmostEqual(clock.sleeps[2][0], 0.001, places=9)
            self.assertEqual(ledger.read_bytes(), before)
        finally:
            self._stop_lock_holder(process, connection)

    def test_waiter_accepts_valid_first_ledger_created_before_lock_acquisition(self):
        storage = self.common / "papercuts"
        storage.mkdir(mode=0o700)
        lock_path = storage / "write.lock"
        lock_path.touch(mode=0o600)
        lock_identity = (lock_path.stat().st_dev, lock_path.stat().st_ino)
        real_lock = repo_module._lock
        winner_recorded = False

        def winner_then_lock(fd, operation):
            nonlocal winner_recorded
            if not winner_recorded:
                winner_recorded = True
                self._record("winner")
            return real_lock(fd, operation)

        try:
            with mock.patch.object(repo_module, "_lock", side_effect=winner_then_lock):
                self._record("waiter")
        except StorageError as exc:
            summaries = ",".join(event["summary"] for event in read_storage(self.common))
            self.fail(f"waiter_error={exc.rule_id} events={summaries}")

        self.assertTrue(winner_recorded)
        self.assertEqual((lock_path.stat().st_dev, lock_path.stat().st_ino), lock_identity)
        events = read_storage(self.common)
        self.assertEqual([event["summary"] for event in events], ["winner", "waiter"])

    def test_valid_first_use_eexist_winners_are_reopened_and_validated(self):
        real_mkdir = os.mkdir
        raced = False

        def directory_winner(path, mode=0o777, *, dir_fd=None):
            nonlocal raced
            if path == "papercuts" and dir_fd is not None and not raced:
                raced = True
                real_mkdir(path, mode, dir_fd=dir_fd)
                raise FileExistsError(errno.EEXIST, "winner")
            return real_mkdir(path, mode, dir_fd=dir_fd)

        with mock.patch.object(repo_module.os, "mkdir", side_effect=directory_winner):
            self._record("directory winner")
        self.assertTrue(raced)

        with tempfile.TemporaryDirectory() as directory:
            self.common = pathlib.Path(directory) / "common"
            self.common.mkdir()
            (self.common / "papercuts").mkdir(mode=0o700)
            real_open = os.open
            raced = False

            def lock_winner(path, flags, *args, **kwargs):
                nonlocal raced
                if path == "write.lock" and flags & os.O_EXCL and not raced:
                    raced = True
                    fd = real_open(path, flags, *args, **kwargs)
                    os.close(fd)
                    raise FileExistsError(errno.EEXIST, "winner")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(repo_module.os, "open", side_effect=lock_winner):
                self._record("lock winner")
            self.assertTrue(raced)

        with tempfile.TemporaryDirectory() as directory:
            self.common = pathlib.Path(directory) / "common"
            self.common.mkdir()
            storage = self.common / "papercuts"
            storage.mkdir(mode=0o700)
            (storage / "write.lock").touch(mode=0o600)
            real_open = os.open
            raced = False

            def ledger_winner(path, flags, *args, **kwargs):
                nonlocal raced
                if path == "events.jsonl" and flags & os.O_EXCL and not raced:
                    raced = True
                    fd = real_open(path, flags, *args, **kwargs)
                    os.close(fd)
                    raise FileExistsError(errno.EEXIST, "winner")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(repo_module.os, "open", side_effect=ledger_winner):
                self._record("ledger winner")
            self.assertTrue(raced)
            self.assertEqual(len(read_storage(self.common)), 1)

    def test_invalid_first_use_winners_fail_closed_without_retry(self):
        def run_directory_variant(kind):
            with tempfile.TemporaryDirectory() as directory:
                common = pathlib.Path(directory) / "common"
                common.mkdir()
                real_mkdir = os.mkdir

                def invalid_winner(path, mode=0o777, *, dir_fd=None):
                    if path == "papercuts" and dir_fd is not None:
                        if kind == "wrong-mode":
                            real_mkdir(path, 0o755, dir_fd=dir_fd)
                            os.chmod(common / "papercuts", 0o755)
                        else:
                            os.symlink(common / "target", path, dir_fd=dir_fd)
                        raise FileExistsError(errno.EEXIST, "invalid winner")
                    return real_mkdir(path, mode, dir_fd=dir_fd)

                sleeper = mock.Mock()
                with mock.patch.object(repo_module.os, "mkdir", side_effect=invalid_winner), mock.patch.object(
                    repo_module, "_sleep", sleeper, create=True
                ):
                    with self.assertRaisesRegex(StorageError, "E_STORAGE_UNSAFE"):
                        record(str(common), category="docs", summary="x", expected="y", observed="z", evidence_basis="test")
                sleeper.assert_not_called()

        for kind in ("wrong-mode", "symlink"):
            with self.subTest(entry="papercuts", kind=kind):
                run_directory_variant(kind)

        for entry in ("write.lock", "events.jsonl"):
            for kind in ("wrong-mode", "hardlink", "non-regular"):
                with self.subTest(entry=entry, kind=kind), tempfile.TemporaryDirectory() as directory:
                    common = pathlib.Path(directory) / "common"
                    common.mkdir()
                    storage = common / "papercuts"
                    storage.mkdir(mode=0o700)
                    if entry == "events.jsonl":
                        (storage / "write.lock").touch(mode=0o600)
                    real_open = os.open
                    fired = False

                    def invalid_winner(path, flags, *args, **kwargs):
                        nonlocal fired
                        if path == entry and flags & os.O_EXCL and not fired:
                            fired = True
                            if kind == "non-regular":
                                os.mkdir(storage / entry)
                            else:
                                fd = real_open(path, flags, *args, **kwargs)
                                os.close(fd)
                                if kind == "wrong-mode":
                                    os.chmod(storage / entry, 0o644)
                                else:
                                    os.link(storage / entry, storage / f"{entry}.alias")
                            raise FileExistsError(errno.EEXIST, "invalid winner")
                        return real_open(path, flags, *args, **kwargs)

                    sleeper = mock.Mock()
                    with mock.patch.object(repo_module.os, "open", side_effect=invalid_winner), mock.patch.object(
                        repo_module, "_sleep", sleeper, create=True
                    ):
                        with self.assertRaisesRegex(StorageError, "E_STORAGE_UNSAFE"):
                            record(str(common), category="docs", summary="x", expected="y", observed="z", evidence_basis="test")
                    self.assertTrue(fired)
                    sleeper.assert_not_called()

    def test_append_retries_neither_validation_storage_nor_post_mutation_failures(self):
        cases = ("validation", "storage", "post-mutation")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                common = pathlib.Path(directory) / "common"
                common.mkdir()
                storage = common / "papercuts"
                storage.mkdir(mode=0o700)
                (storage / "write.lock").touch(mode=0o600)
                sleeper = mock.Mock()
                with mock.patch.object(repo_module, "_sleep", sleeper, create=True):
                    if case == "validation":
                        ledger = storage / "events.jsonl"
                        ledger.write_bytes(b"not-json\n")
                        ledger.chmod(0o600)
                        with self.assertRaises(LedgerError):
                            record(str(common), category="docs", summary="x", expected="y", observed="z", evidence_basis="test")
                    elif case == "storage":
                        os.chmod(storage / "write.lock", 0o644)
                        with self.assertRaisesRegex(StorageError, "E_STORAGE_UNSAFE"):
                            record(str(common), category="docs", summary="x", expected="y", observed="z", evidence_basis="test")
                    else:
                        with mock.patch.object(repo_module.os, "write", side_effect=OSError("injected")) as write:
                            with self.assertRaisesRegex(StorageError, "E_STORAGE_INDETERMINATE"):
                                record(str(common), category="docs", summary="x", expected="y", observed="z", evidence_basis="test")
                        write.assert_called_once()
                sleeper.assert_not_called()

    def test_append_does_not_retry_busy_rule_from_decision_callback(self):
        line = VALID.read_bytes().splitlines(keepends=True)[0]
        decide = mock.Mock(side_effect=StorageError("E_LOCK_BUSY"))
        sleeper = mock.Mock(side_effect=AssertionError("non-lock busy was retried"))
        with mock.patch.object(repo_module, "_monotonic", return_value=100.0), mock.patch.object(
            repo_module, "_sleep", sleeper
        ):
            with self.assertRaisesRegex(StorageError, r"^papercuts: E_LOCK_BUSY$"):
                repo_module.append_event(self.common, line, decide)
        decide.assert_called_once_with([])
        sleeper.assert_not_called()

    def test_read_and_public_lock_contexts_remain_one_shot(self):
        self._record("before")
        lock_fd = os.open(self.common / "papercuts" / "write.lock", os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sleeper = mock.Mock(side_effect=AssertionError("one-shot API attempted retry"))
        try:
            with mock.patch.object(repo_module, "_sleep", sleeper, create=True):
                operations = (
                    lambda: read_storage(self.common),
                    lambda: shared_lock(self.common).__enter__(),
                    lambda: exclusive_lock(self.common).__enter__(),
                )
                for operation in operations:
                    with self.subTest(operation=operation), self.assertRaisesRegex(
                        StorageError, "E_LOCK_BUSY"
                    ):
                        operation()
            sleeper.assert_not_called()
        finally:
            os.close(lock_fd)


if __name__ == "__main__":
    unittest.main()

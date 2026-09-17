import collections
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from papercuts.model import parse_ledger
from papercuts.packet import build_packet
from papercuts.repo import discover_repository
from papercuts.store import record


SOURCE_ROOT = (Path(__file__).resolve().parents[1] / "src").resolve()
WORKERS = 4
REPETITIONS = 3
JOIN_BOUND = 8.0
FORBIDDEN_ERRORS = (
    "E_LOCK_BUSY",
    "E_STORAGE",
    "E_STORAGE_UNSAFE",
    "E_STORAGE_INDETERMINATE",
    "E_OUTPUT",
    "E_OUTPUT_AFTER_COMMIT",
)


def _cli_worker(index, cwd, operation, argv, barrier, connection):
    started = time.monotonic()
    try:
        barrier.wait(timeout=5)
        env = dict(os.environ)
        env.update(
            {
                "LC_ALL": "C",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(SOURCE_ROOT),
            }
        )
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "papercuts", *argv],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        result = {
            "index": index,
            "pid": os.getpid(),
            "operation": operation,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "elapsed": time.monotonic() - started,
        }
    except BaseException as exc:
        result = {
            "index": index,
            "pid": os.getpid(),
            "operation": operation,
            "returncode": -1,
            "stdout": "",
            "stderr": f"worker exception: {type(exc).__name__}: {exc}",
            "elapsed": time.monotonic() - started,
        }
    try:
        connection.send(result)
    finally:
        connection.close()


def _packet_snapshot_worker(paths, locked, continue_read, connection):
    import fcntl
    import papercuts.repo as repo_module
    from papercuts.packet import build_packet as worker_build_packet

    real_lock = repo_module._lock

    def gated_lock(fd, operation):
        result = real_lock(fd, operation)
        if operation == fcntl.LOCK_SH:
            locked.set()
            if not continue_read.wait(timeout=JOIN_BOUND):
                raise RuntimeError("packet snapshot release timeout")
        return result

    repo_module._lock = gated_lock
    try:
        connection.send(worker_build_packet(paths, expected_head="unborn"))
    finally:
        connection.close()


def _record_after_snapshot_worker(common, busy, retrying, release, connection):
    import papercuts.repo as repo_module
    from papercuts.repo import StorageError
    from papercuts.store import record as worker_record

    real_lock = repo_module._lock

    def observed_lock(fd, operation):
        try:
            return real_lock(fd, operation)
        except StorageError as exc:
            if exc.rule_id == "E_LOCK_BUSY":
                busy.set()
            raise

    def wait_for_release(_duration):
        retrying.set()
        if not release.wait(timeout=JOIN_BOUND):
            raise RuntimeError("writer retry release timeout")

    repo_module._lock = observed_lock
    repo_module._sleep = wait_for_release
    try:
        connection.send(
            worker_record(
                common,
                category="tooling",
                summary="waiting writer",
                expected="append after snapshot",
                observed="append completed",
                evidence_basis="test",
            )
        )
    finally:
        connection.close()


class MultiprocessContentionTests(unittest.TestCase):
    maxDiff = None

    def _git_env(self):
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
        return env

    def _git(self, cwd, *args, env=None):
        if env is None:
            env = self._git_env()
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

    def _new_repository(self, root):
        repo = root / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        return repo

    def _linked_worktrees(self, root):
        repo = self._new_repository(root)
        self._git(repo, "commit", "--allow-empty", "-q", "-m", "test root")
        worktrees = []
        for index in range(WORKERS):
            path = root / f"worktree-{index}"
            self._git(repo, "worktree", "add", "-q", "-b", f"contention-{index}", path)
            worktrees.append(path)
        commons = {Path(discover_repository(path).common_dir) for path in worktrees}
        self.assertEqual(commons, {Path(discover_repository(repo).common_dir)})
        return worktrees

    def _run_parallel(self, cwds, operation, argvs):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(WORKERS)
        processes = []
        parents = []
        for index in range(WORKERS):
            parent, child = context.Pipe(duplex=False)
            process = context.Process(
                target=_cli_worker,
                args=(index, str(cwds[index]), operation, argvs[index], barrier, child),
            )
            process.start()
            child.close()
            processes.append(process)
            parents.append(parent)

        deadline = time.monotonic() + JOIN_BOUND
        for process in processes:
            process.join(max(0.0, deadline - time.monotonic()))
        alive = [process for process in processes if process.is_alive()]
        if alive:
            for process in alive:
                process.terminate()
            for process in alive:
                process.join(2)

        results = []
        for index, parent in enumerate(parents):
            if parent.poll():
                results.append(parent.recv())
            else:
                results.append(
                    {
                        "index": index,
                        "pid": processes[index].pid,
                        "operation": operation,
                        "returncode": -1,
                        "stdout": "",
                        "stderr": "worker exceeded join bound or returned no result",
                        "elapsed": JOIN_BOUND,
                    }
                )
            parent.close()
        self.assertFalse(alive, json.dumps(results, sort_keys=True, indent=2))
        self.assertTrue(all(process.exitcode == 0 for process in processes), json.dumps(results, sort_keys=True, indent=2))
        return sorted(results, key=lambda item: item["index"])

    def _record_args(self, scenario, repetition, index):
        return (
            "record",
            "--category",
            "docs",
            "--summary",
            f"{scenario}-{repetition}-{index}",
            "--expected",
            "serialized append",
            "--observed",
            "concurrent operation",
            "--evidence-basis",
            "test",
        )

    def _assert_no_ordinary_failures(self, results):
        rendered = json.dumps(results, sort_keys=True)
        for rule_id in FORBIDDEN_ERRORS:
            self.assertNotIn(rule_id, rendered, rendered)

    def _start_worker(self, context, target, *args):
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=target, args=(*args, child))
        process.start()
        child.close()
        return process, parent

    def _finish_worker(self, process, connection):
        process.join(JOIN_BOUND)
        if process.is_alive():
            process.terminate()
            process.join(2)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 0)
        self.assertTrue(connection.poll(1), "worker returned no result")
        result = connection.recv()
        connection.close()
        return result

    def _assert_packet_view(self, packet, raw, events, record_ids):
        self.assertEqual(
            [item["event"]["event_id"] for item in packet["events"]],
            [event["event_id"] for event in events],
        )
        self.assertEqual(
            [item["record"]["record_id"] for item in packet["records"]],
            record_ids,
        )
        last_event = events[-1]
        self.assertEqual(
            packet["read_watermark"],
            {
                "byte_length": len(raw),
                "event_count": len(events),
                "record_count": len(record_ids),
                "ledger_sha256": hashlib.sha256(raw).hexdigest(),
                "last_event_id": last_event["event_id"],
                "last_occurred_at": last_event["occurred_at"],
            },
        )

    def _exercise_repetition(self, scenario, root, repetition, linked):
        if linked:
            cwds = self._linked_worktrees(root)
            common = Path(discover_repository(cwds[0]).common_dir)
        else:
            repo = self._new_repository(root)
            cwds = [repo] * WORKERS
            common = Path(discover_repository(repo).common_dir)

        env = dict(os.environ)
        env.update(
            {
                "LC_ALL": "C",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(SOURCE_ROOT),
            }
        )
        if scenario == "existing":
            seeded = subprocess.run(
                [sys.executable, "-B", "-m", "papercuts", *self._record_args(scenario, repetition, 99)],
                cwd=cwds[0],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual((seeded.returncode, seeded.stderr), (0, ""))

        ledger = common / "papercuts" / "events.jsonl"
        baseline_events = parse_ledger(ledger.read_bytes()) if ledger.exists() else []
        record_results = self._run_parallel(
            cwds,
            "record",
            [self._record_args(scenario, repetition, index) for index in range(WORKERS)],
        )
        self.assertEqual([item["returncode"] for item in record_results], [0] * WORKERS, json.dumps(record_results, sort_keys=True, indent=2))
        self._assert_no_ordinary_failures(record_results)
        record_ids = [item["stdout"].strip() for item in record_results]
        self.assertEqual(len(set(record_ids)), WORKERS)
        for record_id in record_ids:
            self.assertEqual(str(uuid.UUID(record_id)), record_id)

        after_records = parse_ledger(ledger.read_bytes())
        self.assertEqual(len(after_records), len(baseline_events) + WORKERS)
        appended_records = after_records[len(baseline_events) :]
        self.assertEqual(collections.Counter(event["record_id"] for event in appended_records), collections.Counter(record_ids))

        target = record_ids[0]
        resolve_results = self._run_parallel(
            cwds,
            "resolve",
            [("resolve", target, "--resolution", f"fixed-{index}") for index in range(WORKERS)],
        )
        outcomes = collections.Counter(item["returncode"] for item in resolve_results)
        self.assertEqual(outcomes, collections.Counter({2: 3, 0: 1}), json.dumps(resolve_results, sort_keys=True, indent=2))
        duplicate = "papercuts: E_DUPLICATE_RESOLUTION field=record_id\n"
        self.assertEqual(sum(item["stderr"] == duplicate and item["stdout"] == "" for item in resolve_results), 3, json.dumps(resolve_results, sort_keys=True, indent=2))
        successes = [item for item in resolve_results if item["returncode"] == 0]
        self.assertEqual(successes[0]["stdout"], f"resolved {target}\n")
        self.assertEqual(successes[0]["stderr"], "")
        self._assert_no_ordinary_failures(resolve_results)

        final_bytes = ledger.read_bytes()
        self.assertTrue(final_bytes.endswith(b"\n"))
        final_events = parse_ledger(final_bytes)
        self.assertEqual(len(final_events), len(baseline_events) + WORKERS + 1)
        self.assertEqual(len({event["event_id"] for event in final_events}), len(final_events))
        recorded_ids = [event["record_id"] for event in final_events if event["event_type"] == "recorded"]
        self.assertEqual(len(recorded_ids), len(set(recorded_ids)))
        resolution_counts = collections.Counter(
            event["record_id"] for event in final_events if event["event_type"] == "resolved"
        )
        self.assertTrue(all(count <= 1 for count in resolution_counts.values()))
        self.assertEqual(resolution_counts[target], 1)

        if linked:
            commons = {Path(discover_repository(cwd).common_dir) for cwd in cwds}
            self.assertEqual(commons, {common})
            self.assertEqual({common / "papercuts" / "events.jsonl"}, {ledger})

    def _exercise_scenario(self, scenario, linked=False):
        for repetition in range(REPETITIONS):
            with self.subTest(scenario=scenario, repetition=repetition), tempfile.TemporaryDirectory() as directory:
                self._exercise_repetition(scenario, Path(directory), repetition, linked)

    def test_initially_absent_storage_concurrent_records_and_resolves(self):
        self._exercise_scenario("absent")

    def test_existing_valid_ledger_concurrent_records_and_resolves(self):
        self._exercise_scenario("existing")

    def test_linked_worktrees_share_one_ledger_under_contention(self):
        self._exercise_scenario("linked", linked=True)

    def test_packet_snapshot_excludes_append_until_a_later_snapshot(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            repo = self._new_repository(Path(directory))
            paths = discover_repository(repo)
            common = Path(paths.common_dir)
            baseline_id = record(
                str(common),
                category="tooling",
                summary="baseline",
                expected="baseline",
                observed="baseline",
                evidence_basis="test",
            )
            ledger = common / "papercuts" / "events.jsonl"
            baseline_bytes = ledger.read_bytes()
            baseline_events = parse_ledger(baseline_bytes)

            internal_snapshot_locked = context.Event()
            continue_snapshot = context.Event()
            writer_busy = context.Event()
            writer_retrying = context.Event()
            release_writer = context.Event()
            processes = []
            connections = []
            try:
                packet_process, packet_parent = self._start_worker(
                    context,
                    _packet_snapshot_worker,
                    paths,
                    internal_snapshot_locked,
                    continue_snapshot,
                )
                processes.append(packet_process)
                connections.append(packet_parent)
                self.assertTrue(
                    internal_snapshot_locked.wait(timeout=JOIN_BOUND),
                    "packet did not acquire its internal snapshot lock",
                )

                writer_process, writer_parent = self._start_worker(
                    context,
                    _record_after_snapshot_worker,
                    str(common),
                    writer_busy,
                    writer_retrying,
                    release_writer,
                )
                processes.append(writer_process)
                connections.append(writer_parent)
                self.assertTrue(
                    writer_busy.wait(timeout=JOIN_BOUND),
                    "writer did not observe E_LOCK_BUSY",
                )
                self.assertTrue(
                    writer_retrying.wait(timeout=JOIN_BOUND),
                    "writer did not enter append retry",
                )

                continue_snapshot.set()
                packet = json.loads(
                    self._finish_worker(packet_process, packet_parent)
                )
                self._assert_packet_view(
                    packet, baseline_bytes, baseline_events, [baseline_id]
                )
                self.assertFalse(writer_parent.poll(0))

                release_writer.set()
                writer_id = self._finish_worker(writer_process, writer_parent)
            finally:
                continue_snapshot.set()
                release_writer.set()
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(2)
                for connection in connections:
                    try:
                        connection.close()
                    except OSError:
                        pass

            self.assertEqual(str(uuid.UUID(writer_id)), writer_id)
            final_bytes = ledger.read_bytes()
            final_events = parse_ledger(final_bytes)
            self.assertEqual(len(final_events), 2)
            self.assertEqual(
                {event["record_id"] for event in final_events},
                {baseline_id, writer_id},
            )
            later = json.loads(build_packet(paths, expected_head="unborn"))
            self._assert_packet_view(
                later, final_bytes, final_events, [baseline_id, writer_id]
            )


if __name__ == "__main__":
    unittest.main()

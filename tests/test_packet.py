from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
import subprocess
import tempfile
import unittest

from papercuts.repo import discover_repository


try:
    from papercuts.packet import PacketError, build_packet
except ModuleNotFoundError as exc:
    if exc.name != "papercuts.packet":
        raise
    _PACKET_IMPORT_ERROR = exc
except ImportError as exc:
    _PACKET_IMPORT_ERROR = exc
else:
    _PACKET_IMPORT_ERROR = None


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
VALID = FIXTURES / "valid-events.jsonl"
EXPECTED = FIXTURES / "expected-packet.json"
VALID_BYTES = VALID.read_bytes()
EXPECTED_BYTES = EXPECTED.read_bytes()
EXPECTED_PACKET = json.loads(EXPECTED_BYTES)

COMMITTED_HEAD = "cf2fabd85c9bf73416772e33b3812a6c87074c49"
EVENT_1 = "00000000-0000-4000-8000-000000000001"
EVENT_2 = "00000000-0000-4000-8000-000000000002"
EVENT_3 = "00000000-0000-4000-8000-000000000003"
RECORD_1 = "00000000-0000-4000-8000-000000000101"
RECORD_2 = "00000000-0000-4000-8000-000000000102"

_MISSING = object()
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_TOP_LEVEL_KEYS = {
    "events",
    "packet_kind",
    "packet_schema_version",
    "read_watermark",
    "records",
    "selection",
    "source_repository",
}
_SOURCE_KEYS = {
    "head_commit",
    "head_state",
    "relative_ledger_path",
    "repository_scope",
    "worktree_kind",
}
_WATERMARK_KEYS = {
    "byte_length",
    "event_count",
    "last_event_id",
    "last_occurred_at",
    "ledger_sha256",
    "record_count",
}
_SELECTION_KEYS = {
    "requested_event_ids",
    "requested_record_ids",
    "scope_kind",
}
_EVENT_ENVELOPE_KEYS = {"event", "provenance"}
_EVENT_PROVENANCE_KEYS = {"relative_ledger_path", "source_line"}
_RECORD_ENVELOPE_KEYS = {"record", "provenance"}
_RECORD_KEYS = {
    "category",
    "evidence_basis",
    "expected",
    "observed",
    "record_id",
    "recurrence_count",
    "recurrence_key",
    "resolution",
    "status",
    "summary",
}
_RECORD_PROVENANCE_KEYS = {
    "recorded_event_id",
    "recorded_source_line",
    "resolved_event_id",
    "resolved_source_line",
}
_COMMON_EVENT_KEYS = {
    "event_id",
    "event_type",
    "occurred_at",
    "record_id",
    "schema_version",
}
_RECORDED_EVENT_KEYS = {
    *_COMMON_EVENT_KEYS,
    "category",
    "evidence_basis",
    "expected",
    "observed",
    "summary",
}
_RESOLVED_EVENT_KEYS = {*_COMMON_EVENT_KEYS, "resolution"}


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _uuid(number: int) -> str:
    if not 0 <= number <= 999_999_999_999:
        raise ValueError(number)
    return f"00000000-0000-4000-8000-{number:012d}"


def _canonical_ledger(events: list[dict]) -> bytes:
    return b"".join(_canonical_json(event) for event in events)


def _recorded_events(
    count: int,
    *,
    start: int = 0,
    text: str = "synthetic record",
    summary_text: str | object = _MISSING,
    expected_text: str | object = _MISSING,
    observed_text: str | object = _MISSING,
    recurrence_key: str | None | object = _MISSING,
) -> list[dict]:
    events = []
    for offset in range(count):
        event = {
            "schema_version": 1,
            "event_id": _uuid(1000 + start + offset),
            "event_type": "recorded",
            "occurred_at": f"2026-02-01T00:00:{offset:02d}.000000Z",
            "record_id": _uuid(2000 + start + offset),
            "category": "tooling",
            "summary": text if summary_text is _MISSING else summary_text,
            "expected": text if expected_text is _MISSING else expected_text,
            "observed": text if observed_text is _MISSING else observed_text,
            "evidence_basis": "test",
        }
        if recurrence_key is not _MISSING:
            event["recurrence_key"] = recurrence_key
        events.append(event)
    return events


def _complete_lifecycle_events(count: int) -> list[dict]:
    recorded = _recorded_events(count)
    resolved = [
        {
            "schema_version": 1,
            "event_id": _uuid(3000 + offset),
            "event_type": "resolved",
            "occurred_at": f"2026-02-02T00:00:{offset:02d}.000000Z",
            "record_id": event["record_id"],
            "resolution": "synthetic resolution",
        }
        for offset, event in enumerate(recorded)
    ]
    return recorded + resolved


def _open_records_packet_size_probe(events: list[dict], ledger: bytes) -> bytes:
    records = [
        {
            "record": {
                "category": event["category"],
                "evidence_basis": event["evidence_basis"],
                "expected": event["expected"],
                "observed": event["observed"],
                "record_id": event["record_id"],
                "recurrence_count": len(events),
                "recurrence_key": event.get("recurrence_key"),
                "resolution": None,
                "status": "open",
                "summary": event["summary"],
            },
            "provenance": {
                "recorded_event_id": event["event_id"],
                "recorded_source_line": line_number,
                "resolved_event_id": None,
                "resolved_source_line": None,
            },
        }
        for line_number, event in enumerate(events, 1)
    ]
    packet = {
        "events": [
            {
                "event": event,
                "provenance": {
                    "relative_ledger_path": "papercuts/events.jsonl",
                    "source_line": line_number,
                },
            }
            for line_number, event in enumerate(events, 1)
        ],
        "packet_kind": "evidence_packet",
        "packet_schema_version": 1,
        "read_watermark": {
            "byte_length": len(ledger),
            "event_count": len(events),
            "last_event_id": events[-1]["event_id"],
            "last_occurred_at": events[-1]["occurred_at"],
            "ledger_sha256": hashlib.sha256(ledger).hexdigest(),
            "record_count": len(records),
        },
        "records": records,
        "selection": {
            "requested_event_ids": [],
            "requested_record_ids": [],
            "scope_kind": "all_records",
        },
        "source_repository": {
            "head_commit": None,
            "head_state": "unborn",
            "relative_ledger_path": "papercuts/events.jsonl",
            "repository_scope": "git_common_dir",
            "worktree_kind": "main",
        },
    }
    return _canonical_json(packet)


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Papercuts Test",
        "GIT_AUTHOR_EMAIL": "papercuts-test@example.invalid",
        "GIT_COMMITTER_NAME": "Papercuts Test",
        "GIT_COMMITTER_EMAIL": "papercuts-test@example.invalid",
    }


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        env=_git_env(),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
    return completed


def _install_fixed_commit(root: Path) -> None:
    commit = (
        f"tree {_EMPTY_TREE}\n"
        "author Papercuts Test <papercuts-test@example.invalid> 0 +0000\n"
        "committer Papercuts Test <papercuts-test@example.invalid> 0 +0000\n"
        "\n"
        "synthetic committed HEAD\n"
    ).encode("ascii")
    actual = _git(
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
    _git(root, "update-ref", "refs/heads/main", COMMITTED_HEAD)


@contextmanager
def _source(raw: bytes | object = _MISSING, *, committed: bool):
    with tempfile.TemporaryDirectory(prefix="papercuts-packet-test-") as directory:
        root = Path(directory)
        _git(root, "init", "--quiet", "--initial-branch=main")
        if committed:
            _install_fixed_commit(root)
        if raw is not _MISSING:
            storage = root / ".git" / "papercuts"
            storage.mkdir(mode=0o700)
            os.chmod(storage, 0o700)
            lock = storage / "write.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
            ledger = storage / "events.jsonl"
            ledger.write_bytes(raw)  # type: ignore[arg-type]
            os.chmod(ledger, 0o600)
        yield discover_repository(root)


class PacketImportTests(unittest.TestCase):
    def test_packet_api_imports(self):
        if _PACKET_IMPORT_ERROR is not None:
            raise _PACKET_IMPORT_ERROR


class _PacketTestAssertions:
    def _decode(self, text: str) -> dict:
        self.assertIsInstance(text, str)
        raw = text.encode("utf-8")
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw, _canonical_json(json.loads(raw)))
        return json.loads(raw)

    def _assert_v1_packet_shape(
        self,
        packet: dict,
        *,
        expected_head: str,
        record_ids: tuple[str, ...],
        event_ids: tuple[str, ...],
    ) -> None:
        self.assertEqual(set(packet), _TOP_LEVEL_KEYS)
        self.assertIs(type(packet["packet_schema_version"]), int)
        self.assertEqual(packet["packet_schema_version"], 1)
        self.assertEqual(packet["packet_kind"], "evidence_packet")

        source = packet["source_repository"]
        self.assertEqual(set(source), _SOURCE_KEYS)
        self.assertEqual(source["repository_scope"], "git_common_dir")
        self.assertEqual(source["relative_ledger_path"], "papercuts/events.jsonl")
        self.assertEqual(source["worktree_kind"], "main")
        if expected_head == "unborn":
            self.assertEqual(source["head_state"], "unborn")
            self.assertIsNone(source["head_commit"])
        else:
            self.assertEqual(source["head_state"], "commit")
            self.assertEqual(source["head_commit"], expected_head)

        watermark = packet["read_watermark"]
        self.assertEqual(set(watermark), _WATERMARK_KEYS)
        for field in ("byte_length", "event_count", "record_count"):
            self.assertIs(type(watermark[field]), int)
            self.assertGreaterEqual(watermark[field], 0)
        self.assertIs(type(watermark["ledger_sha256"]), str)
        self.assertEqual(len(watermark["ledger_sha256"]), 64)
        self.assertTrue(
            all(character in "0123456789abcdef" for character in watermark["ledger_sha256"])
        )
        if watermark["event_count"] == 0:
            self.assertIsNone(watermark["last_event_id"])
            self.assertIsNone(watermark["last_occurred_at"])
        else:
            self.assertIs(type(watermark["last_event_id"]), str)
            self.assertIs(type(watermark["last_occurred_at"]), str)

        selection = packet["selection"]
        self.assertEqual(set(selection), _SELECTION_KEYS)
        scope_kind = "record_ids" if record_ids else "event_ids" if event_ids else "all_records"
        self.assertEqual(
            selection,
            {
                "scope_kind": scope_kind,
                "requested_record_ids": sorted(record_ids),
                "requested_event_ids": sorted(event_ids),
            },
        )

        events = packet["events"]
        self.assertIs(type(events), list)
        self.assertLessEqual(len(events), 50)
        for item in events:
            self.assertEqual(set(item), _EVENT_ENVELOPE_KEYS)
            event = item["event"]
            self.assertIs(type(event), dict)
            if event.get("event_type") == "recorded":
                self.assertEqual(set(event), _RECORDED_EVENT_KEYS | {"recurrence_key"} if "recurrence_key" in event else _RECORDED_EVENT_KEYS)
            else:
                self.assertEqual(event.get("event_type"), "resolved")
                self.assertEqual(set(event), _RESOLVED_EVENT_KEYS)
            provenance = item["provenance"]
            self.assertEqual(set(provenance), _EVENT_PROVENANCE_KEYS)
            self.assertEqual(provenance["relative_ledger_path"], "papercuts/events.jsonl")
            self.assertIs(type(provenance["source_line"]), int)
            self.assertGreater(provenance["source_line"], 0)

        records = packet["records"]
        self.assertIs(type(records), list)
        self.assertLessEqual(len(records), 25)
        for item in records:
            self.assertEqual(set(item), _RECORD_ENVELOPE_KEYS)
            record = item["record"]
            self.assertEqual(set(record), _RECORD_KEYS)
            for field in (
                "record_id",
                "summary",
                "status",
            ):
                self.assertIs(type(record[field]), str)
            for field in (
                "category",
                "expected",
                "observed",
                "evidence_basis",
            ):
                if record[field] is not None:
                    self.assertIs(type(record[field]), str)
            self.assertIs(type(record["recurrence_count"]), int)
            self.assertGreater(record["recurrence_count"], 0)
            self.assertIn(record["status"], {"open", "resolved"})
            if record["recurrence_key"] is not None:
                self.assertIs(type(record["recurrence_key"]), str)
            if record["resolution"] is not None:
                self.assertIs(type(record["resolution"]), str)
            provenance = item["provenance"]
            self.assertEqual(set(provenance), _RECORD_PROVENANCE_KEYS)
            self.assertIs(type(provenance["recorded_event_id"]), str)
            self.assertIs(type(provenance["recorded_source_line"]), int)
            self.assertGreater(provenance["recorded_source_line"], 0)
            if provenance["resolved_event_id"] is None:
                self.assertIsNone(provenance["resolved_source_line"])
            else:
                self.assertIs(type(provenance["resolved_event_id"]), str)
                self.assertIs(type(provenance["resolved_source_line"]), int)
                self.assertGreater(provenance["resolved_source_line"], 0)

        if scope_kind == "all_records":
            self.assertEqual(len(events), watermark["event_count"])
            self.assertEqual(len(records), watermark["record_count"])
        self.assertLessEqual(len(_canonical_json(packet)), 65_536)

    def _build_packet(
        self,
        paths,
        *,
        expected_head: str,
        record_ids: tuple[str, ...] = (),
        event_ids: tuple[str, ...] = (),
    ) -> str:
        actual = build_packet(
            paths,
            expected_head=expected_head,
            record_ids=record_ids,
            event_ids=event_ids,
        )
        packet = self._decode(actual)
        self._assert_v1_packet_shape(
            packet,
            expected_head=expected_head,
            record_ids=record_ids,
            event_ids=event_ids,
        )
        return actual

    def _assert_fixture_selection(
        self,
        packet: dict,
        expected_events: list[dict],
        expected_records: list[dict],
    ) -> None:
        self.assertEqual(packet["read_watermark"], EXPECTED_PACKET["read_watermark"])
        self.assertEqual(packet["events"], expected_events)
        self.assertEqual(packet["records"], expected_records)


class PacketFixtureTests(_PacketTestAssertions, unittest.TestCase):
    def test_fixture_is_one_canonical_json_object_plus_terminal_lf(self):
        self.assertEqual(EXPECTED_BYTES.count(b"\n"), 1)
        self.assertTrue(EXPECTED_BYTES.endswith(b"\n"))
        self.assertFalse(EXPECTED_BYTES[:-1].endswith(b"\n"))
        packet = json.loads(EXPECTED_BYTES)
        self.assertIs(type(packet), dict)
        self.assertEqual(EXPECTED_BYTES, _canonical_json(packet))

    def test_fixture_field_names_are_exactly_the_v1_contract(self):
        self.assertEqual(set(EXPECTED_PACKET), _TOP_LEVEL_KEYS)
        self.assertEqual(set(EXPECTED_PACKET["source_repository"]), _SOURCE_KEYS)
        self.assertEqual(set(EXPECTED_PACKET["read_watermark"]), _WATERMARK_KEYS)
        self.assertEqual(set(EXPECTED_PACKET["selection"]), _SELECTION_KEYS)
        for item in EXPECTED_PACKET["events"]:
            self.assertEqual(set(item), _EVENT_ENVELOPE_KEYS)
            self.assertEqual(set(item["provenance"]), _EVENT_PROVENANCE_KEYS)
        for item in EXPECTED_PACKET["records"]:
            self.assertEqual(set(item), _RECORD_ENVELOPE_KEYS)
            self.assertEqual(set(item["record"]), _RECORD_KEYS)
            self.assertEqual(set(item["provenance"]), _RECORD_PROVENANCE_KEYS)

    def test_fixture_freezes_source_order_provenance_and_reduction_semantics(self):
        events = EXPECTED_PACKET["events"]
        source_events = [json.loads(line) for line in VALID_BYTES.splitlines()]
        self.assertEqual(len(source_events), 3)
        self.assertEqual(
            events,
            [
                {
                    "event": source_events[0],
                    "provenance": {
                        "relative_ledger_path": "papercuts/events.jsonl",
                        "source_line": 1,
                    },
                },
                {
                    "event": source_events[1],
                    "provenance": {
                        "relative_ledger_path": "papercuts/events.jsonl",
                        "source_line": 2,
                    },
                },
                {
                    "event": source_events[2],
                    "provenance": {
                        "relative_ledger_path": "papercuts/events.jsonl",
                        "source_line": 3,
                    },
                },
            ],
        )
        self.assertEqual(
            [item["event"]["event_id"] for item in events],
            [EVENT_1, EVENT_2, EVENT_3],
        )
        self.assertEqual(
            [item["provenance"]["source_line"] for item in events],
            [1, 2, 3],
        )
        self.assertGreater(
            events[0]["event"]["occurred_at"], events[1]["event"]["occurred_at"]
        )
        self.assertEqual(
            [item["record"]["record_id"] for item in EXPECTED_PACKET["records"]],
            [RECORD_1, RECORD_2],
        )
        self.assertEqual(
            [item["record"]["recurrence_count"] for item in EXPECTED_PACKET["records"]],
            [2, 2],
        )
        self.assertEqual(
            EXPECTED_PACKET["records"],
            [
                {
                    "record": {
                        "record_id": "00000000-0000-4000-8000-000000000101",
                        "category": "tooling",
                        "summary": "editor opens",
                        "expected": "command opens",
                        "observed": "command stalls",
                        "evidence_basis": "user_observation",
                        "recurrence_count": 2,
                        "recurrence_key": "editor-startup",
                        "resolution": "disabled extension",
                        "status": "resolved",
                    },
                    "provenance": {
                        "recorded_event_id": "00000000-0000-4000-8000-000000000001",
                        "recorded_source_line": 1,
                        "resolved_event_id": "00000000-0000-4000-8000-000000000003",
                        "resolved_source_line": 3,
                    },
                },
                {
                    "record": {
                        "record_id": "00000000-0000-4000-8000-000000000102",
                        "category": "tooling",
                        "summary": "editor opens again",
                        "expected": "command opens",
                        "observed": "command waits",
                        "evidence_basis": "test",
                        "recurrence_count": 2,
                        "recurrence_key": "editor-startup",
                        "resolution": None,
                        "status": "open",
                    },
                    "provenance": {
                        "recorded_event_id": "00000000-0000-4000-8000-000000000002",
                        "recorded_source_line": 2,
                        "resolved_event_id": None,
                        "resolved_source_line": None,
                    },
                },
            ],
        )
        self.assertEqual(
            EXPECTED_PACKET["source_repository"],
            {
                "repository_scope": "git_common_dir",
                "relative_ledger_path": "papercuts/events.jsonl",
                "worktree_kind": "main",
                "head_state": "commit",
                "head_commit": "cf2fabd85c9bf73416772e33b3812a6c87074c49",
            },
        )
        self.assertEqual(
            EXPECTED_PACKET["selection"],
            {
                "scope_kind": "all_records",
                "requested_record_ids": [],
                "requested_event_ids": [],
            },
        )
        self.assertEqual(
            EXPECTED_PACKET["read_watermark"],
            {
                "byte_length": 939,
                "event_count": 3,
                "record_count": 2,
                "ledger_sha256": hashlib.sha256(VALID_BYTES).hexdigest(),
                "last_event_id": EVENT_3,
                "last_occurred_at": "2026-01-03T11:00:00.000000Z",
            },
        )


    def test_null_and_omission_rules_preserve_source_and_projection_shapes(self):
        packet = EXPECTED_PACKET
        self.assertNotIn("recurrence_key", packet["events"][2]["event"])
        self.assertIsNone(packet["records"][1]["record"]["resolution"])
        self.assertIsNone(packet["records"][1]["provenance"]["resolved_event_id"])
        self.assertIsNone(packet["records"][1]["provenance"]["resolved_source_line"])
        self.assertEqual(packet["records"][0]["record"]["resolution"], "disabled extension")


@unittest.skipUnless(_PACKET_IMPORT_ERROR is None, "packet API is not available yet")
class PacketContractTests(_PacketTestAssertions, unittest.TestCase):
    def _assert_packet_error(self, rule_id: str, operation) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(PacketError) as raised:
                operation()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(raised.exception.rule_id, rule_id)
        self.assertEqual(str(raised.exception), f"papercuts: {rule_id}")

    def test_all_scope_matches_the_representative_fixture_byte_for_byte(self):
        with _source(VALID_BYTES, committed=True) as paths:
            actual = self._build_packet(paths, expected_head=COMMITTED_HEAD)
        self.assertEqual(actual.encode("utf-8"), EXPECTED_BYTES)

    def test_built_fixture_round_trips_to_the_same_canonical_bytes(self):
        with _source(VALID_BYTES, committed=True) as paths:
            actual = self._build_packet(paths, expected_head=COMMITTED_HEAD)
        packet = self._decode(actual)
        self.assertEqual(actual.encode("utf-8"), _canonical_json(packet))

    def test_committed_provenance_is_fixed_and_path_neutral(self):
        with _source(VALID_BYTES, committed=True) as paths:
            packet = self._decode(self._build_packet(paths, expected_head=COMMITTED_HEAD))
        self.assertEqual(
            packet["source_repository"],
            {
                "repository_scope": "git_common_dir",
                "relative_ledger_path": "papercuts/events.jsonl",
                "worktree_kind": "main",
                "head_state": "commit",
                "head_commit": COMMITTED_HEAD,
            },
        )
        encoded = json.dumps(packet, ensure_ascii=False)
        for forbidden in (
            "top_level",
            '"git_dir"',
            '"common_dir"',
            "branch",
            "remote",
            paths.top_level,
            paths.git_dir,
            paths.common_dir,
        ):
            self.assertNotIn(forbidden, encoded)

    def test_empty_unborn_packet_has_full_empty_watermark_and_explicit_nulls(self):
        with _source(committed=False) as paths:
            packet = self._decode(self._build_packet(paths, expected_head="unborn"))
        self.assertEqual(
            packet["source_repository"],
            {
                "repository_scope": "git_common_dir",
                "relative_ledger_path": "papercuts/events.jsonl",
                "worktree_kind": "main",
                "head_state": "unborn",
                "head_commit": None,
            },
        )
        self.assertEqual(
            packet["read_watermark"],
            {
                "byte_length": 0,
                "event_count": 0,
                "record_count": 0,
                "ledger_sha256": hashlib.sha256(b"").hexdigest(),
                "last_event_id": None,
                "last_occurred_at": None,
            },
        )
        self.assertEqual(packet["events"], [])
        self.assertEqual(packet["records"], [])
        self.assertEqual(
            packet["selection"],
            {
                "scope_kind": "all_records",
                "requested_record_ids": [],
                "requested_event_ids": [],
            },
        )

    def test_record_scope_selects_complete_lifecycle_and_preserves_ledger_order(self):
        with _source(VALID_BYTES, committed=True) as paths:
            first = self._decode(
                self._build_packet(
                    paths, expected_head=COMMITTED_HEAD, record_ids=(RECORD_1,)
                )
            )
            second = self._decode(
                self._build_packet(
                    paths, expected_head=COMMITTED_HEAD, record_ids=(RECORD_2,)
                )
            )
        self.assertEqual(
            first["selection"],
            {
                "scope_kind": "record_ids",
                "requested_record_ids": [RECORD_1],
                "requested_event_ids": [],
            },
        )
        self._assert_fixture_selection(
            first,
            [EXPECTED_PACKET["events"][0], EXPECTED_PACKET["events"][2]],
            EXPECTED_PACKET["records"][:1],
        )
        self.assertEqual([item["event"]["event_id"] for item in first["events"]], [EVENT_1, EVENT_3])
        self.assertEqual(first["records"], EXPECTED_PACKET["records"][:1])
        self.assertEqual(
            second["selection"],
            {
                "scope_kind": "record_ids",
                "requested_record_ids": [RECORD_2],
                "requested_event_ids": [],
            },
        )
        self._assert_fixture_selection(
            second,
            [EXPECTED_PACKET["events"][1]],
            EXPECTED_PACKET["records"][1:],
        )
        self.assertEqual([item["event"]["event_id"] for item in second["events"]], [EVENT_2])
        self.assertEqual(second["records"], EXPECTED_PACKET["records"][1:])

    def test_event_scope_selects_exact_events_and_only_complete_lifecycles(self):
        with _source(VALID_BYTES, committed=True) as paths:
            recorded_only = self._decode(
                self._build_packet(
                    paths, expected_head=COMMITTED_HEAD, event_ids=(EVENT_1,)
                )
            )
            resolution_only = self._decode(
                self._build_packet(
                    paths, expected_head=COMMITTED_HEAD, event_ids=(EVENT_3,)
                )
            )
            complete = self._decode(
                self._build_packet(
                    paths,
                    expected_head=COMMITTED_HEAD,
                    event_ids=(EVENT_3, EVENT_1),
                )
            )
            open_lifecycle = self._decode(
                self._build_packet(
                    paths, expected_head=COMMITTED_HEAD, event_ids=(EVENT_2,)
                )
            )
        self._assert_fixture_selection(recorded_only, [EXPECTED_PACKET["events"][0]], [])
        self._assert_fixture_selection(resolution_only, [EXPECTED_PACKET["events"][2]], [])
        self._assert_fixture_selection(
            complete,
            [EXPECTED_PACKET["events"][0], EXPECTED_PACKET["events"][2]],
            EXPECTED_PACKET["records"][:1],
        )
        self._assert_fixture_selection(open_lifecycle, [EXPECTED_PACKET["events"][1]], EXPECTED_PACKET["records"][1:])
        self.assertEqual(recorded_only["records"], [])
        self.assertEqual(resolution_only["records"], [])
        self.assertEqual(
            [item["event"]["event_id"] for item in complete["events"]],
            [EVENT_1, EVENT_3],
        )
        self.assertEqual(complete["records"], EXPECTED_PACKET["records"][:1])
        self.assertEqual(open_lifecycle["records"], EXPECTED_PACKET["records"][1:])

    def test_selector_order_does_not_change_bytes_or_source_order(self):
        with _source(VALID_BYTES, committed=True) as paths:
            records_forward = self._build_packet(
                paths,
                expected_head=COMMITTED_HEAD,
                record_ids=(RECORD_1, RECORD_2),
            )
            records_reverse = self._build_packet(
                paths,
                expected_head=COMMITTED_HEAD,
                record_ids=(RECORD_2, RECORD_1),
            )
            events_forward = self._build_packet(
                paths,
                expected_head=COMMITTED_HEAD,
                event_ids=(EVENT_1, EVENT_2, EVENT_3),
            )
            events_reverse = self._build_packet(
                paths,
                expected_head=COMMITTED_HEAD,
                event_ids=(EVENT_3, EVENT_1, EVENT_2),
            )
        self.assertEqual(records_forward, records_reverse)
        self.assertEqual(events_forward, events_reverse)
        records_packet = self._decode(records_forward)
        events_packet = self._decode(events_forward)
        self._assert_fixture_selection(
            records_packet, EXPECTED_PACKET["events"], EXPECTED_PACKET["records"]
        )
        self._assert_fixture_selection(
            events_packet, EXPECTED_PACKET["events"], EXPECTED_PACKET["records"]
        )
        self.assertEqual(
            records_packet["selection"],
            {
                "scope_kind": "record_ids",
                "requested_record_ids": [RECORD_1, RECORD_2],
                "requested_event_ids": [],
            },
        )
        self.assertEqual(
            events_packet["selection"],
            {
                "scope_kind": "event_ids",
                "requested_record_ids": [],
                "requested_event_ids": [EVENT_1, EVENT_2, EVENT_3],
            },
        )
        self.assertEqual(
            [item["event"]["event_id"] for item in records_packet["events"]],
            [EVENT_1, EVENT_2, EVENT_3],
        )
        self.assertEqual(
            [item["event"]["event_id"] for item in events_packet["events"]],
            [EVENT_1, EVENT_2, EVENT_3],
        )

    def test_recorded_recurrence_key_omission_and_null_preserve_source_and_projection(self):
        events = _recorded_events(1) + _recorded_events(1, start=1, recurrence_key=None)
        raw = _canonical_ledger(events)
        with _source(raw, committed=False) as paths:
            packet = self._decode(self._build_packet(paths, expected_head="unborn"))

        self.assertEqual(
            packet["events"],
            [
                {
                    "event": events[0],
                    "provenance": {
                        "relative_ledger_path": "papercuts/events.jsonl",
                        "source_line": 1,
                    },
                },
                {
                    "event": events[1],
                    "provenance": {
                        "relative_ledger_path": "papercuts/events.jsonl",
                        "source_line": 2,
                    },
                },
            ],
        )
        self.assertNotIn("recurrence_key", packet["events"][0]["event"])
        self.assertIn("recurrence_key", packet["events"][1]["event"])
        self.assertIsNone(packet["events"][1]["event"]["recurrence_key"])
        for item in packet["records"]:
            self.assertIsNone(item["record"]["recurrence_key"])
            self.assertIsNone(item["record"]["resolution"])
            self.assertIsNone(item["provenance"]["resolved_event_id"])
            self.assertIsNone(item["provenance"]["resolved_source_line"])

    def test_nullable_capture_fields_preserve_event_and_record_projection(self):
        [event] = _recorded_events(1, recurrence_key=None)
        for field in ("category", "expected", "observed", "evidence_basis"):
            event[field] = None
        raw = _canonical_ledger([event])

        with _source(raw, committed=False) as paths:
            packet = self._decode(
                self._build_packet(
                    paths,
                    expected_head="unborn",
                    record_ids=(event["record_id"],),
                )
            )

        self.assertEqual(packet["events"][0]["event"], event)
        projected = packet["records"][0]["record"]
        self.assertEqual(projected["record_id"], event["record_id"])
        for field in (
            "category",
            "expected",
            "observed",
            "evidence_basis",
            "recurrence_key",
            "resolution",
        ):
            self.assertIsNone(projected[field])

    def test_non_ascii_ledger_text_is_literal_utf8_in_packet(self):
        text = "café — 观察"
        raw = _canonical_ledger(_recorded_events(1, text=text))
        with _source(raw, committed=False) as paths:
            actual = self._build_packet(paths, expected_head="unborn")
        encoded = actual.encode("utf-8")
        self.assertIn(text.encode("utf-8"), encoded)
        self.assertNotIn(json.dumps(text, ensure_ascii=True).encode("ascii"), encoded)

    def test_watermark_uses_the_physically_last_event_not_the_latest_timestamp(self):
        events = _recorded_events(2)
        events[-1]["occurred_at"] = "2026-01-01T00:00:00.000000Z"
        self.assertLess(events[-1]["occurred_at"], events[0]["occurred_at"])
        raw = _canonical_ledger(events)
        with _source(raw, committed=False) as paths:
            packet = self._decode(self._build_packet(paths, expected_head="unborn"))
        self.assertEqual(packet["read_watermark"]["last_event_id"], events[-1]["event_id"])
        self.assertEqual(
            packet["read_watermark"]["last_occurred_at"], events[-1]["occurred_at"]
        )

    def test_selector_failures_are_typed_non_disclosing_and_emit_no_stdout(self):
        unknown_record = _uuid(9000)
        unknown_event = _uuid(9001)
        malformed = "00000000-0000-1000-8000-000000009002"
        with _source(VALID_BYTES, committed=True) as paths:
            self._assert_packet_error(
                "E_UNKNOWN_RECORD",
                lambda: build_packet(
                    paths, expected_head=COMMITTED_HEAD, record_ids=(unknown_record,)
                ),
            )
            self._assert_packet_error(
                "E_UNKNOWN_EVENT",
                lambda: build_packet(
                    paths, expected_head=COMMITTED_HEAD, event_ids=(unknown_event,)
                ),
            )
            self._assert_packet_error(
                "E_UUID4",
                lambda: build_packet(
                    paths, expected_head=COMMITTED_HEAD, record_ids=(malformed,)
                ),
            )
            self._assert_packet_error(
                "E_UUID4",
                lambda: build_packet(
                    paths, expected_head=COMMITTED_HEAD, event_ids=(malformed,)
                ),
            )
            self._assert_packet_error(
                "E_PACKET_SELECTOR",
                lambda: build_packet(
                    paths,
                    expected_head=COMMITTED_HEAD,
                    record_ids=(RECORD_1, RECORD_1),
                ),
            )
            self._assert_packet_error(
                "E_PACKET_SELECTOR",
                lambda: build_packet(
                    paths,
                    expected_head=COMMITTED_HEAD,
                    event_ids=(EVENT_1, EVENT_1),
                ),
            )
            self._assert_packet_error(
                "E_PACKET_SELECTOR",
                lambda: build_packet(
                    paths,
                    expected_head=COMMITTED_HEAD,
                    record_ids=(RECORD_1,),
                    event_ids=(EVENT_1,),
                ),
            )

    def test_selector_limit_is_25_values_and_26_is_rejected(self):
        ids = tuple(_uuid(10_000 + offset) for offset in range(26))
        with _source(b"", committed=False) as paths:
            self._assert_packet_error(
                "E_UNKNOWN_RECORD",
                lambda: build_packet(paths, expected_head="unborn", record_ids=ids[:25]),
            )
            self._assert_packet_error(
                "E_PACKET_SELECTOR_LIMIT",
                lambda: build_packet(paths, expected_head="unborn", record_ids=ids),
            )
            self._assert_packet_error(
                "E_UNKNOWN_EVENT",
                lambda: build_packet(paths, expected_head="unborn", event_ids=ids[:25]),
            )
            self._assert_packet_error(
                "E_PACKET_SELECTOR_LIMIT",
                lambda: build_packet(paths, expected_head="unborn", event_ids=ids),
            )

    def test_wrong_expected_head_is_a_typed_failure_before_packet_output(self):
        with _source(VALID_BYTES, committed=True) as paths:
            self._assert_packet_error(
                "E_PACKET_WRONG_HEAD",
                lambda: build_packet(paths, expected_head="unborn"),
            )
            self._assert_packet_error(
                "E_PACKET_WRONG_HEAD",
                lambda: build_packet(paths, expected_head="not-a-git-object"),
            )

    def test_entry_bounds_accept_50_events_and_25_records(self):
        raw = _canonical_ledger(_complete_lifecycle_events(25))
        with _source(raw, committed=False) as paths:
            packet = self._decode(self._build_packet(paths, expected_head="unborn"))
        self.assertEqual(packet["read_watermark"]["event_count"], 50)
        self.assertEqual(packet["read_watermark"]["record_count"], 25)
        self.assertEqual(len(packet["events"]), 50)
        self.assertEqual(len(packet["records"]), 25)

    def test_record_entry_overflow_is_a_typed_packet_limit(self):
        raw = _canonical_ledger(_recorded_events(26))
        with _source(raw, committed=False) as paths:
            self._assert_packet_error(
                "E_PACKET_LIMIT",
                lambda: build_packet(paths, expected_head="unborn"),
            )

    def test_selected_event_overflow_is_a_typed_packet_limit(self):
        raw = _canonical_ledger(_complete_lifecycle_events(26))
        with _source(raw, committed=False) as paths:
            self._assert_packet_error(
                "E_PACKET_LIMIT",
                lambda: build_packet(paths, expected_head="unborn"),
            )

    def test_output_byte_overflow_is_a_typed_packet_limit(self):
        events = _recorded_events(
            25,
            summary_text="x" * 120,
            expected_text="x" * 500,
            observed_text="x" * 500,
            recurrence_key="r" * 80,
        )
        raw = _canonical_ledger(events)
        packet_size = len(_open_records_packet_size_probe(events, raw))
        self.assertEqual(packet_size, 79_713)
        self.assertGreater(packet_size, 65_536)
        with _source(raw, committed=False) as paths:
            self._assert_packet_error(
                "E_PACKET_LIMIT",
                lambda: build_packet(paths, expected_head="unborn"),
            )


if __name__ == "__main__":
    unittest.main()

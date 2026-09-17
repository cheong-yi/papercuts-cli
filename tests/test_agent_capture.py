import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


class AgentCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.env = dict(os.environ)
        self.env.update(
            {
                "LC_ALL": "C",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(SOURCE_ROOT),
            }
        )

    def tearDown(self):
        self.temp.cleanup()

    @property
    def storage(self):
        return self.repo / ".git" / "papercuts"

    @property
    def ledger(self):
        return self.storage / "events.jsonl"

    def _run(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, "-B", "-m", "papercuts", *args],
            cwd=cwd or self.repo,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _record(self, summary, *args, cwd=None):
        result = self._run("record", "--summary", summary, *args, cwd=cwd)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(len(result.stdout), 37)
        self.assertTrue(result.stdout.endswith("\n"))
        record_id = result.stdout[:-1]
        parsed = uuid.UUID(record_id)
        self.assertEqual(str(parsed), record_id)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(parsed.variant, uuid.RFC_4122)
        return record_id

    def _events(self):
        return [json.loads(line) for line in self.ledger.read_text().splitlines()]

    def test_compact_record_writes_explicit_nulls_and_packet_confirms_exact_id(self):
        record_id = self._record("brief friction")
        before = self.ledger.read_bytes()
        [event] = self._events()

        self.assertEqual(
            set(event),
            {
                "schema_version",
                "event_id",
                "event_type",
                "occurred_at",
                "record_id",
                "category",
                "summary",
                "expected",
                "observed",
                "evidence_basis",
                "recurrence_key",
            },
        )
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(event["event_type"], "recorded")
        self.assertEqual(event["record_id"], record_id)
        self.assertEqual(event["summary"], "brief friction")
        for field in (
            "category",
            "expected",
            "observed",
            "evidence_basis",
            "recurrence_key",
        ):
            self.assertIsNone(event[field])

        result = self._run(
            "packet",
            "--expect-head",
            "unborn",
            "--record-id",
            record_id,
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        packet = json.loads(result.stdout)
        self.assertEqual(
            packet["selection"],
            {
                "scope_kind": "record_ids",
                "requested_record_ids": [record_id],
                "requested_event_ids": [],
            },
        )
        self.assertEqual(len(packet["events"]), 1)
        self.assertEqual(packet["events"][0]["event"], event)
        self.assertEqual(len(packet["records"]), 1)
        projected = packet["records"][0]["record"]
        self.assertEqual(projected["record_id"], record_id)
        for field in (
            "category",
            "expected",
            "observed",
            "evidence_basis",
            "recurrence_key",
            "resolution",
        ):
            self.assertIsNone(projected[field])
        self.assertEqual(packet["read_watermark"]["event_count"], 1)
        self.assertEqual(packet["read_watermark"]["record_count"], 1)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_each_optional_capture_field_is_independently_accepted(self):
        cases = (
            (("--category", "docs"), "category", "docs"),
            (("--expected", "works"), "expected", "works"),
            (("--observed", "blocked"), "observed", "blocked"),
            (("--evidence-basis", "test"), "evidence_basis", "test"),
            (("--recurrence-key", "same-problem"), "recurrence_key", "same-problem"),
        )
        for index, (args, field, value) in enumerate(cases):
            with self.subTest(field=field):
                before_count = len(self._events()) if self.ledger.exists() else 0
                record_id = self._record(f"partial-{index}", *args)
                events = self._events()
                self.assertEqual(len(events), before_count + 1)
                event = events[-1]
                self.assertEqual(event["record_id"], record_id)
                self.assertEqual(event[field], value)
                for omitted in {
                    "category",
                    "expected",
                    "observed",
                    "evidence_basis",
                    "recurrence_key",
                } - {field}:
                    self.assertIsNone(event[omitted])

    def test_fully_specified_record_remains_supported(self):
        record_id = self._record(
            "full form",
            "--category",
            "docs",
            "--expected",
            "works",
            "--observed",
            "blocked",
            "--evidence-basis",
            "test",
            "--recurrence-key",
            "same-problem",
        )
        [event] = self._events()
        self.assertEqual(event["record_id"], record_id)
        self.assertEqual(
            (
                event["category"],
                event["expected"],
                event["observed"],
                event["evidence_basis"],
                event["recurrence_key"],
            ),
            ("docs", "works", "blocked", "test", "same-problem"),
        )

    def test_supplied_private_optional_text_is_rejected_before_storage(self):
        secret = "sk-" + "a" * 24
        result = self._run(
            "record",
            "--summary",
            "safe summary",
            "--expected",
            secret,
        )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (2, "", "papercuts: E_PRIVACY_SK_TOKEN field=expected\n"),
        )
        self.assertNotIn(secret, result.stderr)
        self.assertFalse(self.storage.exists())

    def test_invalid_optional_enum_is_rejected_before_storage_without_echo(self):
        rejected = "secret-category"
        result = self._run(
            "record",
            "--summary",
            "safe summary",
            "--category",
            rejected,
        )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (2, "", "papercuts: E_ARGUMENT\n"),
        )
        self.assertNotIn(rejected, result.stderr)
        self.assertFalse(self.storage.exists())

    def test_list_defaults_to_first_five_matching_records_in_recorded_order(self):
        record_ids = [self._record(f"item-{index}") for index in range(7)]
        resolved = self._run(
            "resolve", record_ids[1], "--resolution", "fixed"
        )
        self.assertEqual(resolved.returncode, 0, resolved.stderr)

        all_rows = self._run("list", "--status", "all")
        open_rows = self._run("list")
        resolved_rows = self._run("list", "--status", "resolved")

        self.assertEqual(
            all_rows.stdout,
            "".join(
                f'{record_ids[index]}\t'
                f'{"resolved" if index == 1 else "open"}\tnull\t"item-{index}"\n'
                for index in range(5)
            ),
        )
        self.assertEqual(
            open_rows.stdout,
            "".join(
                f'{record_ids[index]}\topen\tnull\t"item-{index}"\n'
                for index in (0, 2, 3, 4, 5)
            ),
        )
        self.assertEqual(
            resolved_rows.stdout,
            f'{record_ids[1]}\tresolved\tnull\t"item-1"\n',
        )
        self.assertEqual(
            (all_rows.returncode, open_rows.returncode, resolved_rows.returncode),
            (0, 0, 0),
        )

    def test_list_accepts_explicit_limits_one_and_twenty_five(self):
        record_ids = [self._record(f"item-{index:02}") for index in range(25)]

        one = self._run("list", "--status", "all", "--limit", "1")
        maximum = self._run("list", "--status", "all", "--limit", "25")

        self.assertEqual(
            (one.returncode, one.stdout, one.stderr),
            (0, f'{record_ids[0]}\topen\tnull\t"item-00"\n', ""),
        )
        self.assertEqual((maximum.returncode, maximum.stderr), (0, ""))
        self.assertEqual(
            maximum.stdout,
            "".join(
                f'{record_id}\topen\tnull\t"item-{index:02}"\n'
                for index, record_id in enumerate(record_ids)
            ),
        )
        self.assertLessEqual(len(maximum.stdout.encode("utf-8")), 4096)

    def test_list_rejects_invalid_limits_before_storage(self):
        for value in ("all", "0", "26", "-1"):
            with self.subTest(value=value):
                result = self._run("list", "--limit", value)
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr),
                    (2, "", "papercuts: E_ARGUMENT\n"),
                )
                self.assertFalse(self.storage.exists())

    def test_list_rejects_utf8_output_over_fixed_ceiling_without_stdout(self):
        for index in range(25):
            self._record(f"{index:02}-" + "é" * 117)
        before = self.ledger.read_bytes()

        result = self._run("list", "--status", "all", "--limit", "25")

        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (1, "", "papercuts: E_OUTPUT_LIMIT\n"),
        )
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_absent_ledger_bounded_list_is_read_only(self):
        result = self._run("list", "--limit", "25")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse(self.storage.exists())

    def test_compact_record_from_nested_directory_uses_git_common_dir(self):
        nested = self.repo / "one" / "two"
        nested.mkdir(parents=True)
        record_id = self._record("nested capture", cwd=nested)
        self.assertTrue(self.ledger.is_file())
        [event] = self._events()
        self.assertEqual(event["record_id"], record_id)
        self.assertEqual(event["summary"], "nested capture")


if __name__ == "__main__":
    unittest.main()

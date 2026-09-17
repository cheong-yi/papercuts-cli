import json
import pathlib
import unittest
import uuid

from papercuts.model import (
    LedgerError,
    ReducedRecord,
    parse_ledger,
    parse_and_reduce,
    reduce_events,
)


ROOT = pathlib.Path(__file__).parent
VALID = ROOT / "fixtures" / "valid-events.jsonl"
MALFORMED_TAIL = ROOT / "fixtures" / "malformed-tail.jsonl"


R1 = "00000000-0000-4000-8000-000000000101"
R2 = "00000000-0000-4000-8000-000000000102"
E1 = "00000000-0000-4000-8000-000000000001"


def event(**overrides):
    value = {
        "category": "docs",
        "event_id": "00000000-0000-4000-8000-000000000010",
        "event_type": "recorded",
        "evidence_basis": "docs",
        "expected": "a useful result",
        "observed": "a different result",
        "occurred_at": "2026-01-01T00:00:00.000000Z",
        "record_id": "00000000-0000-4000-8000-000000000110",
        "recurrence_key": "same-problem",
        "schema_version": 1,
        "summary": "small issue",
    }
    value.update(overrides)
    return value


def line(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def ledger(*values):
    return b"".join(line(value) for value in values)


def assert_rule(test, rule, operation, *, line_number=None, field=None):
    with test.assertRaises(LedgerError) as raised:
        operation()
    error = raised.exception
    test.assertEqual(error.rule_id, rule)
    if line_number is not None:
        test.assertEqual(error.line, line_number)
    if field is not None:
        test.assertEqual(error.field, field)
    test.assertNotIn("secret-value", str(error))


class ModelTests(unittest.TestCase):
    def test_valid_fixture_parses_and_reduces_in_recorded_order(self):
        events = parse_ledger(VALID.read_bytes())
        records = parse_and_reduce(VALID.read_bytes())

        self.assertEqual(len(events), 3)
        self.assertEqual([record.record_id for record in records], [R1, R2])
        self.assertEqual(records[0].status, "resolved")
        self.assertEqual(records[0].resolution, "disabled extension")
        self.assertEqual(records[0].recurrence_count, 2)
        self.assertEqual(records[1].status, "open")
        self.assertEqual(records[1].recurrence_count, 2)

    def test_malformed_tail_is_rejected_without_repair(self):
        assert_rule(self, "E_MISSING_FINAL_LF", lambda: parse_ledger(MALFORMED_TAIL.read_bytes()))

    def test_empty_ledger_is_valid(self):
        self.assertEqual(parse_ledger(b""), [])
        self.assertEqual(reduce_events([]), [])

    def test_canonicality_and_physical_bounds_are_checked(self):
        noncanonical = b'{"schema_version":1,"event_type":"recorded"}\n'
        assert_rule(self, "E_NON_CANONICAL", lambda: parse_ledger(noncanonical))
        assert_rule(self, "E_BOM", lambda: parse_ledger(b"\xef\xbb\xbf" + line(event())))
        assert_rule(self, "E_BLANK_LINE", lambda: parse_ledger(b"\n"))
        assert_rule(self, "E_LINE_TOO_LARGE", lambda: parse_ledger(b"{" + b"a" * 8192 + b"\n"))
        assert_rule(self, "E_LEDGER_TOO_LARGE", lambda: parse_ledger(b"x" * (8 * 1024 * 1024 + 1)))
        assert_rule(self, "E_INVALID_UTF8", lambda: parse_ledger(b"{\xff}\n"))

    def test_duplicate_keys_are_rejected(self):
        raw = b'{"category":"docs","category":"tooling"}\n'
        assert_rule(self, "E_DUPLICATE_KEY", lambda: parse_ledger(raw), line_number=1)

    def test_event_count_bound_is_rejected(self):
        raw = line(event()) * 10001
        assert_rule(self, "E_EVENT_LIMIT", lambda: parse_ledger(raw))

    def test_schema_shape_and_exact_types_are_rejected(self):
        for field, value in (("schema_version", True), ("event_id", 3), ("summary", 3), ("recurrence_key", 7)):
            assert_rule(self, "E_FIELD_TYPE", lambda field=field, value=value: parse_ledger(line(event(**{field: value}))), field=field)
        assert_rule(self, "E_UNKNOWN_FIELD", lambda: parse_ledger(line(event(extra="nope"))))
        missing = event()
        missing.pop("summary")
        assert_rule(self, "E_MISSING_FIELD", lambda: parse_ledger(line(missing)), field="summary")

    def test_enums_uuid_and_timestamp_are_strict(self):
        assert_rule(self, "E_ENUM", lambda: parse_ledger(line(event(category="private"))), field="category")
        assert_rule(self, "E_UUID4", lambda: parse_ledger(line(event(event_id=str(uuid.uuid1())))), field="event_id")
        assert_rule(self, "E_UUID4", lambda: parse_ledger(line(event(event_id="00000000-0000-4000-c000-000000000010"))), field="event_id")
        assert_rule(self, "E_TIMESTAMP", lambda: parse_ledger(line(event(occurred_at="2026-01-01T00:00:00Z"))), field="occurred_at")
        assert_rule(self, "E_TIMESTAMP", lambda: parse_ledger(line(event(occurred_at="2026-02-30T00:00:00.000000Z"))), field="occurred_at")

    def test_scalar_lengths_and_whitespace_are_enforced(self):
        assert_rule(self, "E_STRING_BOUNDS", lambda: parse_ledger(line(event(summary=""))), field="summary")
        assert_rule(self, "E_STRING_BOUNDS", lambda: parse_ledger(line(event(summary="x" * 121))), field="summary")
        assert_rule(self, "E_STRING_BOUNDS", lambda: parse_ledger(line(event(expected=" x"))), field="expected")
        assert_rule(self, "E_STRING_BOUNDS", lambda: parse_ledger(line(event(observed="x" * 501))), field="observed")
        assert_rule(self, "E_STRING_BOUNDS", lambda: parse_ledger(line(event(recurrence_key="1" + "a" * 80))), field="recurrence_key")
        assert_rule(self, "E_STRING_BOUNDS", lambda: parse_ledger(line(event(recurrence_key="Bad-Key"))), field="recurrence_key")

    def test_recurrence_key_may_be_omitted_or_null_but_null_records_do_not_merge(self):
        first = event(record_id=R1, event_id=E1, recurrence_key=None)
        second = event(record_id=R2, event_id="00000000-0000-4000-8000-000000000002", recurrence_key=None)
        records = parse_and_reduce(ledger(first, second))
        self.assertEqual([record.recurrence_count for record in records], [1, 1])
        omitted = dict(first)
        omitted.pop("recurrence_key")
        self.assertIsNone(parse_ledger(line(omitted))[0].get("recurrence_key"))

    def test_nullable_capture_fields_parse_and_reduce_without_inference(self):
        compact = event(
            category=None,
            expected=None,
            observed=None,
            evidence_basis=None,
            recurrence_key=None,
        )

        [parsed] = parse_ledger(line(compact))
        [record] = reduce_events([parsed])

        self.assertEqual(parsed, compact)
        self.assertEqual(record.summary, "small issue")
        self.assertIsNone(record.category)
        self.assertIsNone(record.expected)
        self.assertIsNone(record.observed)
        self.assertIsNone(record.evidence_basis)
        self.assertIsNone(record.recurrence_key)
        self.assertEqual(record.recurrence_count, 1)

    def test_duplicate_ids_and_invalid_transitions_are_rejected(self):
        assert_rule(self, "E_DUPLICATE_EVENT_ID", lambda: parse_ledger(ledger(event(), event(event_id=event()["event_id"], record_id=R2))))
        assert_rule(self, "E_DUPLICATE_RECORD_ID", lambda: parse_ledger(ledger(event(), event(event_id="00000000-0000-4000-8000-000000000011", record_id=event()["record_id"]))))
        resolved = {"event_id": "00000000-0000-4000-8000-000000000012", "event_type": "resolved", "occurred_at": "2026-01-01T00:00:00.000000Z", "record_id": R1, "resolution": "fixed", "schema_version": 1}
        assert_rule(self, "E_UNKNOWN_RECORD", lambda: parse_ledger(line(resolved)))
        first_resolution = resolved | {"event_id": "00000000-0000-4000-8000-000000000013"}
        second_resolution = resolved | {"event_id": "00000000-0000-4000-8000-000000000014"}
        assert_rule(self, "E_DUPLICATE_RESOLUTION", lambda: parse_ledger(ledger(event(record_id=R1), first_resolution, second_resolution)))

    def test_resolved_event_has_exact_shape(self):
        resolved = {"event_id": "00000000-0000-4000-8000-000000000012", "event_type": "resolved", "occurred_at": "2026-01-01T00:00:00.000000Z", "record_id": R1, "resolution": "fixed", "schema_version": 1}
        missing = dict(resolved)
        missing.pop("resolution")
        assert_rule(self, "E_MISSING_FIELD", lambda: parse_ledger(line(missing)), field="resolution")
        assert_rule(self, "E_UNKNOWN_FIELD", lambda: parse_ledger(line(resolved | {"category": "docs"})))
        self.assertEqual(parse_ledger(ledger(event(record_id=R1), resolved))[1]["event_type"], "resolved")

    def test_reduction_is_independent_per_record_and_recurrence(self):
        first = event(record_id=R1, event_id=E1, recurrence_key="repeat")
        second = event(record_id=R2, event_id="00000000-0000-4000-8000-000000000002", recurrence_key="repeat")
        resolved = {"event_id": "00000000-0000-4000-8000-000000000003", "event_type": "resolved", "occurred_at": "2026-01-01T00:00:00.000000Z", "record_id": R2, "resolution": "fixed", "schema_version": 1}
        records = reduce_events(parse_ledger(ledger(first, second, resolved)))
        self.assertIsInstance(records[0], ReducedRecord)
        self.assertEqual(records[0].status, "open")
        self.assertEqual(records[1].status, "resolved")
        self.assertEqual(records[0].record_id, R1)
        self.assertEqual(records[1].record_id, R2)


if __name__ == "__main__":
    unittest.main()

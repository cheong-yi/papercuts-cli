"""Strict canonical event-ledger parsing and deterministic reduction."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any, Iterable
import uuid


MAX_LEDGER_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 8192
MAX_EVENTS = 10000

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
_RECURRENCE_RE = re.compile(r"[a-z0-9][a-z0-9._-]*", re.ASCII)

_COMMON_FIELDS = frozenset({
    "schema_version",
    "event_id",
    "event_type",
    "occurred_at",
    "record_id",
})
_RECORDED_FIELDS = frozenset({
    *_COMMON_FIELDS,
    "category",
    "summary",
    "expected",
    "observed",
    "evidence_basis",
})
_RESOLVED_FIELDS = frozenset({*_COMMON_FIELDS, "resolution"})
_CATEGORIES = frozenset({"docs", "tooling", "setup", "validation", "research", "other"})
_EVIDENCE_BASES = frozenset({"tool_error", "test", "docs", "user_observation", "inferred"})


class LedgerError(ValueError):
    """A stable, non-disclosing ledger validation error."""

    def __init__(self, rule_id: str, *, line: int | None = None, field: str | None = None):
        self.rule_id = rule_id
        self.line = line
        self.field = field
        super().__init__(rule_id)

    def __str__(self) -> str:
        parts = [self.rule_id]
        if self.line is not None:
            parts.append(f"line={self.line}")
        if self.field is not None:
            parts.append(f"field={self.field}")
        return "papercuts: " + " ".join(parts)


@dataclass(frozen=True, slots=True)
class ReducedRecord:
    """The deterministic state of one recorded event."""

    record_id: str
    category: str | None
    summary: str
    expected: str | None
    observed: str | None
    evidence_basis: str | None
    recurrence_key: str | None
    recurrence_count: int
    status: str
    resolution: str | None = None


class _DuplicateKey(Exception):
    pass


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _reject_constant(_: str) -> Any:
    raise ValueError


def _error(rule_id: str, line: int | None = None, field: str | None = None) -> LedgerError:
    return LedgerError(rule_id, line=line, field=field)


def _require_string(value: Any, field: str, *, line: int, maximum: int) -> str:
    if type(value) is not str:
        raise _error("E_FIELD_TYPE", line, field)
    if not 1 <= len(value) <= maximum or value != value.strip():
        raise _error("E_STRING_BOUNDS", line, field)
    return value


def _require_uuid4(value: Any, field: str, *, line: int) -> str:
    if type(value) is not str:
        raise _error("E_FIELD_TYPE", line, field)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise _error("E_UUID4", line, field) from None
    if str(parsed) != value or parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise _error("E_UUID4", line, field)
    return value


def _require_timestamp(value: Any, *, line: int) -> str:
    if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
        raise _error("E_TIMESTAMP", line, "occurred_at")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise _error("E_TIMESTAMP", line, "occurred_at") from None
    return value


def _validate_shape(value: Any, *, line: int) -> str:
    if type(value) is not dict:
        raise _error("E_EVENT_OBJECT", line)

    if "event_type" not in value:
        raise _error("E_MISSING_FIELD", line, "event_type")
    event_type = value["event_type"]
    if type(event_type) is not str:
        raise _error("E_FIELD_TYPE", line, "event_type")
    if event_type not in {"recorded", "resolved"}:
        raise _error("E_ENUM", line, "event_type")

    expected_fields = _RECORDED_FIELDS if event_type == "recorded" else _RESOLVED_FIELDS
    unknown = sorted(set(value) - expected_fields - ({"recurrence_key"} if event_type == "recorded" else set()))
    if unknown:
        raise _error("E_UNKNOWN_FIELD", line)
    missing = sorted(expected_fields - set(value))
    if missing:
        raise _error("E_MISSING_FIELD", line, missing[0])

    if type(value["schema_version"]) is not int:
        raise _error("E_FIELD_TYPE", line, "schema_version")
    if value["schema_version"] != 1:
        raise _error("E_SCHEMA_VERSION", line, "schema_version")
    _require_uuid4(value["event_id"], "event_id", line=line)
    _require_uuid4(value["record_id"], "record_id", line=line)
    _require_timestamp(value["occurred_at"], line=line)

    if event_type == "recorded":
        category = value["category"]
        if category is not None and (
            type(category) is not str or category not in _CATEGORIES
        ):
            raise _error("E_ENUM", line, "category")
        evidence_basis = value["evidence_basis"]
        if evidence_basis is not None and (
            type(evidence_basis) is not str
            or evidence_basis not in _EVIDENCE_BASES
        ):
            raise _error("E_ENUM", line, "evidence_basis")
        _require_string(value["summary"], "summary", line=line, maximum=120)
        if value["expected"] is not None:
            _require_string(value["expected"], "expected", line=line, maximum=500)
        if value["observed"] is not None:
            _require_string(value["observed"], "observed", line=line, maximum=500)
        if "recurrence_key" in value and value["recurrence_key"] is not None:
            key = value["recurrence_key"]
            if type(key) is not str:
                raise _error("E_FIELD_TYPE", line, "recurrence_key")
            if len(key) > 80 or _RECURRENCE_RE.fullmatch(key) is None:
                raise _error("E_STRING_BOUNDS", line, "recurrence_key")
    else:
        _require_string(value["resolution"], "resolution", line=line, maximum=500)
    return event_type


def parse_ledger(data: bytes | bytearray | memoryview) -> list[dict[str, Any]]:
    """Parse and validate canonical ledger bytes, returning event dictionaries."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise _error("E_INPUT_TYPE")
    raw_data = bytes(data)
    if len(raw_data) > MAX_LEDGER_BYTES:
        raise _error("E_LEDGER_TOO_LARGE")
    if not raw_data:
        return []
    if raw_data.startswith(b"\xef\xbb\xbf"):
        raise _error("E_BOM")
    if not raw_data.endswith(b"\n"):
        raise _error("E_MISSING_FINAL_LF")

    physical_lines = raw_data.split(b"\n")[:-1]
    if len(physical_lines) > MAX_EVENTS:
        raise _error("E_EVENT_LIMIT")
    events: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    record_ids: set[str] = set()
    resolved_ids: set[str] = set()

    for line_number, encoded_line in enumerate(physical_lines, 1):
        line_size = len(encoded_line) + 1
        if line_size > MAX_LINE_BYTES:
            raise _error("E_LINE_TOO_LARGE", line_number)
        if not encoded_line:
            raise _error("E_BLANK_LINE", line_number)
        try:
            text = encoded_line.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _error("E_INVALID_UTF8", line_number) from None
        try:
            parsed = json.loads(
                text,
                object_pairs_hook=_pairs_without_duplicates,
                parse_constant=_reject_constant,
            )
        except _DuplicateKey:
            raise _error("E_DUPLICATE_KEY", line_number) from None
        except (json.JSONDecodeError, ValueError, TypeError):
            raise _error("E_INVALID_JSON", line_number) from None
        if type(parsed) is not dict:
            raise _error("E_EVENT_OBJECT", line_number)
        try:
            canonical = json.dumps(
                parsed,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise _error("E_INVALID_JSON", line_number) from None
        if canonical != encoded_line:
            raise _error("E_NON_CANONICAL", line_number)

        event_type = _validate_shape(parsed, line=line_number)
        event_id = parsed["event_id"]
        if event_id in event_ids:
            raise _error("E_DUPLICATE_EVENT_ID", line_number, "event_id")
        event_ids.add(event_id)
        record_id = parsed["record_id"]
        if event_type == "recorded":
            if record_id in record_ids:
                raise _error("E_DUPLICATE_RECORD_ID", line_number, "record_id")
            record_ids.add(record_id)
        else:
            if record_id not in record_ids:
                raise _error("E_UNKNOWN_RECORD", line_number, "record_id")
            if record_id in resolved_ids:
                raise _error("E_DUPLICATE_RESOLUTION", line_number, "record_id")
            resolved_ids.add(record_id)
        events.append(parsed)

    return events


def reduce_events(events: Iterable[dict[str, Any]]) -> list[ReducedRecord]:
    """Reduce validated events in first-recorded order."""

    events = list(events)
    ordered: dict[str, dict[str, Any]] = {}
    resolutions: dict[str, str] = {}
    recurrence_counts = Counter(
        event.get("recurrence_key")
        for event in events
        if event.get("event_type") == "recorded" and event.get("recurrence_key") is not None
    )
    for event in events:
        if event["event_type"] == "recorded":
            ordered[event["record_id"]] = event
        else:
            resolutions[event["record_id"]] = event["resolution"]
    result: list[ReducedRecord] = []
    for record_id, event in ordered.items():
        key = event.get("recurrence_key")
        result.append(
            ReducedRecord(
                record_id=record_id,
                category=event["category"],
                summary=event["summary"],
                expected=event["expected"],
                observed=event["observed"],
                evidence_basis=event["evidence_basis"],
                recurrence_key=key,
                recurrence_count=recurrence_counts[key] if key is not None else 1,
                status="resolved" if record_id in resolutions else "open",
                resolution=resolutions.get(record_id),
            )
        )
    return result


def parse_and_reduce(data: bytes | bytearray | memoryview) -> list[ReducedRecord]:
    return reduce_events(parse_ledger(data))


# Descriptive aliases for callers that prefer explicit names.
parse_jsonl = parse_ledger
validate_ledger = parse_ledger
reduce_ledger = parse_and_reduce


__all__ = [
    "LedgerError",
    "ReducedRecord",
    "MAX_LEDGER_BYTES",
    "MAX_LINE_BYTES",
    "MAX_EVENTS",
    "parse_ledger",
    "parse_jsonl",
    "validate_ledger",
    "reduce_events",
    "reduce_ledger",
    "parse_and_reduce",
]

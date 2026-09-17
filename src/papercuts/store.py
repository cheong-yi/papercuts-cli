"""Append-only lifecycle operations over the papercuts ledger."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
import uuid

from . import repo as _repo
from .model import LedgerError, reduce_events
from .privacy import validate_caller_field
from .repo import read_storage


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _uuid() -> str:
    return str(uuid.uuid4())


def _canonical(event: dict) -> bytes:
    return json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def _append(common: str, event: dict, decide) -> None:
    _repo.append_event(common, _canonical(event), decide)


def record(common: str, *, summary: str, category: str | None = None, expected: str | None = None, observed: str | None = None, evidence_basis: str | None = None, recurrence_key: str | None = None) -> str:
    validate_caller_field(summary, "summary")
    for value, field in ((expected, "expected"), (observed, "observed")):
        if value is not None:
            validate_caller_field(value, field)
    validate_caller_field(recurrence_key, "recurrence_key")
    if category is not None and category not in {"docs", "tooling", "setup", "validation", "research", "other"}:
        raise LedgerError("E_ENUM", field="category")
    if evidence_basis is not None and evidence_basis not in {"tool_error", "test", "docs", "user_observation", "inferred"}:
        raise LedgerError("E_ENUM", field="evidence_basis")
    record_id = _uuid()
    event = {"schema_version": 1, "event_id": _uuid(), "event_type": "recorded", "occurred_at": _now(), "record_id": record_id, "category": category, "summary": summary, "expected": expected, "observed": observed, "evidence_basis": evidence_basis, "recurrence_key": recurrence_key}
    _append(common, event, lambda _events: None)
    return record_id


def resolve(common: str, record_id: str, *, resolution: str) -> str:
    validate_caller_field(resolution, "resolution")
    try:
        parsed_id = uuid.UUID(record_id)
    except (ValueError, AttributeError, TypeError):
        raise LedgerError("E_UUID4", field="record_id") from None
    if str(parsed_id) != record_id or parsed_id.version != 4 or parsed_id.variant != uuid.RFC_4122:
        raise LedgerError("E_UUID4", field="record_id")
    event = {"schema_version": 1, "event_id": _uuid(), "event_type": "resolved", "occurred_at": _now(), "record_id": record_id, "resolution": resolution}

    def decide(events):
        records = {row.record_id: row for row in reduce_events(events)}
        if record_id not in records:
            raise LedgerError("E_UNKNOWN_RECORD", field="record_id")
        if records[record_id].status == "resolved":
            raise LedgerError("E_DUPLICATE_RESOLUTION", field="record_id")

    _append(common, event, decide)
    return record_id


def snapshot(common: str):
    return reduce_events(read_storage(common))


def check(common: str) -> tuple[int, int]:
    events = read_storage(common)
    return len(events), len(reduce_events(events))


__all__ = ["record", "resolve", "snapshot", "check"]

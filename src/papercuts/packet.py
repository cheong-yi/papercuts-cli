"""Deterministic evidence-packet projection over a coherent repository snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

from .model import ReducedRecord, reduce_events
from .repo import RepoPaths, SourceSnapshot, capture_source_snapshot


_RELATIVE_LEDGER_PATH = "papercuts/events.jsonl"
_MAX_SELECTORS = 25
_MAX_PACKET_EVENTS = 50
_MAX_PACKET_RECORDS = 25
_MAX_PACKET_BYTES = 65_536
_UUID4_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_HEAD_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class PacketError(RuntimeError):
    """A stable, non-disclosing packet projection error."""

    def __init__(self, rule_id: str):
        self.rule_id = rule_id
        super().__init__(rule_id)

    def __str__(self) -> str:
        return f"papercuts: {self.rule_id}"


def _error(rule_id: str) -> PacketError:
    return PacketError(rule_id)


def _selector_tuple(values: Iterable[str]) -> tuple[str, ...]:
    selected = []
    try:
        iterator = iter(values)
        for _ in range(_MAX_SELECTORS + 1):
            try:
                selected.append(next(iterator))
            except StopIteration:
                break
    except Exception:
        raise _error("E_PACKET_SELECTOR") from None
    return tuple(selected)


def _validate_selectors(
    record_ids: Iterable[str], event_ids: Iterable[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    records = _selector_tuple(record_ids)
    events = _selector_tuple(event_ids)
    if records and events:
        raise _error("E_PACKET_SELECTOR")
    selected = records or events
    if len(selected) > _MAX_SELECTORS:
        raise _error("E_PACKET_SELECTOR_LIMIT")
    if any(type(value) is not str or _UUID4_RE.fullmatch(value) is None for value in selected):
        raise _error("E_UUID4")
    if len(selected) != len(set(selected)):
        raise _error("E_PACKET_SELECTOR")
    return records, events


def _validate_expected_head(expected_head: str) -> None:
    if type(expected_head) is not str or (
        expected_head != "unborn" and _HEAD_RE.fullmatch(expected_head) is None
    ):
        raise _error("E_PACKET_WRONG_HEAD")


def _record_projection(record: ReducedRecord) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "category": record.category,
        "summary": record.summary,
        "expected": record.expected,
        "observed": record.observed,
        "evidence_basis": record.evidence_basis,
        "recurrence_count": record.recurrence_count,
        "recurrence_key": record.recurrence_key,
        "resolution": record.resolution,
        "status": record.status,
    }


def _record_envelopes(
    events: list[dict[str, Any]], records: list[ReducedRecord]
) -> tuple[list[dict[str, Any]], dict[str, frozenset[str]]]:
    recorded: dict[str, tuple[str, int]] = {}
    resolved: dict[str, tuple[str, int]] = {}
    lifecycle_ids: dict[str, set[str]] = {}
    for source_line, event in enumerate(events, 1):
        record_id = event["record_id"]
        event_id = event["event_id"]
        lifecycle_ids.setdefault(record_id, set()).add(event_id)
        if event["event_type"] == "recorded":
            recorded[record_id] = (event_id, source_line)
        else:
            resolved[record_id] = (event_id, source_line)

    envelopes = []
    for record in records:
        recorded_event_id, recorded_source_line = recorded[record.record_id]
        resolution = resolved.get(record.record_id)
        envelopes.append(
            {
                "record": _record_projection(record),
                "provenance": {
                    "recorded_event_id": recorded_event_id,
                    "recorded_source_line": recorded_source_line,
                    "resolved_event_id": resolution[0] if resolution else None,
                    "resolved_source_line": resolution[1] if resolution else None,
                },
            }
        )
    return envelopes, {
        record_id: frozenset(event_ids)
        for record_id, event_ids in lifecycle_ids.items()
    }


def _event_envelopes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "event": event,
            "provenance": {
                "relative_ledger_path": _RELATIVE_LEDGER_PATH,
                "source_line": source_line,
            },
        }
        for source_line, event in enumerate(events, 1)
    ]


def _select_projection(
    events: list[dict[str, Any]],
    event_envelopes: list[dict[str, Any]],
    record_envelopes: list[dict[str, Any]],
    lifecycle_ids: dict[str, frozenset[str]],
    record_ids: tuple[str, ...],
    event_ids: tuple[str, ...],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    source_record_ids = {event["record_id"] for event in events}
    source_event_ids = {event["event_id"] for event in events}

    if record_ids:
        requested_records = set(record_ids)
        if not requested_records <= source_record_ids:
            raise _error("E_UNKNOWN_RECORD")
        selected_events = [
            envelope
            for event, envelope in zip(events, event_envelopes)
            if event["record_id"] in requested_records
        ]
        selected_records = [
            envelope
            for envelope in record_envelopes
            if envelope["record"]["record_id"] in requested_records
        ]
        return "record_ids", selected_events, selected_records

    if event_ids:
        requested_events = set(event_ids)
        if not requested_events <= source_event_ids:
            raise _error("E_UNKNOWN_EVENT")
        selected_events = [
            envelope
            for event, envelope in zip(events, event_envelopes)
            if event["event_id"] in requested_events
        ]
        complete_records = {
            record_id
            for record_id, required_ids in lifecycle_ids.items()
            if required_ids <= requested_events
        }
        selected_records = [
            envelope
            for envelope in record_envelopes
            if envelope["record"]["record_id"] in complete_records
        ]
        return "event_ids", selected_events, selected_records

    return "all_records", event_envelopes, record_envelopes


def _source_repository(source: SourceSnapshot) -> dict[str, Any]:
    return {
        "repository_scope": "git_common_dir",
        "relative_ledger_path": _RELATIVE_LEDGER_PATH,
        "worktree_kind": source.worktree_kind,
        "head_state": source.head.state,
        "head_commit": source.head.commit,
    }


def build_packet(
    repo_paths: RepoPaths,
    *,
    expected_head: str,
    record_ids: Iterable[str] = (),
    event_ids: Iterable[str] = (),
) -> str:
    """Build one complete canonical packet without output or source mutation."""

    requested_record_ids, requested_event_ids = _validate_selectors(
        record_ids, event_ids
    )
    _validate_expected_head(expected_head)
    source = capture_source_snapshot(repo_paths)
    actual_head = source.head.commit if source.head.state == "commit" else "unborn"
    if expected_head != actual_head:
        raise _error("E_PACKET_WRONG_HEAD")

    events = source.storage.materialize_events()
    records = reduce_events(events)
    all_event_envelopes = _event_envelopes(events)
    all_record_envelopes, lifecycle_ids = _record_envelopes(events, records)
    scope_kind, selected_events, selected_records = _select_projection(
        events,
        all_event_envelopes,
        all_record_envelopes,
        lifecycle_ids,
        requested_record_ids,
        requested_event_ids,
    )
    if (
        len(selected_events) > _MAX_PACKET_EVENTS
        or len(selected_records) > _MAX_PACKET_RECORDS
    ):
        raise _error("E_PACKET_LIMIT")

    last_event = events[-1] if events else None
    packet = {
        "packet_schema_version": 1,
        "packet_kind": "evidence_packet",
        "source_repository": _source_repository(source),
        "read_watermark": {
            "byte_length": len(source.storage.raw_bytes),
            "event_count": len(events),
            "record_count": len(records),
            "ledger_sha256": hashlib.sha256(source.storage.raw_bytes).hexdigest(),
            "last_event_id": last_event["event_id"] if last_event else None,
            "last_occurred_at": last_event["occurred_at"] if last_event else None,
        },
        "selection": {
            "scope_kind": scope_kind,
            "requested_record_ids": sorted(requested_record_ids),
            "requested_event_ids": sorted(requested_event_ids),
        },
        "events": selected_events,
        "records": selected_records,
    }
    text = json.dumps(
        packet,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    if len(text.encode("utf-8")) > _MAX_PACKET_BYTES:
        raise _error("E_PACKET_LIMIT")
    return text


__all__ = ["PacketError", "build_packet"]

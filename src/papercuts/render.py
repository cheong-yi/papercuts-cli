"""Deterministic inert Markdown formatting for reduced records."""

from __future__ import annotations

from collections.abc import Iterable
import json

from .model import ReducedRecord


def render_markdown(records: Iterable[ReducedRecord]) -> str:
    """Format already-selected records as deterministic Markdown."""

    sections: list[str] = []
    for record in records:
        category = "null" if record.category is None else record.category
        evidence_basis = (
            "null" if record.evidence_basis is None else record.evidence_basis
        )
        lines = [
            (
                f"## {record.record_id} | {record.status} | {category} "
                f"| recurrence={record.recurrence_count}"
            ),
            "    summary="
            + json.dumps(
                record.summary,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ),
            "    expected="
            + json.dumps(
                record.expected,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ),
            "    observed="
            + json.dumps(
                record.observed,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ),
            f"    evidence_basis={evidence_basis}",
        ]
        if record.status == "resolved":
            lines.append(
                "    resolution="
                + json.dumps(
                    record.resolution,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        sections.append("\n".join(lines))

    if not sections:
        return "# Papercuts\n\n_No records._\n"
    return "# Papercuts\n\n" + "\n\n".join(sections) + "\n"


__all__ = ["render_markdown"]

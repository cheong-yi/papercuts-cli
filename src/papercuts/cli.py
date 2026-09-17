"""Explicit command-line lifecycle for the local papercuts ledger."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import NoReturn

from .model import LedgerError
from .packet import PacketError, build_packet
from .privacy import PrivacyError
from .render import render_markdown
from .repo import RepoError, StorageError, discover_repository
from .store import check, record, resolve, snapshot


DEFAULT_ROWS = 5
MAX_ROWS = 25
MAX_STDOUT_BYTES = 4096

_INPUT_RULES = frozenset(
    {
        "E_ENUM",
        "E_UUID4",
        "E_UNKNOWN_RECORD",
        "E_DUPLICATE_RESOLUTION",
    }
)
_PACKET_INPUT_RULES = frozenset(
    {
        "E_PACKET_SELECTOR",
        "E_PACKET_SELECTOR_LIMIT",
        "E_PACKET_WRONG_HEAD",
        "E_UNKNOWN_EVENT",
        "E_UNKNOWN_RECORD",
        "E_UUID4",
    }
)


def _diagnose(message: str) -> None:
    """Best-effort exact diagnostic output without recursive failure handling."""

    line = message + "\n"
    try:
        written = sys.stderr.write(line)
        if written != len(line):
            return
        sys.stderr.flush()
    except (OSError, UnicodeError):
        return


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        _diagnose("papercuts: E_ARGUMENT")
        raise SystemExit(2)


def _row_limit(value: str) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("invalid limit") from None
    if not 1 <= limit <= MAX_ROWS:
        raise argparse.ArgumentTypeError("invalid limit")
    return limit


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="papercut")
    commands = parser.add_subparsers(dest="command", required=True)

    record_parser = commands.add_parser("record")
    record_parser.add_argument(
        "--category",
        choices=("docs", "tooling", "setup", "validation", "research", "other"),
    )
    record_parser.add_argument("--summary", required=True)
    record_parser.add_argument("--expected")
    record_parser.add_argument("--observed")
    record_parser.add_argument(
        "--evidence-basis",
        choices=("tool_error", "test", "docs", "user_observation", "inferred"),
    )
    record_parser.add_argument("--recurrence-key")

    resolve_parser = commands.add_parser("resolve")
    resolve_parser.add_argument("record_id")
    resolve_parser.add_argument("--resolution", required=True)

    list_parser = commands.add_parser("list")
    list_parser.add_argument(
        "--status", choices=("open", "resolved", "all"), default="open"
    )
    list_parser.add_argument("--limit", type=_row_limit, default=DEFAULT_ROWS)

    render_parser = commands.add_parser("render")
    render_parser.add_argument(
        "--status", choices=("open", "resolved", "all"), default="open"
    )

    commands.add_parser("check")

    packet_parser = commands.add_parser("packet")
    packet_parser.add_argument("--expect-head", required=True, metavar="OID|unborn")
    packet_parser.add_argument("--record-id", action="append")
    packet_parser.add_argument("--event-id", action="append")
    return parser


def _emit(text: str, *, after_commit: bool = False) -> int:
    try:
        written = sys.stdout.write(text)
        if written != len(text):
            raise OSError("short output")
        sys.stdout.flush()
    except (OSError, UnicodeError):
        rule = "E_OUTPUT_AFTER_COMMIT" if after_commit else "E_OUTPUT"
        _diagnose(f"papercuts: {rule}")
        return 1
    return 0


def _select(records, status: str):
    return [
        item for item in records if status == "all" or item.status == status
    ]


def _format_rows(records) -> str:
    lines: list[str] = []
    for item in records:
        summary = json.dumps(item.summary, ensure_ascii=False)
        category = "null" if item.category is None else item.category
        lines.append(
            f"{item.record_id}\t{item.status}\t{category}\t{summary}\n"
        )
    return "".join(lines)


def _run(args: argparse.Namespace, repo_paths) -> int:
    common_dir = repo_paths.common_dir
    if args.command == "record":
        record_id = record(
            common_dir,
            category=args.category,
            summary=args.summary,
            expected=args.expected,
            observed=args.observed,
            evidence_basis=args.evidence_basis,
            recurrence_key=args.recurrence_key,
        )
        return _emit(f"{record_id}\n", after_commit=True)

    if args.command == "resolve":
        record_id = resolve(
            common_dir,
            args.record_id,
            resolution=args.resolution,
        )
        return _emit(f"resolved {record_id}\n", after_commit=True)

    if args.command in {"list", "render"}:
        records = _select(snapshot(common_dir), args.status)
        if args.command == "list":
            output = _format_rows(records[: args.limit])
            if len(output.encode("utf-8")) > MAX_STDOUT_BYTES:
                raise LedgerError("E_OUTPUT_LIMIT")
            return _emit(output)
        return _emit(render_markdown(records))

    if args.command == "packet":
        packet = build_packet(
            repo_paths,
            expected_head=args.expect_head,
            record_ids=args.record_id or (),
            event_ids=args.event_id or (),
        )
        return _emit(packet)

    event_count, record_count = check(common_dir)
    return _emit(f"ok: {event_count} events, {record_count} records\n")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    try:
        repo_paths = discover_repository()
        return _run(args, repo_paths)
    except PrivacyError as exc:
        _diagnose(str(exc))
        return 2
    except PacketError as exc:
        _diagnose(str(exc))
        return 2 if exc.rule_id in _PACKET_INPUT_RULES else 1
    except LedgerError as exc:
        _diagnose(str(exc))
        return 2 if exc.line is None and exc.rule_id in _INPUT_RULES else 1
    except (RepoError, StorageError) as exc:
        _diagnose(str(exc))
        return 1


__all__ = ["main"]

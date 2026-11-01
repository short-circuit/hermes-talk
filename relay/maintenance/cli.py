"""Command-line entry point for ``python -m relay.maintenance prune``.

The CLI loads the same environment-backed settings as the relay, configures
logging, redaction, and the audit sink so deletions land in
``$DATA_DIR/audit.log``, then runs the requested prune.  It never prints
absolute paths in the human summary.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relay.config import Settings
from relay.logging import configure_audit, configure_logging, configure_redaction
from relay.maintenance.retention import (
    RetentionPolicy,
    RetentionReport,
    prune_all,
)


def _non_negative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"expected an integer, got {raw!r}"
        raise argparse.ArgumentTypeError(msg) from exc
    if value < 0:
        msg = f"expected a non-negative integer, got {value}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _iso8601(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        msg = f"expected an ISO 8601 timestamp, got {raw!r}"
        raise argparse.ArgumentTypeError(msg) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m relay.maintenance",
        description="Delete expired relay data: inbox attachments, outbox artifacts, transcripts.",
    )
    subparsers = parser.add_subparsers(dest="command")
    prune = subparsers.add_parser(
        "prune",
        help="delete data past its retention window",
        description=(
            "Unlink expired inbox attachments, remove expired outbox artifacts, and delete "
            "expired api_server Hermes sessions.  Retention 0 keeps data until manual delete."
        ),
    )
    prune.add_argument("--dry-run", action="store_true", help="report without deleting anything")
    prune.add_argument("--json", action="store_true", help="print the report as JSON")
    prune.add_argument("--inbox-days", type=_non_negative_int, default=None)
    prune.add_argument("--outbox-days", type=_non_negative_int, default=None)
    prune.add_argument("--transcript-days", type=_non_negative_int, default=None)
    prune.add_argument("--inbox-dir", type=str, default=None)
    prune.add_argument("--outbox-dir", type=str, default=None)
    prune.add_argument(
        "--now",
        type=_iso8601,
        default=None,
        help="reference time as ISO 8601 (tests and replays; naive is UTC)",
    )
    return parser


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> RetentionPolicy:
    if args.inbox_dir:
        settings.inbox_dir = Path(args.inbox_dir).expanduser()
    if args.outbox_dir:
        settings.outbox_dir = Path(args.outbox_dir).expanduser()
    policy = RetentionPolicy.from_settings(settings)
    if args.inbox_days is not None:
        policy = RetentionPolicy(
            inbox_days=args.inbox_days,
            outbox_days=policy.outbox_days,
            transcript_days=policy.transcript_days,
        )
    if args.outbox_days is not None:
        policy = RetentionPolicy(
            inbox_days=policy.inbox_days,
            outbox_days=args.outbox_days,
            transcript_days=policy.transcript_days,
        )
    if args.transcript_days is not None:
        policy = RetentionPolicy(
            inbox_days=policy.inbox_days,
            outbox_days=policy.outbox_days,
            transcript_days=args.transcript_days,
        )
    return policy


def _human_summary(report: RetentionReport) -> str:
    mode = "dry run" if report.dry_run else "prune"
    lines = [
        f"Retention {mode}: {len(report.deleted)} deleted, "
        f"{len(report.skipped)} skipped, {len(report.errors)} errors"
    ]
    for item in report.deleted:
        lines.append(
            f"  deleted {item.kind} {item.identifier} (age {item.age_days}d, reason {item.reason})"
        )
    for item in report.skipped:
        lines.append(f"  skipped {item.kind} {item.identifier} (reason {item.reason})")
    for kind, identifier, message in report.errors:
        lines.append(f"  error {kind} {identifier}: {message}")
    if report.stale_locks_removed:
        lines.append(f"  stale lock files removed: {report.stale_locks_removed}")
    return "\n".join(lines)


def _run_prune(args: argparse.Namespace) -> int:
    settings = Settings()
    policy = _apply_overrides(settings, args)
    configure_logging(settings)
    configure_redaction(settings)
    configure_audit(settings)

    report = prune_all(
        settings,
        policy,
        now=args.now,
        dry_run=args.dry_run,
    )

    if args.json:
        payload: dict[str, Any] = report.to_dict()
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_human_summary(report))
    return 0 if not report.errors else 1


def main(argv: list[str] | None = None) -> int:
    """Run the maintenance CLI; return the process exit code."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 2
    if args.command != "prune":
        parser.print_usage(sys.stderr)
        return 2
    return _run_prune(args)


__all__ = ["main"]

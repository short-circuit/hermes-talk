"""Offline log-retention CLI: ``python -m relay.logging.prune``.

The command reads configuration from the environment (via ``Settings``), scans
existing log files, and deletes or rotates them.  It never creates directories,
opens no sockets, and makes no network calls.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from relay.config import Settings
from relay.logging.retention import PrunePolicy, prune_logs


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m relay.logging.prune",
        description="Delete or rotate old relay logs and audit files offline.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="DATA_DIR to prune (default: DATA_DIR from the environment).",
    )
    parser.add_argument(
        "--max-age-days",
        type=_positive_int,
        default=None,
        help="Delete rotated files older than this many days.",
    )
    parser.add_argument(
        "--max-bytes",
        type=_positive_int,
        default=None,
        help="Maximum bytes per log family before pruning rotated files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the plan without deleting or rotating anything.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the report as JSON.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the retention CLI and return an exit status."""
    args = _parser().parse_args(argv)
    settings = Settings()
    data_dir = args.data_dir.expanduser() if args.data_dir else settings.data_dir
    defaults = PrunePolicy.from_settings(settings)
    policy = PrunePolicy(
        max_age_days=args.max_age_days or defaults.max_age_days,
        max_bytes=args.max_bytes or defaults.max_bytes,
        backup_count=defaults.backup_count,
    )

    paths: list[Path] = []
    if data_dir is not None:
        paths = [data_dir / "logs", data_dir]

    report = prune_logs(paths, policy, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    else:
        print(f"deleted: {len(report['deleted'])}")
        for path in report["deleted"]:
            print(f"  delete {path}")
        print(f"rotated: {len(report['rotated'])}")
        for path in report["rotated"]:
            print(f"  rotate {path}")
        print(f"bytes_before: {report['bytes_before']}")
        print(f"bytes_after: {report['bytes_after']}")
        if args.dry_run:
            print("dry run: no files changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

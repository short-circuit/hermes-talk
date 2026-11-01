"""Pure retention planning and execution for relay logs and audit files.

The planner never mutates the filesystem.  Execution deletes whole rotated
files or renames the active audit file; it never truncates an active audit log
and never rewrites earlier bytes.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from relay.config import Settings

_ACTIVE_FILES = frozenset({"relay.log", "audit.log"})


@dataclass(frozen=True)
class PrunePolicy:
    """Retention limits applied to one log family at a time."""

    max_age_days: int
    max_bytes: int
    backup_count: int = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> PrunePolicy:
        """Build a conservative policy from environment-backed settings."""
        return cls(
            max_age_days=max(settings.log_max_age_days, settings.audit_max_age_days),
            max_bytes=max(settings.log_max_bytes, settings.audit_max_bytes),
            backup_count=max(settings.log_backup_count, settings.audit_backup_count),
        )


class PruneAction(NamedTuple):
    """One planned retention action."""

    action: str
    path: Path
    reason: str


def _is_active(path: Path) -> bool:
    return path.name in _ACTIVE_FILES


def _family(path: Path) -> str:
    name = path.name
    if name in _ACTIVE_FILES:
        return name
    if ".log." in name:
        return name.split(".log.", 1)[0] + ".log"
    return name


def _collect(paths: Iterable[Path | str]) -> list[Path]:
    found: set[Path] = set()
    for item in paths:
        candidate = Path(item)
        try:
            if candidate.is_dir():
                for child in candidate.glob("*.log*"):
                    if child.is_file():
                        found.add(child)
            elif candidate.is_file() and fnmatch.fnmatch(candidate.name, "*.log*"):
                found.add(candidate)
                # A file path also owns its rotated siblings (audit.log.<ts>).
                for sibling in candidate.parent.glob(f"{candidate.name}.*"):
                    if sibling.is_file():
                        found.add(sibling)
        except OSError:
            continue
    return sorted(found, key=str)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _mtime_datetime(path: Path) -> datetime:
    return datetime.fromtimestamp(_mtime(path), tz=UTC)


def _unique_rotation_path(path: Path, now: datetime) -> Path:
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{stamp}-{counter}")
        counter += 1
    return candidate


def plan_prune(
    paths: Iterable[Path | str],
    policy: PrunePolicy,
    *,
    now: datetime | None = None,
) -> list[PruneAction]:
    """Return the actions needed to satisfy ``policy`` without touching files."""
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    cutoff = current - timedelta(days=policy.max_age_days)

    files = _collect(paths)
    to_delete: dict[Path, str] = {}

    # Age pruning never removes the active relay.log or audit.log.
    for path in files:
        if _is_active(path):
            continue
        if _mtime_datetime(path) < cutoff:
            to_delete[path] = "age"

    remaining = [path for path in files if path not in to_delete]
    families: dict[str, list[Path]] = {}
    for path in remaining:
        families.setdefault(_family(path), []).append(path)

    rotate: list[Path] = []
    for family, family_files in sorted(families.items()):
        active = next((path for path in family_files if _is_active(path)), None)
        rotated = sorted(
            (path for path in family_files if not _is_active(path)),
            key=lambda path: (_mtime(path), str(path)),
        )
        total = sum(_size(path) for path in family_files)

        if total > policy.max_bytes:
            for path in rotated:
                if total <= policy.max_bytes:
                    break
                total -= _size(path)
                to_delete[path] = "size"

            if (
                active is not None
                and total > policy.max_bytes
                and family == "audit.log"
                and _size(active) > policy.max_bytes
            ):
                rotate.append(active)

        surviving_rotated = [path for path in rotated if path not in to_delete]
        cap = policy.backup_count - 1 if rotate and rotate[-1] == active else policy.backup_count
        while len(surviving_rotated) > max(cap, 0):
            oldest = surviving_rotated.pop(0)
            to_delete[oldest] = "size"

    actions: list[PruneAction] = []
    for path, reason in to_delete.items():
        actions.append(PruneAction("delete", path, reason))
    for path in rotate:
        actions.append(PruneAction("rotate", path, "size"))
    return actions


def prune_logs(
    paths: Iterable[Path | str],
    policy: PrunePolicy,
    *,
    dry_run: bool = False,
) -> dict[str, list[str] | int]:
    """Execute a retention plan and return a JSON-friendly report."""
    targets = _collect(paths)
    bytes_before = sum(_size(path) for path in targets)
    actions = plan_prune(paths, policy)
    now = datetime.now(UTC)

    deleted: list[str] = []
    rotated: list[str] = []
    projected = bytes_before
    for action in actions:
        if action.action == "delete":
            projected -= _size(action.path)
            if not dry_run:
                with suppress(OSError):
                    action.path.unlink()
            deleted.append(str(action.path))
        elif action.action == "rotate":
            target = _unique_rotation_path(action.path, now)
            if not dry_run:
                with suppress(OSError):
                    os.replace(action.path, target)
            rotated.append(str(target))

    bytes_after = max(projected, 0) if dry_run else sum(_size(path) for path in _collect(paths))
    return {
        "deleted": deleted,
        "rotated": rotated,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
    }

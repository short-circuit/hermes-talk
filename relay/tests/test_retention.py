"""Offline tests for data retention and deletion.

No sockets are opened: transcript pruning always uses a fake transport.  Audit
assertions read a real ``AuditLog`` configured against a temporary ``DATA_DIR``.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from relay.config import Settings
from relay.logging import configure_audit
from relay.maintenance import (
    HermesUnavailableError,
    ReferenceGuard,
    RetentionConfigError,
    RetentionPolicy,
    prune_all,
    prune_inbox,
    prune_outbox,
    prune_sessions,
)
from relay.maintenance.cli import main

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _write(path: Path, content: bytes = b"payload", *, age_days: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    timestamp = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    overrides.setdefault("data_dir", data_dir)
    return Settings(_env_file=None, **overrides)


def _configured(tmp_path: Path, **overrides: Any) -> tuple[Settings, Any]:
    settings = _settings(tmp_path, **overrides)
    sink = configure_audit(settings)
    assert sink is not None
    return settings, sink


def _row(
    session_id: str,
    *,
    source: str = "api_server",
    last_active: float | None = None,
    started_at: float | None = None,
) -> dict[str, Any]:
    return {
        "id": session_id,
        "source": source,
        "last_active": last_active,
        "started_at": started_at,
        "title": session_id,
        "archived": False,
        "hidden": False,
    }


class FakeTransport:
    """Records list/delete calls and returns canned pages and statuses."""

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        statuses: dict[str, int] | None = None,
        pages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.rows = rows or []
        self.statuses = statuses or {}
        self.pages = pages
        self.list_calls: list[tuple[int, int]] = []
        self.delete_calls: list[str] = []

    def list_sessions(self, *, offset: int, limit: int) -> dict[str, Any]:
        self.list_calls.append((offset, limit))
        if self.pages is not None:
            index = offset // limit
            return self.pages[index]
        return {
            "object": "list",
            "data": self.rows if offset == 0 else [],
            "limit": limit,
            "offset": offset,
            "has_more": False,
        }

    def delete_session(self, session_id: str) -> int:
        self.delete_calls.append(session_id)
        return self.statuses.get(session_id, 200)


class ExplodingTransport:
    """Fails the test if any network-shaped call is attempted."""

    def list_sessions(self, *, offset: int, limit: int) -> dict[str, Any]:
        raise AssertionError("list_sessions should not be called")

    def delete_session(self, session_id: str) -> int:
        raise AssertionError("delete_session should not be called")


class UnavailableTransport:
    def list_sessions(self, *, offset: int, limit: int) -> dict[str, Any]:
        raise HermesUnavailableError("connection refused")

    def delete_session(self, session_id: str) -> int:
        raise HermesUnavailableError("connection refused")


def _events(sink: Any, name: str) -> list[dict[str, Any]]:
    return [entry for entry in sink.read_entries() if entry.get("event") == name]


def test_expired_inbox_attachment_deleted_and_fresh_survives(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    old = _write(inbox / "old.png", b"old-bytes", age_days=40)
    fresh = _write(inbox / "fresh.png", b"fresh-bytes", age_days=1)
    settings, sink = _configured(tmp_path, inbox_dir=inbox)

    report = prune_inbox(settings, RetentionPolicy(inbox_days=30), now=NOW)

    assert not old.exists()
    assert fresh.exists()
    assert len(report.deleted) == 1
    item = report.deleted[0]
    assert item.kind == "inbox"
    assert item.identifier == "old.png"
    assert item.reason == "age"
    assert item.age_days >= 30
    assert item.size_bytes == len(b"old-bytes")

    deleted = _events(sink, "retention.deleted")
    assert len(deleted) == 1
    entry = deleted[0]
    assert entry["kind"] == "inbox"
    assert entry["path"] == "old.png"
    assert entry["reason"] == "age"
    assert entry["age_days"] >= 30
    assert entry["size_bytes"] == len(b"old-bytes")
    assert len(_events(sink, "retention.summary")) == 1


def test_claimed_inbox_file_is_skipped_until_released(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    old = _write(inbox / "claimed.png", b"claimed", age_days=40)
    settings, sink = _configured(tmp_path, inbox_dir=inbox)
    guard = ReferenceGuard(inbox)
    policy = RetentionPolicy(inbox_days=30)

    with guard.hold(old):
        report = prune_inbox(settings, policy, now=NOW, guard=guard)
        assert old.exists()
        assert not report.deleted
        assert any(item.reason == "in_use" for item in report.skipped)
        skipped = _events(sink, "retention.skipped")
        assert any(entry.get("reason") == "in_use" for entry in skipped)

    second = prune_inbox(settings, policy, now=NOW, guard=guard)
    assert not old.exists()
    assert [item.identifier for item in second.deleted] == ["claimed.png"]


def test_outbox_nested_expired_deleted_and_fresh_symlink_outside_untouched(
    tmp_path: Path,
) -> None:
    outbox = tmp_path / "outbox"
    nested = _write(outbox / "sub" / "artifact.bin", b"artifact", age_days=30)
    fresh = _write(outbox / "fresh.bin", b"fresh", age_days=1)
    outside = _write(tmp_path / "outside" / "target.txt", b"outside", age_days=60)
    link = outbox / "link.txt"
    link.symlink_to(outside)
    settings, _sink = _configured(tmp_path, outbox_dir=outbox)

    report = prune_outbox(settings, RetentionPolicy(outbox_days=14), now=NOW)

    assert not nested.exists()
    assert fresh.exists()
    assert link.is_symlink()
    assert outside.exists()
    assert [item.identifier for item in report.deleted] == ["sub/artifact.bin"]


def test_transcript_pruning_statuses_and_source_filter(tmp_path: Path) -> None:
    old = (NOW - timedelta(days=100)).timestamp()
    recent = (NOW - timedelta(days=2)).timestamp()
    rows = [
        _row("s-old", last_active=old, started_at=old),
        _row("s-recent", last_active=recent, started_at=recent),
        _row("s-telegram", source="telegram", last_active=old, started_at=old),
        _row("s-gone", last_active=old, started_at=old),
        _row("s-busy", last_active=old, started_at=old),
        _row("s-error", last_active=old, started_at=old),
        _row("s-notime", last_active=None, started_at=None),
    ]
    transport = FakeTransport(
        rows,
        statuses={"s-gone": 404, "s-busy": 409, "s-error": 500},
    )
    settings, sink = _configured(tmp_path, hermes_api_key="test-key")

    report = prune_sessions(
        settings,
        RetentionPolicy(transcript_days=90),
        now=NOW,
        transport=transport,
    )

    assert sorted(transport.delete_calls) == ["s-busy", "s-error", "s-gone", "s-old"]
    assert [item.identifier for item in report.deleted] == ["s-old"]
    reasons = {(item.identifier, item.reason) for item in report.skipped}
    assert ("s-gone", "already_gone") in reasons
    assert ("s-busy", "active_turn") in reasons
    assert ("s-notime", "no_timestamp") in reasons
    assert "s-telegram" not in transport.delete_calls
    assert "s-recent" not in transport.delete_calls
    assert [(kind, identifier) for kind, identifier, _message in report.errors] == [
        ("transcript", "s-error")
    ]

    deleted = _events(sink, "retention.deleted")
    assert len(deleted) == 1
    assert deleted[0]["session_id"] == "s-old"
    assert deleted[0]["reason"] == "age"


def test_transcript_transport_unavailable_is_a_single_error(tmp_path: Path) -> None:
    settings, sink = _configured(tmp_path, hermes_api_key="test-key")

    report = prune_sessions(
        settings,
        RetentionPolicy(transcript_days=90),
        now=NOW,
        transport=UnavailableTransport(),
    )

    assert report.errors == [("transcript", "-", "connection refused")]
    assert not report.deleted
    errors = _events(sink, "retention.error")
    assert len(errors) == 1
    assert errors[0]["error"] == "connection refused"


def test_transcript_pagination_and_malformed_body(tmp_path: Path) -> None:
    old = (NOW - timedelta(days=100)).timestamp()
    first = {
        "object": "list",
        "data": [_row("s-page-1", last_active=old, started_at=old)],
        "limit": 200,
        "offset": 0,
        "has_more": True,
    }
    second = {
        "object": "list",
        "data": [_row("s-page-2", last_active=old, started_at=old)],
        "limit": 200,
        "offset": 200,
        "has_more": False,
    }
    settings, _sink = _configured(tmp_path, hermes_api_key="test-key")
    transport = FakeTransport(pages=[first, second])

    report = prune_sessions(
        settings,
        RetentionPolicy(transcript_days=90),
        now=NOW,
        transport=transport,
    )

    assert sorted(transport.delete_calls) == ["s-page-1", "s-page-2"]
    assert [item.identifier for item in report.deleted] == ["s-page-1", "s-page-2"]
    assert transport.list_calls == [(0, 200), (200, 200)]

    malformed = FakeTransport(pages=[{"object": "list"}])
    broken = prune_sessions(
        settings,
        RetentionPolicy(transcript_days=90),
        now=NOW,
        transport=malformed,
    )
    assert broken.deleted == []
    assert broken.errors == [("transcript", "-", "malformed_list_body")]
    assert malformed.delete_calls == []


def test_transcript_retention_zero_makes_no_calls(tmp_path: Path) -> None:
    settings = _settings(tmp_path, hermes_api_key="test-key")

    report = prune_sessions(
        settings,
        RetentionPolicy(transcript_days=0),
        now=NOW,
        transport=ExplodingTransport(),
    )

    assert report.deleted == []
    assert report.skipped == []
    assert report.errors == []


def test_transcript_retention_without_api_key_records_error(tmp_path: Path) -> None:
    settings, sink = _configured(tmp_path)

    report = prune_sessions(
        settings,
        RetentionPolicy(transcript_days=30),
        now=NOW,
        transport=ExplodingTransport(),
    )

    assert report.errors == [("transcript", "-", "hermes_api_key_not_configured")]
    errors = _events(sink, "retention.error")
    assert len(errors) == 1
    assert errors[0]["error"] == "hermes_api_key_not_configured"


def test_repeated_prune_is_idempotent(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    _write(inbox / "old.png", b"old", age_days=40)
    settings, sink = _configured(tmp_path, inbox_dir=inbox)
    policy = RetentionPolicy(inbox_days=30)

    first = prune_inbox(settings, policy, now=NOW)
    assert len(first.deleted) == 1
    deleted_before = len(_events(sink, "retention.deleted"))

    second = prune_inbox(settings, policy, now=NOW)
    assert second.deleted == []
    assert len(_events(sink, "retention.deleted")) == deleted_before


def test_dry_run_reports_without_deleting_or_delete_calls(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    old_inbox = _write(inbox / "old.png", b"old", age_days=10)
    old_outbox = _write(outbox / "old.bin", b"old", age_days=10)
    old = (NOW - timedelta(days=100)).timestamp()
    transport = FakeTransport([_row("s-old", last_active=old, started_at=old)])
    settings, sink = _configured(
        tmp_path,
        inbox_dir=inbox,
        outbox_dir=outbox,
        hermes_api_key="test-key",
    )

    report = prune_all(
        settings,
        RetentionPolicy(inbox_days=1, outbox_days=1, transcript_days=90),
        now=NOW,
        dry_run=True,
        transport=transport,
    )

    assert old_inbox.exists()
    assert old_outbox.exists()
    assert transport.delete_calls == []
    planned = {(item.kind, item.identifier) for item in report.deleted}
    assert ("inbox", "old.png") in planned
    assert ("outbox", "old.bin") in planned
    assert ("transcript", "s-old") in planned
    assert report.dry_run is True
    summaries = _events(sink, "retention.summary")
    assert len(summaries) == 1
    assert summaries[0]["dry_run"] is True
    assert _events(sink, "retention.deleted") == []
    assert _events(sink, "retention.skipped") == []
    assert _events(sink, "retention.error") == []


def test_zero_retention_keeps_everything_and_negative_is_rejected(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    old_inbox = _write(inbox / "old.png", b"old", age_days=400)
    old_outbox = _write(outbox / "old.bin", b"old", age_days=400)
    settings, _sink = _configured(tmp_path, inbox_dir=inbox, outbox_dir=outbox)

    report = prune_all(
        settings,
        RetentionPolicy(inbox_days=0, outbox_days=0, transcript_days=0),
        now=NOW,
        transport=ExplodingTransport(),
    )

    assert report.deleted == []
    assert old_inbox.exists()
    assert old_outbox.exists()

    for field in (
        "inbox_retention_days",
        "outbox_retention_days",
        "transcript_retention_days",
    ):
        with pytest.raises(ValidationError, match="retention days must be zero or greater"):
            Settings(_env_file=None, **{field: -1})


def test_audit_entries_are_relative_and_content_free(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    content = b"SECRET-ATTACHMENT-CONTENT"
    _write(inbox / "old.png", content, age_days=40)
    settings, sink = _configured(tmp_path, inbox_dir=inbox)

    prune_inbox(settings, RetentionPolicy(inbox_days=30), now=NOW)

    audit_text = sink.path.read_text(encoding="utf-8")
    assert "SECRET-ATTACHMENT-CONTENT" not in audit_text
    for entry in sink.read_entries():
        for key in ("path", "session_id"):
            value = entry.get(key)
            if isinstance(value, str) and value != "-":
                assert not value.startswith("/")
    assert stat.S_IMODE(sink.path.stat().st_mode) == 0o600


def test_cli_json_dry_run_returns_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    _write(inbox / "old.png", b"old", age_days=10)
    monkeypatch.setenv("INBOX_DIR", str(inbox))
    monkeypatch.setenv("OUTBOX_DIR", str(outbox))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    code = main(
        [
            "prune",
            "--json",
            "--dry-run",
            "--inbox-days",
            "1",
            "--outbox-days",
            "1",
            "--transcript-days",
            "0",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["dry_run"] is True
    assert payload["deleted"][0]["identifier"] == "old.png"


def test_cli_transcript_without_api_key_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    code = main(["prune", "--transcript-days", "1"])

    captured = capsys.readouterr()
    assert code == 1
    assert "hermes_api_key_not_configured" in captured.out
    audit_path = tmp_path / "data" / "audit.log"
    entries = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        entry.get("event") == "retention.error"
        and entry.get("error") == "hermes_api_key_not_configured"
        for entry in entries
    )


def test_cli_missing_subcommand_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower()


def test_root_inside_repository_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = tmp_path / "inbox"
    old = _write(inbox / "old.png", b"old", age_days=40)
    settings, _sink = _configured(tmp_path, inbox_dir=inbox)
    monkeypatch.setattr(
        "relay.maintenance.retention.path_is_inside_repo",
        lambda _path: True,
    )

    report = prune_inbox(settings, RetentionPolicy(inbox_days=30), now=NOW)

    assert old.exists()
    assert report.deleted == []
    assert report.errors == [("inbox", ".", "root_inside_repo")]


def test_reference_guard_rejects_target_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "inbox"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    guard = ReferenceGuard(root)

    with pytest.raises(RetentionConfigError):
        guard.lock_path(outside)
    with pytest.raises(RetentionConfigError), guard.hold(outside):
        pass


def test_stale_lock_cleanup_leaves_busy_claims(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    old = _write(inbox / "claimed.png", b"claimed", age_days=40)
    settings, _sink = _configured(tmp_path, inbox_dir=inbox)
    guard = ReferenceGuard(inbox)
    policy = RetentionPolicy(inbox_days=30)
    lock_path = guard.lock_path(old)

    with guard.hold(old):
        os.unlink(old)
        busy = prune_inbox(settings, policy, now=NOW, guard=guard)
        assert lock_path.exists()
        assert busy.stale_locks_removed == 0

    assert lock_path.exists()
    released = prune_inbox(settings, policy, now=NOW, guard=guard)
    assert not lock_path.exists()
    assert released.stale_locks_removed == 1


def test_hold_retries_when_lock_file_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = tmp_path / "inbox"
    target = _write(inbox / "attachment.png", age_days=1)
    guard = ReferenceGuard(inbox)
    lock_path = guard.lock_path(target)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b"")

    stale_descriptor = os.open(lock_path, os.O_RDWR)
    os.unlink(lock_path)
    lock_path.write_bytes(b"")

    real_open = os.open
    replacements = 0
    opens = 0

    def replacing_open(path: Any, flags: int, mode: int = 0o777) -> int:
        nonlocal replacements, opens
        opens += 1
        descriptor = real_open(path, flags, mode)
        if replacements == 0 and Path(path) == lock_path:
            replacements += 1
            os.unlink(lock_path)
            lock_path.write_bytes(b"")
        return descriptor

    monkeypatch.setattr("relay.maintenance.retention.os.open", replacing_open)
    try:
        with guard.hold(target):
            pass
        assert replacements == 1
        assert opens == 2
    finally:
        os.close(stale_descriptor)

    assert lock_path.exists()
    assert guard.is_claimed(target) is False

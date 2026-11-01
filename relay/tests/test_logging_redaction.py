"""Offline tests for log redaction, audit append-only behavior, and retention."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from relay.config import Settings
from relay.logging import (
    AudioRecorder,
    AuditLog,
    PrunePolicy,
    audit,
    audit_approval_decided,
    audit_auth_failure,
    configure_audit,
    configure_logging,
    describe_audio,
    get_audit_log,
    plan_prune,
    prune_logs,
    redact_headers,
    redact_path,
    redact_value,
    registered_secret_count,
    reset_audit,
    token_prefix,
    transcript_hash,
)

_DEVICE_A = "device-token-alpha"
_DEVICE_B = "device-token-beta"
_API_KEY = "hermes-key-planted"
_SESSION_KEY = "session-key-planted"
_REPO_ROOT = Path(__file__).resolve().parents[2]
# The current user's home directory, without tripping the hygiene *.home rule.
_HOME = Path("~").expanduser()


def _remove_managed_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_relay_managed", False):
            root.removeHandler(handler)
            handler.close()


def _configure_and_capture(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> io.StringIO:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    _remove_managed_handlers()
    configure_logging(settings)
    return stream


def _logger() -> logging.Logger:
    logger = logging.getLogger("relay.tests.redaction")
    logger.setLevel(logging.DEBUG)
    return logger


def _last_payload(output: str) -> dict[str, object]:
    lines = [line for line in output.splitlines() if line.strip()]
    return json.loads(lines[-1])


def _planted_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path),
        hermes_api_key=_API_KEY,
        hermes_session_key=_SESSION_KEY,
        device_tokens_raw=f"{_DEVICE_A}, {_DEVICE_B}",
        **overrides,
    )


def test_planted_secrets_are_redacted_in_json_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _planted_settings(tmp_path)
    stream = _configure_and_capture(settings, monkeypatch)
    logger = _logger()
    logger.info(
        "message %s %s",
        _API_KEY,
        _SESSION_KEY,
        extra={
            "token_field": _DEVICE_A,
            "secret_obj": SecretStr(_SESSION_KEY),
            "headers": {
                "Authorization": f"Bearer {_DEVICE_B}",
                "X-Hermes-Session-Key": _SESSION_KEY,
            },
        },
    )
    output = stream.getvalue()

    for secret in (_DEVICE_A, _DEVICE_B, _API_KEY, _SESSION_KEY):
        assert secret not in output
    assert "***" in output

    payload = _last_payload(output)
    assert payload["message"] == "message *** ***"
    fields = payload["fields"]
    assert fields["token_field"] == "***"
    assert fields["secret_obj"] == "***"
    assert fields["headers"]["Authorization"] == "***"
    assert fields["headers"]["X-Hermes-Session-Key"] == "***"


def test_planted_secrets_are_redacted_in_text_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _planted_settings(tmp_path, log_format="text")
    stream = _configure_and_capture(settings, monkeypatch)
    headers = {"Authorization": f"Bearer {_DEVICE_B}"}
    _logger().info(
        "message %s %s headers=%s",
        _API_KEY,
        _SESSION_KEY,
        headers,
    )
    output = stream.getvalue()

    for secret in (_DEVICE_A, _DEVICE_B, _API_KEY, _SESSION_KEY):
        assert secret not in output
    assert "***" in output


def test_tracebacks_are_redacted_but_retained(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _planted_settings(tmp_path)
    stream = _configure_and_capture(settings, monkeypatch)
    logger = _logger()
    captured: RuntimeError | None = None
    try:
        raise RuntimeError(f"failed with {_API_KEY}", _DEVICE_A)
    except RuntimeError as exc:
        captured = exc
        logger.exception("boom %s", _SESSION_KEY)

    output = stream.getvalue()
    assert captured is not None
    assert _API_KEY in repr(captured)
    assert "Traceback" in output
    assert "RuntimeError" in output
    for secret in (_DEVICE_A, _API_KEY, _SESSION_KEY):
        assert secret not in output


def test_long_transcripts_are_truncated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _planted_settings(tmp_path, log_transcript_max_chars=60)
    stream = _configure_and_capture(settings, monkeypatch)
    transcript = "start " + ("x" * 400) + " END_MARKER"
    _logger().info(transcript)

    payload = _last_payload(stream.getvalue())
    message = str(payload["message"])
    assert "[truncated" in message
    assert "END_MARKER" not in message
    assert len(message) < 120


def test_paths_are_relative_to_data_dir_and_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _planted_settings(tmp_path)
    stream = _configure_and_capture(settings, monkeypatch)
    data_path = str(tmp_path / "inbox" / "x.pdf")
    home_path = str(_HOME / "notes.txt")
    _logger().info("paths %s %s", data_path, home_path)

    output = stream.getvalue()
    assert "inbox/x.pdf" in output
    assert "~/notes.txt" in output
    assert str(_HOME) not in output
    assert str(tmp_path) not in output
    assert redact_path(data_path, tmp_path) == "inbox/x.pdf"
    assert redact_path(home_path, tmp_path) == "~/notes.txt"


def test_audio_is_summarized_and_optional_recording(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    frame = b"\x00\x01" * 8
    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    stream = _configure_and_capture(settings, monkeypatch)
    _logger().info("frame", extra={"audio": frame})

    summary = describe_audio(frame)
    output = stream.getvalue()
    assert summary["bytes"] == len(frame)
    assert len(summary["sha256"]) == 16
    log_digest = hashlib.sha256(frame).hexdigest()[:12]
    assert f"<{len(frame)} bytes sha256={log_digest}>" in output
    assert "\\x00" not in output

    monkeypatch.setenv("RECORD_AUDIO", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    enabled = Settings(_env_file=None)
    assert enabled.record_audio is True
    with AudioRecorder(enabled, "session-1") as recorder:
        recorder.record_frame(frame)
    recordings = list((tmp_path / "recordings").glob("*.wav"))
    assert len(recordings) == 1
    assert recordings[0].read_bytes()[:4] == b"RIFF"
    assert not (_REPO_ROOT / "recordings").exists()

    disabled_dir = tmp_path / "disabled"
    disabled = Settings(_env_file=None, data_dir=str(disabled_dir), record_audio=False)
    with AudioRecorder(disabled, "session-2") as recorder:
        recorder.record_frame(frame)
    assert not (disabled_dir / "recordings").exists()


def test_audit_denied_approval_is_private_and_append_only(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    assert configure_audit(settings) is not None
    transcript = "the transcript text that must never be logged"
    digest = transcript_hash(transcript)
    emitted = audit_approval_decided(
        "prompt-1",
        2,
        "denied",
        transcript_sha256=digest,
        actor="phone",
    )
    assert emitted is not None

    path = tmp_path / "audit.log"
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    first = path.read_bytes()
    lines = first.decode("utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["prompt_id"] == "prompt-1"
    assert payload["execution_generation"] == 2
    assert payload["decision"] == "denied"
    assert payload["transcript_sha256"] == digest
    assert payload["ts"]
    assert payload["event"] == "approval.decided"
    assert transcript not in first.decode("utf-8")
    assert digest in first.decode("utf-8")

    audit_approval_decided("prompt-2", 3, "approved", transcript_sha256=digest)
    second = path.read_bytes()
    assert second.startswith(first)
    assert second != first


def test_audit_rotation_preserves_entries_and_honors_backup_cap(tmp_path: Path) -> None:
    sink = AuditLog(tmp_path / "audit.log", max_bytes=60, backup_count=2)
    sink.emit("test.one", payload="a" * 120)
    first_line = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()[0]
    sink.emit("test.two", payload="b" * 120)

    rotated = sorted(tmp_path.glob("audit.log.*"))
    assert rotated
    assert first_line in rotated[0].read_text(encoding="utf-8")
    assert "test.two" in (tmp_path / "audit.log").read_text(encoding="utf-8")

    for index in range(6):
        sink.emit(f"test.{index}", payload="c" * 120)
    rotated = sorted(tmp_path.glob("audit.log.*"))
    assert len(rotated) <= 2


def test_retention_age_pruning_keeps_active_files(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    active = logs / "relay.log"
    active.write_text("active\n", encoding="utf-8")
    old_rotated = logs / "relay.log.1"
    old_rotated.write_text("old\n", encoding="utf-8")
    audit_active = tmp_path / "audit.log"
    audit_active.write_text("audit\n", encoding="utf-8")
    old_audit = tmp_path / "audit.log.20260101T000000Z"
    old_audit.write_text("old audit\n", encoding="utf-8")

    old = datetime.now(UTC) - timedelta(days=30)
    for path in (active, old_rotated, audit_active, old_audit):
        os.utime(path, (old.timestamp(), old.timestamp()))

    policy = PrunePolicy(max_age_days=7, max_bytes=10_000, backup_count=5)
    actions = plan_prune([logs, tmp_path], policy)
    deleted = {str(action.path) for action in actions if action.action == "delete"}
    assert str(old_rotated) in deleted
    assert str(old_audit) in deleted
    assert str(active) not in deleted
    assert str(audit_active) not in deleted


def test_retention_size_pruning_rotates_audit_and_dry_run(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    active = logs / "relay.log"
    active.write_text("x" * 100, encoding="utf-8")
    oldest_rotated = logs / "relay.log.1"
    oldest_rotated.write_text("y" * 100, encoding="utf-8")
    newer_rotated = logs / "relay.log.2"
    newer_rotated.write_text("z" * 100, encoding="utf-8")
    audit_active = tmp_path / "audit.log"
    audit_active.write_text("a" * 500, encoding="utf-8")

    now = datetime.now(UTC)
    os.utime(oldest_rotated, ((now - timedelta(hours=3)).timestamp(),) * 2)
    os.utime(newer_rotated, ((now - timedelta(hours=2)).timestamp(),) * 2)
    os.utime(active, ((now - timedelta(hours=1)).timestamp(),) * 2)
    os.utime(audit_active, (now.timestamp(), now.timestamp()))

    policy = PrunePolicy(max_age_days=365, max_bytes=250, backup_count=5)
    actions = plan_prune([logs, tmp_path], policy, now=now)
    deletes = [action for action in actions if action.action == "delete"]
    rotates = [action for action in actions if action.action == "rotate"]
    assert any(action.path == oldest_rotated and action.reason == "size" for action in deletes)
    assert any(action.action == "rotate" and action.path == audit_active for action in rotates)
    assert not any(action.action == "delete" and action.path == audit_active for action in actions)

    dry = prune_logs([logs, tmp_path], policy, dry_run=True)
    assert str(oldest_rotated) in dry["deleted"]
    assert oldest_rotated.exists()
    assert audit_active.exists()

    report = prune_logs([logs, tmp_path], policy, dry_run=False)
    assert str(oldest_rotated) in report["deleted"]
    assert not oldest_rotated.exists()
    assert not audit_active.exists()
    rotated_audit = list(tmp_path.glob("audit.log.*"))
    assert rotated_audit
    assert rotated_audit[0].read_text(encoding="utf-8") == "a" * 500


def test_configure_logging_never_writes_inside_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_package = Path(__file__).resolve().parents[1]
    _remove_managed_handlers()
    configure_logging(Settings(_env_file=None, data_dir=str(repo_package)))
    assert not (repo_package / "logs").exists()
    assert not (repo_package / "audit.log").exists()
    logging.getLogger("relay.tests.guard").info("hello")
    assert not (repo_package / "logs").exists()

    stream = _configure_and_capture(
        Settings(_env_file=None, data_dir=str(tmp_path)),
        monkeypatch,
    )
    logging.getLogger("relay.tests.guard").info("hello")
    assert (tmp_path / "logs" / "relay.log").exists()
    assert stream.getvalue()
    assert not (repo_package / "logs").exists()


def test_foreign_handlers_are_hardened_too(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A sink installed before configure_logging must still see redacted text."""
    foreign = io.StringIO()
    handler = logging.StreamHandler(foreign)
    logging.getLogger().addHandler(handler)
    try:
        _configure_and_capture(_planted_settings(tmp_path), monkeypatch)
        _logger().info("query ?token=%s", _DEVICE_A)
        captured = foreign.getvalue()
        assert _DEVICE_A not in captured
        assert "***" in captured
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def test_auth_failure_audit_contains_only_a_prefix(tmp_path: Path) -> None:
    token = _DEVICE_A
    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    assert configure_audit(settings) is not None
    audit_auth_failure("invalid_token", token_prefix=token, device_label="phone")

    content = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert token not in content
    assert token[:8] in content
    assert token_prefix(token) == "devi…"


def test_unconfigured_audit_is_a_silent_noop() -> None:
    reset_audit()
    assert configure_audit(Settings(_env_file=None)) is None
    assert get_audit_log() is None
    assert audit("test.event", value="x") is None
    assert audit_approval_decided("prompt", 1, "denied") is None


def test_configure_logging_registers_every_device_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path),
        device_tokens_raw=f"{_DEVICE_A}, {_DEVICE_B}",
    )
    stream = _configure_and_capture(settings, monkeypatch)
    assert registered_secret_count() >= 2

    _logger().info("tokens %s %s", _DEVICE_A, _DEVICE_B)
    output = stream.getvalue()
    assert _DEVICE_A not in output
    assert _DEVICE_B not in output
    assert "***" in output


def test_redact_headers_and_sensitive_values() -> None:
    headers = redact_headers(
        {
            "Authorization": f"Bearer {_DEVICE_A}",
            "X-Hermes-Session-Key": _SESSION_KEY,
            "Content-Type": "application/json",
        }
    )
    assert headers["Authorization"] == "***"
    assert headers["X-Hermes-Session-Key"] == "***"
    assert headers["Content-Type"] == "application/json"
    assert redact_value("value", key="authorization") == "***"
    assert redact_value(SecretStr(_SESSION_KEY)) == "***"
    assert redact_value(b"\x00\x01").startswith("<2 bytes sha256=")


def test_record_audio_rejects_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECORD_AUDIO", "maybe")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

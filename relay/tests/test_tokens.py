"""Offline tests for the hash-at-rest device token lifecycle."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import pathlib
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from relay.app import create_app
from relay.auth.tokens import (
    DEFAULT_STATE_FILENAME,
    TOKEN_BYTES,
    TOKEN_ID_LENGTH,
    TOKEN_PREFIX,
    DeviceToken,
    TokenStateError,
    TokenStore,
    UnknownDeviceError,
    extract_bearer_token,
    generate_token,
    hash_token,
    is_well_formed,
    token_id_for,
)
from relay.config import Settings
from relay.logging import JsonFormatter

_BASE_URL = "http://relay.test"
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BOOTSTRAP = "bootstrap-token-placeholder"
_ISO = "2026-01-01T00:00:00+00:00"


def _state_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "state" / DEFAULT_STATE_FILENAME


def _write_state(path: pathlib.Path, entries: list[dict[str, object]]) -> None:
    path.write_text(json.dumps({"version": 1, "tokens": entries}), encoding="utf-8")


def _entry(token_id: str, label: str, digest: str) -> dict[str, object]:
    return {
        "token_id": token_id,
        "label": label,
        "issued_at": _ISO,
        "digest": digest,
        "revoked_at": None,
    }


async def _get(
    app: FastAPI,
    path: str,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=_BASE_URL,
        cookies=cookies,
    ) as client:
        return await client.get(path, headers=headers)


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def capture_json_logs() -> Iterator[_CaptureHandler]:
    handler = _CaptureHandler()
    handler.setFormatter(JsonFormatter())
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)


def test_generate_token_format() -> None:
    token = generate_token()
    assert token.startswith(TOKEN_PREFIX)
    payload = token[len(TOKEN_PREFIX) :]
    assert len(payload) == 43
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", payload)
    padded = payload + "=" * (-len(payload) % 4)
    assert len(base64.urlsafe_b64decode(padded)) == TOKEN_BYTES
    assert is_well_formed(token)
    assert len(token_id_for(token)) == TOKEN_ID_LENGTH


def test_issue_persists_hash_only_with_private_modes(tmp_path: pathlib.Path) -> None:
    path = _state_path(tmp_path)
    store = TokenStore(path)

    token, record = store.issue("phone")

    assert record.label == "phone"
    assert record.token_id == token_id_for(token)
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    raw = path.read_text(encoding="utf-8")
    assert hash_token(token) in raw
    assert record.label in raw
    assert token not in raw
    payload = json.loads(raw)
    assert payload["version"] == 1
    assert payload["tokens"][0]["token_id"] == record.token_id


def test_verify_accepts_only_well_formed_known_tokens(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TokenStore(_state_path(tmp_path))
    first, _ = store.issue("phone")
    second, _ = store.issue("tablet")

    calls = 0
    real_compare = hmac.compare_digest

    def counting_compare(left: str, right: str) -> bool:
        nonlocal calls
        calls += 1
        return real_compare(left, right)

    monkeypatch.setattr(hmac, "compare_digest", counting_compare)

    assert store.verify(first) is not None
    assert calls == 2
    calls = 0
    assert store.verify(second) is not None
    assert calls == 2

    calls = 0
    assert store.verify(generate_token()) is None
    assert calls == 2

    malformed = [
        "not-a-token",
        TOKEN_PREFIX + "a" * 42,
        TOKEN_PREFIX + "a" * 44,
        TOKEN_PREFIX + "a" * 42 + "!",
    ]
    for token in malformed:
        assert not is_well_formed(token)
        calls = 0
        assert store.verify(token) is None
        assert calls == 0


def test_revoke_is_visible_to_another_store_on_the_next_verify(
    tmp_path: pathlib.Path,
) -> None:
    path = _state_path(tmp_path)
    writer = TokenStore(path)
    doomed, _ = writer.issue("phone")
    sibling, _ = writer.issue("tablet")
    reader = TokenStore(path)

    writer.revoke(label="phone")

    assert reader.verify(doomed) is None
    assert reader.verify(sibling) is not None


def test_rotate_is_atomic_and_revokes_the_predecessor(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _state_path(tmp_path)
    store = TokenStore(path)
    old_token, old_record = store.issue("phone")

    replaces = 0
    real_replace = os.replace

    def counting_replace(source: object, target: object) -> None:
        nonlocal replaces
        replaces += 1
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", counting_replace)

    new_token, new_record = store.rotate(label="phone")

    assert replaces == 1
    assert store.verify(old_token) is None
    assert store.verify(new_token) is not None
    assert new_record.label == "phone"
    active = [record for record in store.active_tokens() if record.label == "phone"]
    assert len(active) == 1
    predecessor = next(
        record for record in store.all_tokens() if record.token_id == old_record.token_id
    )
    assert predecessor.revoked_at is not None
    leftovers = [
        item
        for item in path.parent.iterdir()
        if item.name.startswith(".") and item.name.endswith(".tmp")
    ]
    assert leftovers == []


def test_revoke_label_targets_active_successor_after_rotation(
    tmp_path: pathlib.Path,
) -> None:
    store = TokenStore(_state_path(tmp_path))
    _, predecessor = store.issue("phone")
    successor_token, successor = store.rotate(label="phone")

    revoked = store.revoke(label="phone")

    assert revoked.token_id == successor.token_id
    assert revoked.revoked_at is not None
    assert store.verify(successor_token) is None
    assert [record for record in store.active_tokens() if record.label == "phone"] == []
    stored_predecessor = next(
        record for record in store.all_tokens() if record.token_id == predecessor.token_id
    )
    assert stored_predecessor.revoked_at is not None


def test_revoke_fully_revoked_label_is_idempotent(
    tmp_path: pathlib.Path,
) -> None:
    store = TokenStore(_state_path(tmp_path))
    _, predecessor = store.issue("phone")
    store.rotate(label="phone")
    store.revoke(label="phone")

    again = store.revoke(label="phone")

    assert again.token_id == predecessor.token_id
    assert again.revoked_at is not None


def test_revoke_already_revoked_id_is_idempotent(
    tmp_path: pathlib.Path,
) -> None:
    store = TokenStore(_state_path(tmp_path))
    _, predecessor = store.issue("phone")
    store.rotate(label="phone")

    again = store.revoke(token_id=predecessor.token_id)

    assert again.token_id == predecessor.token_id
    assert again.revoked_at is not None


def test_failed_replace_leaves_previous_state_intact(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _state_path(tmp_path)
    store = TokenStore(path)
    token, _ = store.issue("phone")
    before = path.read_bytes()

    def boom(source: object, target: object) -> None:
        raise RuntimeError("replace failed")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(TokenStateError):
        store.issue("tablet")

    assert path.read_bytes() == before
    assert store.verify(token) is not None


def test_state_path_safety_mode_repair_and_fail_closed(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(TokenStateError, match="git work tree"):
        TokenStore(repo / DEFAULT_STATE_FILENAME)

    path = _state_path(tmp_path)
    store = TokenStore(path)
    token, record = store.issue("phone")
    path.chmod(0o644)

    caplog.set_level(logging.WARNING, logger="relay.auth.tokens")
    repaired = TokenStore(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert repaired.verify(token) is not None
    assert any(record.getMessage() == "auth.state_mode_repaired" for record in caplog.records)

    path.write_text("{not valid json", encoding="utf-8")
    corrupt = TokenStore(path, bootstrap_tokens=[_BOOTSTRAP])
    assert corrupt.verify(_BOOTSTRAP) is None
    assert corrupt.verify(generate_token()) is None

    duplicate = _state_path(tmp_path)
    _write_state(
        duplicate,
        [
            _entry("0123abcd", "a", hash_token("a")),
            _entry("0123abcd", "b", hash_token("b")),
        ],
    )
    with pytest.raises(TokenStateError, match="duplicate"):
        TokenStore(duplicate).reload()

    wrong_version = tmp_path / "wrong-version.json"
    wrong_version.write_text('{"version": 2, "tokens": []}', encoding="utf-8")
    with pytest.raises(TokenStateError, match="version"):
        TokenStore(wrong_version).reload()

    assert record.label == "phone"


def test_audit_lines_are_structured_and_never_contain_tokens(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
    capture_json_logs: _CaptureHandler,
) -> None:
    store = TokenStore(_state_path(tmp_path))
    caplog.set_level(logging.INFO, logger="relay.audit")

    first, first_record = store.issue("phone")
    second, second_record = store.rotate(label="phone")
    store.verify("not-a-token")
    store.verify(generate_token())
    store.revoke(label="phone")

    audit_records = [record for record in caplog.records if record.name == "relay.audit"]
    events = [record.__dict__["event"] for record in audit_records]
    assert events.count("auth.issue") == 1
    assert events.count("auth.rotate") == 1
    assert events.count("auth.revoke") == 1
    assert events.count("auth.failure") == 2
    failures = [
        record.__dict__ for record in audit_records if record.__dict__["event"] == "auth.failure"
    ]
    assert [failure["reason"] for failure in failures] == ["malformed_token", "unknown_token"]
    issued = next(record for record in audit_records if record.__dict__["event"] == "auth.issue")
    assert issued.__dict__["label"] == "phone"
    assert issued.__dict__["token_id"] == first_record.token_id
    rotated = next(record for record in audit_records if record.__dict__["event"] == "auth.rotate")
    assert rotated.__dict__["token_id"] == second_record.token_id
    assert rotated.__dict__["predecessor_token_id"] == first_record.token_id

    for token in (first, second):
        assert token not in caplog.text
        assert token not in "\n".join(capture_json_logs.lines)
        for record in audit_records:
            assert token not in str(record.__dict__.values())


def test_bootstrap_fallback_is_deprecated_and_disabled_by_state(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="relay.auth.tokens")
    bootstrap_only = TokenStore(None, bootstrap_tokens=[_BOOTSTRAP])

    record = bootstrap_only.verify(_BOOTSTRAP)
    assert record is not None
    assert record.label == "bootstrap"
    assert record.token_id == token_id_for(_BOOTSTRAP)
    assert bootstrap_only.verify(_BOOTSTRAP) is not None
    warnings = [
        item for item in caplog.records if item.getMessage() == "auth.bootstrap_tokens_active"
    ]
    assert len(warnings) == 1

    path = _state_path(tmp_path)
    store = TokenStore(path, bootstrap_tokens=[_BOOTSTRAP])
    token, _ = store.issue("phone")
    assert store.verify(_BOOTSTRAP) is None
    assert store.verify(token) is not None
    assert TokenStore(path, bootstrap_tokens=[_BOOTSTRAP]).verify(_BOOTSTRAP) is None


def test_http_auth_is_header_only_and_live(
    tmp_path: pathlib.Path,
    make_app: Callable[..., FastAPI],
) -> None:
    path = _state_path(tmp_path)
    writer = TokenStore(path)
    phone, _ = writer.issue("phone")
    tablet, _ = writer.issue("tablet")
    rotating, rotating_record = writer.issue("laptop")
    app = make_app(token_state_path=str(path))

    response = asyncio.run(_get(app, "/v1/whoami", headers={"Authorization": f"Bearer {phone}"}))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"label", "token_id", "issued_at"}
    assert body["label"] == "phone"
    assert body["token_id"] == token_id_for(phone)
    assert phone not in response.text

    health = asyncio.run(_get(app, "/health"))
    assert health.status_code == 200
    assert phone not in health.text
    assert tablet not in health.text

    missing = asyncio.run(_get(app, "/v1/whoami"))
    assert missing.status_code == 401
    assert missing.headers["WWW-Authenticate"] == "Bearer"
    assert missing.json() == {"error": "unauthorized", "reason": "missing_credentials"}

    wrong_scheme = asyncio.run(_get(app, "/v1/whoami", headers={"Authorization": f"Basic {phone}"}))
    assert wrong_scheme.status_code == 401
    assert wrong_scheme.json()["reason"] == "invalid_scheme"

    query_only = asyncio.run(_get(app, f"/v1/whoami?token={phone}"))
    assert query_only.status_code == 401
    assert query_only.json()["reason"] == "missing_credentials"

    cookie_only = asyncio.run(_get(app, "/v1/whoami", cookies={"token": phone}))
    assert cookie_only.status_code == 401
    assert cookie_only.json()["reason"] == "missing_credentials"

    unknown = asyncio.run(
        _get(app, "/v1/whoami", headers={"Authorization": f"Bearer {generate_token()}"})
    )
    assert unknown.status_code == 401
    assert unknown.json()["reason"] == "invalid_token"

    writer.revoke(label="phone")
    revoked = asyncio.run(_get(app, "/v1/whoami", headers={"Authorization": f"Bearer {phone}"}))
    assert revoked.status_code == 401
    assert revoked.json()["reason"] == "invalid_token"
    sibling = asyncio.run(_get(app, "/v1/whoami", headers={"Authorization": f"Bearer {tablet}"}))
    assert sibling.status_code == 200

    successor, _ = writer.rotate(label="laptop")
    predecessor = asyncio.run(
        _get(app, "/v1/whoami", headers={"Authorization": f"Bearer {rotating}"})
    )
    assert predecessor.status_code == 401
    rotated = asyncio.run(_get(app, "/v1/whoami", headers={"Authorization": f"Bearer {successor}"}))
    assert rotated.status_code == 200
    assert rotated.json()["label"] == rotating_record.label
    assert rotating not in rotated.text
    assert successor not in rotated.text


def test_query_token_is_never_logged_and_uvicorn_access_is_disabled(
    tmp_path: pathlib.Path,
) -> None:
    path = _state_path(tmp_path)
    token, _ = TokenStore(path).issue("phone")
    app = create_app(Settings(_env_file=None, token_state_path=str(path), log_level="DEBUG"))
    handler = _CaptureHandler()
    handler.setFormatter(JsonFormatter())
    handler.setLevel(logging.DEBUG)

    with TestClient(app) as client:
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            response = client.get(f"/v1/whoami?token={token}")
        finally:
            root.removeHandler(handler)

    assert response.status_code == 401
    assert response.json()["reason"] == "missing_credentials"
    assert logging.getLogger("uvicorn.access").disabled is True
    output = "\n".join(handler.lines)
    assert output
    assert token not in output


def test_ready_counts_state_file_tokens(
    tmp_path: pathlib.Path,
    make_app: Callable[..., FastAPI],
) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    path = _state_path(tmp_path)
    TokenStore(path).issue("phone")
    app = make_app(
        inbox_dir=str(inbox),
        outbox_dir=str(outbox),
        data_dir=str(tmp_path),
        token_state_path=str(path),
    )

    response = asyncio.run(_get(app, "/ready"))

    assert response.status_code == 200
    checks = {check["name"]: check for check in response.json()["checks"]}
    assert checks["auth"]["ok"] is True
    assert "1 active device token" in checks["auth"]["detail"]


def _cli_env(tmp_path: pathlib.Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "DATA_DIR",
        "TOKEN_STATE_PATH",
        "DEVICE_TOKENS",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "INBOX_DIR",
        "OUTBOX_DIR",
    ):
        env.pop(key, None)
    env["DATA_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "relay.tokens", *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_issue_list_revoke_and_rotate(tmp_path: pathlib.Path) -> None:
    env = _cli_env(tmp_path)

    issued = _run_cli(["issue", "--label", "phone"], env)
    assert issued.returncode == 0, issued.stderr
    token = issued.stdout.splitlines()[0].strip()
    assert is_well_formed(token)
    assert issued.stdout.count(token) == 1

    listed = _run_cli(["list"], env)
    assert listed.returncode == 0, listed.stderr
    assert token not in listed.stdout
    assert "phone" in listed.stdout
    assert token_id_for(token) in listed.stdout
    assert "active" in listed.stdout

    revoked = _run_cli(["revoke", "--label", "phone"], env)
    assert revoked.returncode == 0, revoked.stderr
    assert "phone" in revoked.stdout
    assert TokenStore(tmp_path / DEFAULT_STATE_FILENAME).verify(token) is None

    issued = _run_cli(["issue", "--label", "tablet"], env)
    assert issued.returncode == 0, issued.stderr
    predecessor = issued.stdout.splitlines()[0].strip()
    rotated = _run_cli(["rotate", "--label", "tablet"], env)
    assert rotated.returncode == 0, rotated.stderr
    successor = rotated.stdout.splitlines()[0].strip()
    assert successor != predecessor
    store = TokenStore(tmp_path / DEFAULT_STATE_FILENAME)
    assert store.verify(predecessor) is None
    verified = store.verify(successor)
    assert verified is not None
    assert verified.label == "tablet"


def test_cli_revoke_label_targets_active_successor_after_rotation(
    tmp_path: pathlib.Path,
) -> None:
    env = _cli_env(tmp_path)

    issued = _run_cli(["issue", "--label", "phone"], env)
    assert issued.returncode == 0, issued.stderr

    rotated = _run_cli(["rotate", "--label", "phone"], env)
    assert rotated.returncode == 0, rotated.stderr
    successor = rotated.stdout.splitlines()[0].strip()
    successor_id = token_id_for(successor)

    revoked = _run_cli(["revoke", "--label", "phone"], env)
    assert revoked.returncode == 0, revoked.stderr
    assert revoked.stdout.strip() == f"revoked label=phone token_id={successor_id}"
    assert TokenStore(tmp_path / DEFAULT_STATE_FILENAME).verify(successor) is None


def test_cli_requires_data_dir_or_state_path(tmp_path: pathlib.Path) -> None:
    env = _cli_env(tmp_path)
    env.pop("DATA_DIR", None)

    result = _run_cli(["list"], env)

    assert result.returncode != 0
    assert "DATA_DIR" in result.stderr
    assert "Traceback" not in result.stderr


def test_extract_bearer_token_is_strict() -> None:
    token = generate_token()
    assert extract_bearer_token(f"Bearer {token}") == token
    assert extract_bearer_token(f"bearer {token}") == token
    assert extract_bearer_token(f"Bearer  {token}") == token
    assert extract_bearer_token(f"Bearer {token} extra") is None
    assert extract_bearer_token(f"Basic {token}") is None
    assert extract_bearer_token("Bearer") is None
    assert extract_bearer_token("") is None
    assert extract_bearer_token(None) is None


def test_unknown_revoke_and_rotate_targets(tmp_path: pathlib.Path) -> None:
    store = TokenStore(_state_path(tmp_path))
    store.issue("phone")

    with pytest.raises(UnknownDeviceError):
        store.revoke(label="missing")
    with pytest.raises(UnknownDeviceError):
        store.rotate(token_id="deadbeef")
    assert isinstance(DeviceToken("deadbeef", "x", _ISO), DeviceToken)

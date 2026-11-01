"""In-process ASGI tests for the authenticated artifact endpoints.

Every test builds an app from explicit settings with the ``make_app`` fixture;
all data lives under ``tmp_path`` and requests go through ``TestClient`` without
sockets.  The repository tree is snapshotted to prove serving writes nothing.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_AUTH = {"Authorization": "Bearer test-token"}
_SECONDS_PER_DAY = 86400
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXCLUDED_REPO_PARTS = frozenset(
    {".git", ".venv", "build", "__pycache__", ".ruff_cache", ".pytest_cache"}
)


def _png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


def _app(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
    **overrides: Any,
) -> FastAPI:
    settings: dict[str, Any] = {
        "outbox_dir": tmp_path / "outbox",
        "device_tokens_raw": "test-token",
    }
    settings.update(overrides)
    return make_app(**settings)


def _write(
    root: Path,
    relative: str,
    payload: bytes,
    *,
    mtime: float | None = None,
) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _snapshot_repository() -> set[str]:
    files: set[str] = set()
    for path in _REPO_ROOT.rglob("*"):
        relative = path.relative_to(_REPO_ROOT)
        if any(part in _EXCLUDED_REPO_PARTS for part in relative.parts):
            continue
        if path.is_file():
            files.add(relative.as_posix())
    return files


def test_round_trip_list_and_download(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    root = tmp_path / "outbox"
    payload = _png_bytes()
    _write(root, "images/pic.png", payload)
    digest = hashlib.sha256(payload).hexdigest()

    with TestClient(app) as client:
        listing = client.get("/v1/artifacts", headers=_AUTH)
        download = client.get("/v1/artifacts/images/pic.png", headers=_AUTH)

    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["count"] == 1
    assert body["limit"] == 50
    assert body["retention_days"] == 0
    artifact = body["artifacts"][0]
    assert artifact["id"] == "images/pic.png"
    assert artifact["name"] == "pic.png"
    assert artifact["mime"] == "image/png"
    assert artifact["size_bytes"] == len(payload)
    assert artifact["sha256"] == digest
    assert artifact["expires_at"] is None
    assert "path" not in artifact
    assert str(tmp_path) not in listing.text

    assert download.status_code == 200
    assert download.content == payload
    assert download.headers["content-type"].startswith("image/png")
    assert download.headers["content-disposition"] == 'attachment; filename="pic.png"'
    assert download.headers["etag"] == f'"{digest}"'
    assert download.headers["x-artifact-sha256"] == digest
    assert download.headers["x-artifact-id"] == "images/pic.png"
    assert int(download.headers["content-length"]) == len(payload)
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert "last-modified" in download.headers
    assert "accept-ranges" not in download.headers


def test_list_and_download_require_a_device_token(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    _write(tmp_path / "outbox", "images/pic.png", _png_bytes())

    with TestClient(app) as client:
        missing_list = client.get("/v1/artifacts")
        missing_download = client.get("/v1/artifacts/images/pic.png")
        wrong = client.get(
            "/v1/artifacts",
            headers={"Authorization": "Bearer wrong-token"},
        )

    for response in (missing_list, missing_download, wrong):
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"
        assert response.json()["reason"]


def test_traversal_and_unknown_ids_are_not_found(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    root = tmp_path / "outbox"
    _write(root, "images/pic.png", _png_bytes())
    _write(root, ".hidden.txt", b"hidden")
    (tmp_path / "secret.txt").write_bytes(b"outside")

    with TestClient(app) as client:
        for url in (
            "/v1/artifacts/../secret.txt",
            "/v1/artifacts/%2E%2E/secret.txt",
            "/v1/artifacts/images/%2E%2E/%2E%2E/secret.txt",
            "/v1/artifacts/missing.txt",
            "/v1/artifacts/.hidden.txt",
        ):
            response = client.get(url, headers=_AUTH)
            assert response.status_code == 404, url


def test_symlink_escape_is_not_found(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    root = tmp_path / "outbox"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    (root / "escape.txt").symlink_to(outside)

    with TestClient(app) as client:
        response = client.get("/v1/artifacts/escape.txt", headers=_AUTH)

    assert response.status_code == 404


def test_expired_artifact_is_not_found_and_not_listed(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path, outbox_retention_days=1)
    root = tmp_path / "outbox"
    _write(root, "fresh.txt", b"fresh", mtime=time.time() - 3600)
    _write(root, "old.txt", b"old", mtime=time.time() - 2 * _SECONDS_PER_DAY)

    with TestClient(app) as client:
        listing = client.get("/v1/artifacts", headers=_AUTH)
        expired = client.get("/v1/artifacts/old.txt", headers=_AUTH)
        fresh = client.get("/v1/artifacts/fresh.txt", headers=_AUTH)

    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["artifacts"]] == ["fresh.txt"]
    assert expired.status_code == 404
    assert expired.json()["error"] == "artifact_expired"
    assert expired.json()["spoken"] == "That file has expired."
    assert fresh.status_code == 200
    assert fresh.content == b"fresh"


def test_unconfigured_outbox_returns_spoken_503(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app(outbox_dir=None, device_tokens_raw="test-token")
    with TestClient(app) as client:
        listing = client.get("/v1/artifacts", headers=_AUTH)
        download = client.get("/v1/artifacts/anything.txt", headers=_AUTH)

    assert app.state.outbox is None
    for response in (listing, download):
        assert response.status_code == 503
        assert response.json()["error"] == "outbox_not_configured"
        assert response.json()["spoken"] == "Files are not set up on this relay yet."


def test_outbox_inside_repository_returns_spoken_503(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app(outbox_dir=_REPO_ROOT, device_tokens_raw="test-token")
    with TestClient(app) as client:
        response = client.get("/v1/artifacts", headers=_AUTH)

    assert app.state.outbox is None
    assert response.status_code == 503
    assert response.json()["error"] == "outbox_not_configured"


def test_head_returns_headers_with_empty_body(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    payload = _png_bytes()
    _write(tmp_path / "outbox", "images/pic.png", payload)
    digest = hashlib.sha256(payload).hexdigest()

    with TestClient(app) as client:
        response = client.head("/v1/artifacts/images/pic.png", headers=_AUTH)

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["etag"] == f'"{digest}"'
    assert int(response.headers["content-length"]) == len(payload)
    assert response.headers["content-type"].startswith("image/png")


def test_if_none_match_returns_not_modified(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    _write(tmp_path / "outbox", "images/pic.png", _png_bytes())

    with TestClient(app) as client:
        first = client.get("/v1/artifacts/images/pic.png", headers=_AUTH)
        conditional = client.get(
            "/v1/artifacts/images/pic.png",
            headers={**_AUTH, "If-None-Match": first.headers["etag"]},
        )

    assert first.status_code == 200
    assert conditional.status_code == 304
    assert conditional.content == b""


def test_listing_excludes_hidden_and_expired_and_respects_limit(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path, outbox_retention_days=1)
    root = tmp_path / "outbox"
    now = time.time()
    _write(root, "one.txt", b"one", mtime=now - 10)
    _write(root, "two.txt", b"two", mtime=now - 20)
    _write(root, "three.txt", b"three", mtime=now - 30)
    _write(root, ".hidden.txt", b"hidden", mtime=now)
    _write(root, "expired.txt", b"expired", mtime=now - 2 * _SECONDS_PER_DAY)

    with TestClient(app) as client:
        response = client.get("/v1/artifacts?limit=1", headers=_AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["limit"] == 1
    assert body["retention_days"] == 1
    assert len(body["artifacts"]) == 1
    assert body["artifacts"][0]["id"] == "one.txt"


def test_limit_outside_the_range_is_rejected(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)

    with TestClient(app) as client:
        too_small = client.get("/v1/artifacts?limit=0", headers=_AUTH)
        too_large = client.get("/v1/artifacts?limit=201", headers=_AUTH)

    assert too_small.status_code == 422
    assert too_large.status_code == 422


def test_download_rate_limit_is_inherited(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path, rate_limit_download_burst=1)
    _write(tmp_path / "outbox", "images/pic.png", _png_bytes())

    with TestClient(app) as client:
        first = client.get("/v1/artifacts", headers=_AUTH)
        second = client.get("/v1/artifacts", headers=_AUTH)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"] == "rate_limited"


def test_serving_writes_nothing_inside_the_repository(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    _write(tmp_path / "outbox", "images/pic.png", _png_bytes())

    before = _snapshot_repository()
    with TestClient(app) as client:
        assert client.get("/v1/artifacts", headers=_AUTH).status_code == 200
        assert client.get("/v1/artifacts/images/pic.png", headers=_AUTH).status_code == 200
        assert client.head("/v1/artifacts/images/pic.png", headers=_AUTH).status_code == 200
    after = _snapshot_repository()

    assert before == after


@pytest.mark.parametrize("limit", [1, 50, 200])
def test_listing_accepts_valid_limits(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
    limit: int,
) -> None:
    app = _app(make_app, tmp_path)
    _write(tmp_path / "outbox", "images/pic.png", _png_bytes())

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts?limit={limit}", headers=_AUTH)

    assert response.status_code == 200
    assert response.json()["limit"] == limit

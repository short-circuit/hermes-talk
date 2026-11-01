"""Authenticated artifact listing and download endpoints.

Every request is authenticated with a device token.  Listing and serving are
read-only filesystem operations delegated to
:class:`relay.storage.outbox.OutboxStore`; this module makes no network call
beyond the HTTP client.  Errors carry a short ``spoken`` sentence so the phone
can read the reason aloud.

This module imports the auth dependency directly from ``relay.auth.deps`` and
never from ``relay.app``, so the app can include the router without an import
cycle.
"""

from __future__ import annotations

from email.utils import formatdate
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from relay.auth.deps import require_device_token
from relay.auth.tokens import DeviceToken
from relay.logging import audit
from relay.storage.outbox import (
    Artifact,
    ArtifactHandle,
    OutboxError,
    OutboxStore,
)

router = APIRouter()

_STREAM_CHUNK_BYTES = 64 * 1024
_DEFAULT_LIMIT = 50


def _store_for(request: Request) -> OutboxStore:
    """Return the app's outbox store or rebuild it to surface the reason."""
    store = getattr(request.app.state, "outbox", None)
    if store is not None:
        return store
    return OutboxStore(request.app.state.settings)


def _audit_served(artifact: Artifact) -> None:
    audit(
        "artifact.served",
        artifact_id=artifact.id,
        sha256=artifact.sha256,
        mime=artifact.mime,
        size_bytes=artifact.size_bytes,
    )


def _audit_rejected(artifact_id: str, exc: OutboxError) -> None:
    audit("artifact.rejected", artifact_id=artifact_id, reason=exc.code)


def _header_safe(value: str) -> str:
    """Keep header values ASCII; percent-encode anything else."""
    if value.isascii():
        return value
    return quote(value, safe="/")


def _content_disposition(name: str) -> str:
    """Build an attachment disposition with an RFC 5987 fallback when needed."""
    fallback = "".join(
        character
        if character.isascii() and character.isprintable() and character not in '"\\'
        else "_"
        for character in name
    )
    fallback = fallback or "artifact"
    disposition = f'attachment; filename="{fallback}"'
    if not name.isascii():
        disposition += f"; filename*=utf-8''{quote(name, safe='')}"
    return disposition


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        value = candidate.strip()
        if value == "*" or value == etag or value == f"W/{etag}":
            return True
    return False


def _download_headers(artifact: Artifact) -> dict[str, str]:
    etag = f'"{artifact.sha256}"'
    return {
        "Content-Type": artifact.mime,
        "Content-Length": str(artifact.size_bytes),
        "Content-Disposition": _content_disposition(artifact.name),
        "ETag": etag,
        "Last-Modified": formatdate(artifact.mtime_ns / 1_000_000_000, usegmt=True),
        "X-Artifact-SHA256": artifact.sha256,
        "X-Artifact-Id": _header_safe(artifact.id),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=0, must-revalidate",
    }


def _iter_handle(handle: ArtifactHandle):
    """Yield the open artifact in bounded chunks and close it on exit."""
    try:
        while True:
            chunk = handle.read_chunk(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            yield chunk
    finally:
        handle.close()


@router.get("/v1/artifacts")
def list_artifacts(
    request: Request,
    device: Annotated[DeviceToken, Depends(require_device_token)],
    limit: Annotated[int, Query(ge=1, le=200)] = _DEFAULT_LIMIT,
) -> JSONResponse:
    """List the unexpired artifacts visible to an authenticated device."""
    store = _store_for(request)
    artifacts = store.list_artifacts(limit=limit)
    return JSONResponse(
        {
            "artifacts": [artifact.to_public_dict() for artifact in artifacts],
            "count": len(artifacts),
            "limit": limit,
            "retention_days": store.retention_days,
        }
    )


@router.head("/v1/artifacts/{artifact_id:path}")
@router.get("/v1/artifacts/{artifact_id:path}")
def download_artifact(
    request: Request,
    artifact_id: str,
    device: Annotated[DeviceToken, Depends(require_device_token)],
) -> Response:
    """Stream one artifact, or return an authenticated conditional response."""
    try:
        store = _store_for(request)
        handle = store.open_artifact(artifact_id)
    except OutboxError as exc:
        _audit_rejected(artifact_id, exc)
        raise

    artifact = handle.artifact
    etag = f'"{artifact.sha256}"'
    headers = _download_headers(artifact)
    _audit_served(artifact)

    if _etag_matches(request.headers.get("if-none-match"), etag):
        handle.close()
        return Response(status_code=304, headers=headers)

    if request.method == "HEAD":
        handle.close()
        return Response(status_code=200, headers=headers)

    return StreamingResponse(
        _iter_handle(handle),
        status_code=200,
        headers=headers,
    )


__all__ = ["download_artifact", "list_artifacts", "router"]

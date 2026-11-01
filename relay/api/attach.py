"""HTTP attachment endpoints.

Uploads are authenticated with a device token, admitted through the upload
capacity lease, and handed to :class:`relay.storage.inbox.InboxStore`, which
performs validation, atomic storage, and audit logging.  This module never
imports ``relay.app``; it depends on ``relay.auth.deps`` directly so the app
can include the router without an import cycle.
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse

from relay.auth.deps import require_device_token
from relay.auth.tokens import DeviceToken
from relay.limits import LimitError
from relay.logging import audit_attachment
from relay.storage.inbox import (
    Attachment,
    FileTooLargeError,
    InboxConfigError,
    InboxError,
    InboxStore,
    UploadBusyError,
    sanitize_filename,
)

router = APIRouter()

_CAP_HEADROOM_BYTES = 1024 * 1024
_LEASE_POLL_SECONDS = 0.02
_SPOKEN_BY_KIND: dict[str, str] = {
    "image": "Picture saved.",
    "audio": "Audio saved.",
    "document": "Document saved.",
    "text": "Text file saved.",
}
_SPOKEN_FALLBACK = "File saved."


def _spoken_for(kind: str) -> str:
    return _SPOKEN_BY_KIND.get(kind, _SPOKEN_FALLBACK)


def _audit_rejection(
    exc: InboxError,
    *,
    filename: str,
    declared_content_type: str | None,
    size_bytes: int = 0,
) -> None:
    audit_attachment(
        False,
        sha256="",
        mime=declared_content_type or "",
        size_bytes=size_bytes,
        reason=exc.code,
        filename=sanitize_filename(filename),
    )


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _accept_upload(
    request: Request,
    file: UploadFile,
    device: DeviceToken,
) -> dict[str, object]:
    settings = request.app.state.settings
    filename = file.filename or "attachment"
    declared_content_type = file.content_type
    cap_bytes = settings.max_attachment_mb * 1024 * 1024

    # Cheap pre-check: refuse an obviously oversized body before parsing it.
    # The streaming cap inside InboxStore.ingest remains authoritative.
    declared_length = _declared_length(request)
    if declared_length is not None and declared_length > cap_bytes + _CAP_HEADROOM_BYTES:
        exc = FileTooLargeError(
            detail=(
                f"declared length {declared_length} bytes exceeds "
                f"the {settings.max_attachment_mb} MB limit"
            ),
        )
        _audit_rejection(
            exc,
            filename=filename,
            declared_content_type=declared_content_type,
            size_bytes=declared_length,
        )
        raise exc

    inbox: InboxStore | None = getattr(request.app.state, "inbox", None)
    if inbox is None:
        # The app kept booting without a store so /health stays live; rebuild
        # once here to surface the precise configuration reason to the client.
        try:
            InboxStore(settings)
        except InboxConfigError as exc:
            _audit_rejection(
                exc,
                filename=filename,
                declared_content_type=declared_content_type,
            )
            raise
        exc = InboxConfigError(detail="INBOX_DIR is not configured")
        _audit_rejection(
            exc,
            filename=filename,
            declared_content_type=declared_content_type,
        )
        raise exc

    limits = request.app.state.limits
    try:
        lease = limits.acquire_upload(device.token_id)
    except LimitError as exc:
        audit_attachment(
            False,
            sha256="",
            mime=declared_content_type or "",
            size_bytes=declared_length or 0,
            reason=exc.code,
            filename=sanitize_filename(filename),
        )
        raise

    try:
        if lease.waiting:
            deadline = time.monotonic() + settings.reject_retry_after_seconds
            while lease.waiting and time.monotonic() < deadline:
                lease.start()
                if not lease.waiting:
                    break
                await asyncio.sleep(_LEASE_POLL_SECONDS)
            if lease.waiting:
                exc = UploadBusyError(
                    retry_after=settings.reject_retry_after_seconds,
                )
                _audit_rejection(
                    exc,
                    filename=filename,
                    declared_content_type=declared_content_type,
                    size_bytes=declared_length or 0,
                )
                raise exc

        # InboxStore.ingest audits the accept/reject decision itself, using a
        # basename-only attachment id, hash, MIME type, and stored size.
        attachment: Attachment = await inbox.ingest(
            filename=filename,
            declared_content_type=declared_content_type,
            stream=file,
        )
    finally:
        lease.release()

    return {
        "attachment": attachment.to_public_dict(),
        "spoken": _spoken_for(attachment.kind),
    }


@router.post("/v1/attachments", status_code=201)
@router.put("/v1/attachments", status_code=201)
async def upload_attachment(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    device: Annotated[DeviceToken, Depends(require_device_token)],
) -> JSONResponse:
    """Accept one multipart attachment and return its stored record."""
    payload = await _accept_upload(request, file, device)
    return JSONResponse(payload, status_code=201)


__all__ = ["router", "upload_attachment"]

"""Attachment error hierarchy.

Every rejection carries a short, non-technical ``spoken`` sentence so the
client can read it aloud, plus a stable ``code`` for machines.  This module
imports nothing beyond the standard library and nothing from ``relay`` so the
error types can be shared by the parent process and the probe launcher without
creating an import cycle.
"""

from __future__ import annotations


class InboxError(Exception):
    """Base class for an attachment decision that maps to an HTTP error."""

    status_code: int = 500
    code: str = "inbox_error"
    spoken: str = "Something went wrong saving that file. Try again."
    detail: str = "attachment rejected"
    retry_after: int | None = None

    def __init__(
        self,
        *,
        detail: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        if detail is not None:
            self.detail = detail
        self.retry_after = retry_after
        super().__init__(self.detail)

    def to_body(self, correlation_id: str | None = None) -> dict[str, object]:
        """Render the client-facing error body; never contains raw bytes."""
        body: dict[str, object] = {
            "error": self.code,
            "detail": self.detail,
            "spoken": self.spoken,
            "retry_after": self.retry_after,
        }
        if correlation_id:
            body["correlation_id"] = correlation_id
        return body

    def headers(self) -> dict[str, str]:
        """Return HTTP headers, including ``Retry-After`` when a delay is known."""
        if self.retry_after is None:
            return {}
        return {"Retry-After": str(self.retry_after)}


class InboxConfigError(InboxError):
    """The inbox root is unset or lives inside the repository."""

    status_code = 503
    code = "inbox_not_configured"
    spoken = "Attachments are not set up on this relay yet."
    detail = "inbox_dir is not configured"


class InboxUnavailableError(InboxError):
    """The inbox root cannot be created or written to."""

    status_code = 503
    code = "inbox_unavailable"
    spoken = "The relay cannot save the file right now. Try again in a moment."
    detail = "the attachment inbox is unavailable"


class UploadBusyError(InboxError):
    """Every upload slot stayed occupied for the retry window."""

    status_code = 503
    code = "upload_busy"
    spoken = "The relay is busy saving another file. Try again in a moment."
    detail = "no upload slot became available"


class EmptyUploadError(InboxError):
    """The upload carried no bytes."""

    status_code = 400
    code = "empty_upload"
    spoken = "That file was empty, so there was nothing to save."
    detail = "the uploaded file is empty"


class UnsupportedTypeError(InboxError):
    """The magic bytes are not on the allowlist."""

    status_code = 415
    code = "unsupported_type"
    spoken = "That file type is not supported."
    detail = "the file type is not supported"


class TypeMismatchError(InboxError):
    """The declared extension does not match the sniffed type."""

    status_code = 415
    code = "type_mismatch"
    spoken = "The file's contents do not match its name."
    detail = "the file extension does not match its contents"


class FileTooLargeError(InboxError):
    """The upload exceeded the configured wire-size cap."""

    status_code = 413
    code = "file_too_large"
    spoken = "That file is too large to send. Try a smaller one."
    detail = "the file exceeds the size limit"


class CorruptMediaError(InboxError):
    """The sniffed image could not be decoded safely."""

    status_code = 422
    code = "corrupt_media"
    spoken = "That picture could not be read. Try another one."
    detail = "the image is corrupt or too large to decode"


class AttachmentNotFoundError(InboxError):
    """An attachment id is unknown or resolves outside the inbox."""

    status_code = 404
    code = "attachment_not_found"
    spoken = "That attachment could not be found."
    detail = "the attachment does not exist"


class ParseTimeoutError(InboxError):
    """The isolated parser exceeded its wall-clock budget."""

    status_code = 422
    code = "parse_timeout"
    spoken = "That file took too long to read. Try a smaller one."
    detail = "parsing the attachment exceeded the time limit"


class ImageTooLargeError(InboxError):
    """The image declares more pixels than the relay will decode."""

    status_code = 413
    code = "image_too_large"
    spoken = "That picture has too many pixels. Try a smaller one."
    detail = "the image exceeds the pixel limit"


class DocumentTooComplexError(InboxError):
    """The document has too many pages or too much extracted text."""

    status_code = 422
    code = "document_too_complex"
    spoken = "That document is too long. Try a shorter one."
    detail = "the document exceeds the page or text limit"


class EncryptedDocumentError(InboxError):
    """The document is password protected and was not decrypted."""

    status_code = 422
    code = "document_encrypted"
    spoken = "That document is locked with a password."
    detail = "the document is encrypted"


class CorruptDocumentError(InboxError):
    """The document structure could not be parsed."""

    status_code = 422
    code = "corrupt_document"
    spoken = "That document could not be read. Try another one."
    detail = "the document is corrupt"


class ArchiveTooComplexError(InboxError):
    """The archive declares too many entries, bytes, or compression ratio."""

    status_code = 413
    code = "archive_too_complex"
    spoken = "That zip file is too big to unpack. Try a smaller one."
    detail = "the archive exceeds the unpacking limits"


class ArchiveCorruptError(InboxError):
    """The archive structure could not be parsed."""

    status_code = 422
    code = "archive_corrupt"
    spoken = "That zip file could not be read. Try another one."
    detail = "the archive is corrupt"


class IntegrityError(InboxError):
    """The stored bytes no longer match the hash recorded at ingest."""

    status_code = 409
    code = "attachment_changed"
    spoken = "That file changed after it was saved. Try sending it again."
    detail = "the stored attachment no longer matches its recorded hash"


__all__ = [
    "ArchiveCorruptError",
    "ArchiveTooComplexError",
    "AttachmentNotFoundError",
    "CorruptDocumentError",
    "CorruptMediaError",
    "DocumentTooComplexError",
    "EmptyUploadError",
    "EncryptedDocumentError",
    "FileTooLargeError",
    "ImageTooLargeError",
    "InboxConfigError",
    "InboxError",
    "InboxUnavailableError",
    "IntegrityError",
    "ParseTimeoutError",
    "TypeMismatchError",
    "UnsupportedTypeError",
    "UploadBusyError",
]

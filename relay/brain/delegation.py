"""Provenance-envelope builder for untrusted content.

Attachments and tool output are handed to the model as data, not as prompt
text: the envelope carries the source kind, a sanitized name, the SHA-256 of
the original bytes or text, and a nonce.  The body is sanitized and truncated
with an explicit marker; path-not-text handoff keeps full documents on disk;
delimiter forgery is neutralised so a hostile body cannot close the envelope
early.  No network calls are made here.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from relay.brain.prompts import (
    ENVELOPE_CLOSE_TEMPLATE,
    ENVELOPE_OPEN_TEMPLATE,
    ENVELOPE_PREFIX,
    TRUNCATION_MARKER_TEMPLATE,
)
from relay.storage.inbox import Attachment, InboxStore, sanitize_filename

MAX_INLINE_EXCERPT_CHARS = 2000
MAX_TOOL_OUTPUT_CHARS = 1200
MAX_UTTERANCE_CHARS = 2000
ALLOWED_SPAN_KINDS = frozenset({"attachment", "document", "tool_output"})
_MAX_SPAN_NAME_CHARS = 64

_BOM = "\ufeff"
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f]")
_NEWLINE_RUN_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class Truncation:
    """One sanitized and possibly truncated untrusted body."""

    text: str
    truncated: bool
    shown_chars: int
    total_chars: int


@dataclass(frozen=True)
class UntrustedSpan:
    """One untrusted span and the provenance metadata attached to it."""

    kind: str
    name: str
    sha256: str
    text: str


def sanitize_untrusted_text(text: str) -> str:
    """Normalise untrusted text without interpreting any of its content."""
    if not isinstance(text, str):
        text = str(text)
    text = text.lstrip(_BOM)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS_RE.sub("", text)
    text = _NEWLINE_RUN_RE.sub("\n\n", text)
    lines = [line.rstrip(" ") for line in text.split("\n")]
    return "\n".join(lines).strip()


def sha256_text(text: str) -> str:
    """Return the hex SHA-256 of text encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def truncate_untrusted(text: str, limit: int) -> Truncation:
    """Sanitize text and truncate it with an explicit, visible marker."""
    if limit <= 0:
        msg = "limit must be positive"
        raise ValueError(msg)
    sanitized = sanitize_untrusted_text(text)
    total = len(sanitized)
    if total <= limit:
        return Truncation(
            text=sanitized,
            truncated=False,
            shown_chars=total,
            total_chars=total,
        )
    kept = sanitized[:limit]
    marker = TRUNCATION_MARKER_TEMPLATE.format(
        shown=f"{limit:,}",
        total=f"{total:,}",
    )
    return Truncation(
        text=f"{kept}\n{marker}",
        truncated=True,
        shown_chars=limit,
        total_chars=total,
    )


def envelope(span: UntrustedSpan, *, limit: int = MAX_INLINE_EXCERPT_CHARS) -> str:
    """Wrap one untrusted span in exactly one opening and one closing tag."""
    if span.kind not in ALLOWED_SPAN_KINDS:
        msg = f"unknown span kind: {span.kind!r}"
        raise ValueError(msg)
    name = sanitize_filename(span.name)[:_MAX_SPAN_NAME_CHARS]
    body = truncate_untrusted(span.text, limit).text
    base_nonce = sha256_text(span.text)[:12]
    nonce = base_nonce
    suffix = 0
    while True:
        open_tag = ENVELOPE_OPEN_TEMPLATE.format(
            kind=span.kind,
            name=name,
            sha256=span.sha256,
            nonce=nonce,
        )
        close_tag = ENVELOPE_CLOSE_TEMPLATE.format(nonce=nonce)
        collision = close_tag in body or open_tag in body
        if not collision and not (suffix == 0 and ENVELOPE_PREFIX in body):
            break
        suffix += 1
        nonce = f"{base_nonce}-{suffix}"
    return f"{open_tag}\n{body}\n{close_tag}"


def attachment_span(attachment: Attachment, excerpt: str | None = None) -> UntrustedSpan:
    """Build the provenance span for one stored attachment."""
    return UntrustedSpan(
        kind="attachment",
        name=sanitize_filename(attachment.original_name),
        sha256=attachment.sha256,
        text=excerpt or "",
    )


def document_span(name: str, text: str) -> UntrustedSpan:
    """Build the provenance span for a document body."""
    return UntrustedSpan(
        kind="document",
        name=name,
        sha256=sha256_text(text),
        text=text,
    )


def tool_output_span(tool: str, text: str) -> UntrustedSpan:
    """Build the provenance span for one tool's raw output."""
    return UntrustedSpan(
        kind="tool_output",
        name=tool,
        sha256=sha256_text(text),
        text=text,
    )


def tool_output_envelope(tool: str, text: str, *, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """Wrap raw tool output in a bounded provenance envelope."""
    return envelope(tool_output_span(tool, text), limit=limit)


def delegation_input(
    utterance: str,
    attachments: Sequence[Attachment],
    store: InboxStore,
    *,
    excerpts: Mapping[str, str] | None = None,
    inline_images: bool = True,
) -> list[dict[str, object]]:
    """Build the ``input`` content array for ``POST /v1/runs``.

    The user's spoken words come first.  Every attachment then contributes the
    trusted machine line from :meth:`InboxStore.delegation_parts` (plus its
    inline image when applicable), and only an explicit excerpt is wrapped in
    an untrusted envelope.  A document without an excerpt is handed over as a
    path only.
    """
    parts: list[dict[str, object]] = [
        {
            "type": "text",
            "text": truncate_untrusted(utterance, MAX_UTTERANCE_CHARS).text,
        }
    ]
    for attachment in attachments:
        parts.extend(store.delegation_parts([attachment], inline_images=inline_images))
        if excerpts is not None and attachment.id in excerpts:
            parts.append(
                {
                    "type": "text",
                    "text": envelope(attachment_span(attachment, excerpts[attachment.id])),
                }
            )
    return parts


__all__ = [
    "ALLOWED_SPAN_KINDS",
    "MAX_INLINE_EXCERPT_CHARS",
    "MAX_TOOL_OUTPUT_CHARS",
    "MAX_UTTERANCE_CHARS",
    "Truncation",
    "UntrustedSpan",
    "attachment_span",
    "delegation_input",
    "document_span",
    "envelope",
    "sanitize_untrusted_text",
    "sha256_text",
    "tool_output_envelope",
    "tool_output_span",
    "truncate_untrusted",
]

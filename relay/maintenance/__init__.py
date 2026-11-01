"""Offline data-retention helpers for the relay.

The package makes no network calls except the loopback Hermes session API on
``127.0.0.1``, used solely to delete expired ``api_server`` transcripts.  It
unlinks expired inbox attachments and outbox artifacts, audits every deletion
(path or session id, age, and reason — never file contents), and is safe to run
while a call is active because inbox claims are held with
:class:`~relay.maintenance.retention.ReferenceGuard`.
"""

from __future__ import annotations

from relay.maintenance.cli import main
from relay.maintenance.retention import (
    DEFAULT_RETENTION_DAYS,
    HermesUnavailableError,
    HttpSessionTransport,
    ReferenceGuard,
    RetentionConfigError,
    RetentionItem,
    RetentionPolicy,
    RetentionReport,
    SessionRecord,
    SessionTransport,
    prune_all,
    prune_inbox,
    prune_outbox,
    prune_sessions,
)

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "HermesUnavailableError",
    "HttpSessionTransport",
    "ReferenceGuard",
    "RetentionConfigError",
    "RetentionItem",
    "RetentionPolicy",
    "RetentionReport",
    "SessionRecord",
    "SessionTransport",
    "main",
    "prune_all",
    "prune_inbox",
    "prune_outbox",
    "prune_sessions",
]

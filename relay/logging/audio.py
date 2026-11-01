"""Optional audio debug recording plus a log-safe audio summary.

Raw audio bytes are never logged.  :func:`describe_audio` returns only a byte
count and a short SHA-256 prefix, which is what the redaction filter emits for
any bytes-like extra.
"""

from __future__ import annotations

import hashlib
import re
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relay.config import Settings
from relay.logging.redact import path_is_inside_repo

_SAMPLE_RATE = 16000
_CHANNELS = 1
_SAMPLE_WIDTH = 2
_SESSION_RE = re.compile(r"[^A-Za-z0-9_-]+")


def audio_debug_enabled(settings: Settings) -> bool:
    """Return whether optional audio debug recording is enabled."""
    return bool(settings.record_audio)


def describe_audio(payload: bytes | bytearray | memoryview | str) -> dict[str, Any]:
    """Return the only audio representation that may reach a log sink."""
    try:
        raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    except Exception:
        raw = b""
    return {
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest()[:16],
    }


def _safe_session(session_id: str) -> str:
    cleaned = _SESSION_RE.sub("-", str(session_id)).strip("-")
    return cleaned[:64] or "session"


class AudioRecorder:
    """Context manager that writes 16-bit mono PCM frames to a WAV file.

    The recorder is a no-op when ``RECORD_AUDIO`` is off, ``DATA_DIR`` is
    unset, or the recordings directory resolves inside the repository.
    """

    def __init__(
        self,
        settings: Settings,
        session_id: str,
        *,
        sample_rate: int = _SAMPLE_RATE,
    ) -> None:
        self.settings = settings
        self.session_id = _safe_session(session_id)
        self.sample_rate = sample_rate
        self._wave: wave.Wave_write | None = None
        self.path: Path | None = None
        recordings_dir = settings.recordings_dir
        self._enabled = (
            audio_debug_enabled(settings)
            and recordings_dir is not None
            and not path_is_inside_repo(recordings_dir)
        )

    def __enter__(self) -> AudioRecorder:
        if not self._enabled:
            return self
        recordings_dir = self.settings.recordings_dir
        if recordings_dir is None:
            return self
        recordings_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.path = recordings_dir / f"{self.session_id}-{stamp}.wav"
        self._wave = wave.open(str(self.path), "wb")
        self._wave.setnchannels(_CHANNELS)
        self._wave.setsampwidth(_SAMPLE_WIDTH)
        self._wave.setframerate(self.sample_rate)
        return self

    def record_frame(self, frame: bytes) -> None:
        """Append one 16-bit mono PCM frame; no-op when disabled."""
        if self._wave is None:
            return
        self._wave.writeframes(bytes(frame))

    def close(self) -> None:
        """Finalise the WAV header and close the file."""
        if self._wave is None:
            return
        try:
            self._wave.close()
        finally:
            self._wave = None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.close()
        return False

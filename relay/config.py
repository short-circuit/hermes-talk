"""Typed runtime configuration for the voice relay.

All values have non-secret defaults so the service can boot with an empty
environment.  Secrets are held in :class:`pydantic.SecretStr` so they are
masked in reprs and logs.  Nothing in this module touches the network.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from relay.auth.tokens import DEFAULT_STATE_FILENAME

_ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_ALLOWED_LOG_FORMATS = frozenset({"json", "text"})
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
_ALLOWED_STT_ENGINES = frozenset({"sherpa", "whisper"})
_ALLOWED_TTS_ENGINES = frozenset({"kokoro"})


class Settings(BaseSettings):
    """Relay settings loaded from the environment and an optional ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    relay_host: str = "127.0.0.1"
    relay_port: int = 8790
    hermes_base_url: str = "http://127.0.0.1:8642"
    hermes_api_key: SecretStr | None = None
    hermes_session_key: SecretStr | None = None
    localai_base_url: str = "http://127.0.0.1:8081"
    voice_brain_base_url: str = "http://127.0.0.1:8080"
    voice_brain_model: str = "lfm3-1.7b"
    voice_brain_temperature: float = 0.1
    voice_brain_max_tokens: int = 160
    voice_brain_tool_choice: str = "required"
    voice_brain_deferred_tools: bool = False
    voice_brain_first_token_timeout_s: float = 2.0
    voice_brain_request_timeout_s: float = 20.0
    voice_brain_max_utterance_chars: int = 2000
    stt_model: str = "whisper-1"
    tts_model: str = "kokoro"
    vad_model: str = "silero-vad"
    device_tokens_raw: str = Field(default="", validation_alias="DEVICE_TOKENS")
    inbox_dir: Path | None = None
    outbox_dir: Path | None = None
    data_dir: Path | None = None
    token_state_path: Path | None = None
    max_attachment_mb: int = 50
    max_call_minutes: int = 60
    log_level: str = "INFO"
    log_format: str = "json"
    log_transcript_max_chars: int = 280
    log_max_age_days: int = 14
    log_max_bytes: int = 10485760
    log_backup_count: int = 5
    audit_max_age_days: int = 90
    audit_max_bytes: int = 5242880
    audit_backup_count: int = 10
    approval_confirmation_phrase: str = "yes run it"
    approval_min_confidence: float = 0.6
    approval_max_reasks: int = 1
    approval_echo_guard: bool = True
    approval_timeout_s: float = 20.0
    record_audio: bool = False
    shutdown_timeout_seconds: float = 10.0
    max_concurrent_sessions: int = 3
    max_concurrent_runs: int = 2
    max_concurrent_runs_per_device: int = 1
    max_concurrent_uploads: int = 2
    max_queued_uploads: int = 8
    session_idle_timeout_seconds: float = 120.0
    session_reaper_interval_seconds: float = 5.0
    rate_limit_run_per_minute: int = 20
    rate_limit_run_burst: int = 5
    rate_limit_upload_per_minute: int = 30
    rate_limit_upload_burst: int = 10
    rate_limit_download_per_minute: int = 120
    rate_limit_download_burst: int = 30
    rate_limit_max_devices: int = 64
    max_sse_frame_bytes: int = 65536
    sse_idle_timeout_seconds: float = 30.0
    sse_max_reconnect_attempts: int = 5
    barge_in_min_speech_ms: int = 300
    barge_in_cooldown_ms: int = 1000
    reject_retry_after_seconds: int = 2
    stt_engine: str = "sherpa"
    stt_model_dir: Path | None = None
    stt_num_threads: int = 4
    stt_partial_interval_ms: int = 120
    stt_flush_padding_ms: int = 480
    whisper_model_dir: Path | None = None
    whisper_chunk_ms: int = 2000
    tts_engine: str = "kokoro"
    tts_model_path: Path | None = None
    tts_voices_path: Path | None = None
    tts_voice: str = "af_sarah"
    tts_speed: float = 1.0
    tts_max_pending_sentences: int = 3
    tts_first_segment_max_chars: int = 20
    opus_bit_rate: int = 32000
    speech_max_buffer_bytes: int = 480000
    speech_max_buffer_frames: int = 250
    speech_backpressure_timeout_s: float = 2.0
    inbox_retention_days: int = 0
    outbox_retention_days: int = 0
    transcript_retention_days: int = 0

    @field_validator("relay_port")
    @classmethod
    def _validate_relay_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            msg = "relay_port must be between 1 and 65535"
            raise ValueError(msg)
        return value

    @field_validator("max_attachment_mb", "max_call_minutes")
    @classmethod
    def _validate_positive_limits(cls, value: int) -> int:
        if value <= 0:
            msg = "limit values must be greater than zero"
            raise ValueError(msg)
        return value

    @field_validator(
        "log_transcript_max_chars",
        "log_max_age_days",
        "log_max_bytes",
        "audit_max_age_days",
        "audit_max_bytes",
    )
    @classmethod
    def _validate_positive_log_limits(cls, value: int) -> int:
        if value <= 0:
            msg = "logging and audit limits must be greater than zero"
            raise ValueError(msg)
        return value

    @field_validator("log_backup_count", "audit_backup_count")
    @classmethod
    def _validate_backup_counts(cls, value: int) -> int:
        if value < 1:
            msg = "backup counts must be at least one"
            raise ValueError(msg)
        return value

    @field_validator("record_audio", mode="before")
    @classmethod
    def _validate_record_audio(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        msg = "record_audio must be one of 1/0, true/false, yes/no, on/off"
        raise ValueError(msg)

    @field_validator("approval_min_confidence")
    @classmethod
    def _validate_approval_min_confidence(cls, value: float) -> float:
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            msg = "approval_min_confidence must be finite and between 0.0 and 1.0"
            raise ValueError(msg)
        return value

    @field_validator("approval_max_reasks")
    @classmethod
    def _validate_approval_max_reasks(cls, value: int) -> int:
        if value < 0:
            msg = "approval_max_reasks must be zero or greater"
            raise ValueError(msg)
        return value

    @field_validator("approval_echo_guard", mode="before")
    @classmethod
    def _validate_approval_echo_guard(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        msg = "approval_echo_guard must be one of 1/0, true/false, yes/no, on/off"
        raise ValueError(msg)

    @field_validator("approval_timeout_s")
    @classmethod
    def _validate_approval_timeout_s(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            msg = "approval_timeout_s must be finite and greater than zero"
            raise ValueError(msg)
        return value

    @field_validator("shutdown_timeout_seconds")
    @classmethod
    def _validate_shutdown_timeout(cls, value: float) -> float:
        if value < 0:
            msg = "shutdown_timeout_seconds must be zero or greater"
            raise ValueError(msg)
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in _ALLOWED_LOG_LEVELS:
            msg = f"log_level must be one of {sorted(_ALLOWED_LOG_LEVELS)}"
            raise ValueError(msg)
        return normalized

    @field_validator("log_format")
    @classmethod
    def _validate_log_format(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _ALLOWED_LOG_FORMATS:
            msg = f"log_format must be one of {sorted(_ALLOWED_LOG_FORMATS)}"
            raise ValueError(msg)
        return normalized

    @field_validator("hermes_base_url", "localai_base_url", "voice_brain_base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES or not parsed.hostname:
            msg = "base URL must be an http(s) URL with a host"
            raise ValueError(msg)
        return value

    @field_validator(
        "inbox_dir",
        "outbox_dir",
        "data_dir",
        "token_state_path",
        "stt_model_dir",
        "whisper_model_dir",
        "tts_model_path",
        "tts_voices_path",
        mode="before",
    )
    @classmethod
    def _validate_optional_path(cls, value: object) -> Path | None:
        if value is None:
            return None
        if isinstance(value, Path):
            return value
        raw = str(value).strip()
        if not raw:
            return None
        return Path(raw).expanduser()

    @field_validator(
        "max_concurrent_sessions",
        "max_concurrent_runs",
        "max_concurrent_runs_per_device",
        "max_concurrent_uploads",
        "max_queued_uploads",
        "rate_limit_run_per_minute",
        "rate_limit_run_burst",
        "rate_limit_upload_per_minute",
        "rate_limit_upload_burst",
        "rate_limit_download_per_minute",
        "rate_limit_download_burst",
        "rate_limit_max_devices",
        "max_sse_frame_bytes",
        "sse_max_reconnect_attempts",
        "reject_retry_after_seconds",
    )
    @classmethod
    def _validate_positive_admission_limits(cls, value: int) -> int:
        if value <= 0:
            msg = "admission control limits must be greater than zero"
            raise ValueError(msg)
        return value

    @field_validator("barge_in_cooldown_ms")
    @classmethod
    def _validate_barge_in_cooldown(cls, value: int) -> int:
        if value < 0:
            msg = "barge_in_cooldown_ms must be zero or greater"
            raise ValueError(msg)
        return value

    @field_validator("barge_in_min_speech_ms")
    @classmethod
    def _validate_barge_in_min_speech(cls, value: int) -> int:
        if value < 1:
            msg = "barge_in_min_speech_ms must be at least one"
            raise ValueError(msg)
        return value

    @field_validator(
        "session_idle_timeout_seconds",
        "session_reaper_interval_seconds",
        "sse_idle_timeout_seconds",
    )
    @classmethod
    def _validate_positive_admission_floats(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            msg = "admission control timeouts must be finite and greater than zero"
            raise ValueError(msg)
        return value

    @field_validator("stt_engine")
    @classmethod
    def _validate_stt_engine(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _ALLOWED_STT_ENGINES:
            msg = f"stt_engine must be one of {sorted(_ALLOWED_STT_ENGINES)}"
            raise ValueError(msg)
        return normalized

    @field_validator("tts_engine")
    @classmethod
    def _validate_tts_engine(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _ALLOWED_TTS_ENGINES:
            msg = f"tts_engine must be one of {sorted(_ALLOWED_TTS_ENGINES)}"
            raise ValueError(msg)
        return normalized

    @field_validator("tts_voice")
    @classmethod
    def _validate_tts_voice(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            msg = "tts_voice must not be empty"
            raise ValueError(msg)
        return normalized

    @field_validator("stt_num_threads")
    @classmethod
    def _validate_stt_num_threads(cls, value: int) -> int:
        if not 1 <= value <= 64:
            msg = "stt_num_threads must be between 1 and 64"
            raise ValueError(msg)
        return value

    @field_validator("stt_partial_interval_ms")
    @classmethod
    def _validate_stt_partial_interval(cls, value: int) -> int:
        if not 20 <= value <= 2000:
            msg = "stt_partial_interval_ms must be between 20 and 2000"
            raise ValueError(msg)
        return value

    @field_validator("stt_flush_padding_ms")
    @classmethod
    def _validate_stt_flush_padding(cls, value: int) -> int:
        if not 0 <= value <= 1000:
            msg = "stt_flush_padding_ms must be between 0 and 1000"
            raise ValueError(msg)
        return value

    @field_validator("whisper_chunk_ms")
    @classmethod
    def _validate_whisper_chunk_ms(cls, value: int) -> int:
        if not 500 <= value <= 30000:
            msg = "whisper_chunk_ms must be between 500 and 30000"
            raise ValueError(msg)
        return value

    @field_validator("tts_speed")
    @classmethod
    def _validate_tts_speed(cls, value: float) -> float:
        if not math.isfinite(value) or not 0.5 <= value <= 2.0:
            msg = "tts_speed must be finite and between 0.5 and 2.0"
            raise ValueError(msg)
        return value

    @field_validator("tts_max_pending_sentences")
    @classmethod
    def _validate_tts_max_pending_sentences(cls, value: int) -> int:
        if not 1 <= value <= 16:
            msg = "tts_max_pending_sentences must be between 1 and 16"
            raise ValueError(msg)
        return value

    @field_validator("tts_first_segment_max_chars")
    @classmethod
    def _validate_tts_first_segment_max_chars(cls, value: int) -> int:
        if not 4 <= value <= 400:
            msg = "tts_first_segment_max_chars must be between 4 and 400"
            raise ValueError(msg)
        return value

    @field_validator("opus_bit_rate")
    @classmethod
    def _validate_opus_bit_rate(cls, value: int) -> int:
        if not 6000 <= value <= 510000:
            msg = "opus_bit_rate must be between 6000 and 510000"
            raise ValueError(msg)
        return value

    @field_validator("speech_max_buffer_bytes", "speech_max_buffer_frames")
    @classmethod
    def _validate_speech_buffer_limits(cls, value: int) -> int:
        if value <= 0:
            msg = "speech buffer limits must be greater than zero"
            raise ValueError(msg)
        return value

    @field_validator("speech_backpressure_timeout_s")
    @classmethod
    def _validate_speech_backpressure_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            msg = "speech_backpressure_timeout_s must be finite and zero or greater"
            raise ValueError(msg)
        return value

    @field_validator(
        "inbox_retention_days",
        "outbox_retention_days",
        "transcript_retention_days",
    )
    @classmethod
    def _validate_retention_days(cls, value: int) -> int:
        if value < 0:
            msg = "retention days must be zero or greater (0 keeps data until manual delete)"
            raise ValueError(msg)
        return value

    @field_validator("voice_brain_temperature")
    @classmethod
    def _validate_voice_brain_temperature(cls, value: float) -> float:
        if not math.isfinite(value) or not 0.0 <= value <= 2.0:
            msg = "voice_brain_temperature must be finite and between 0.0 and 2.0"
            raise ValueError(msg)
        return value

    @field_validator("voice_brain_max_tokens")
    @classmethod
    def _validate_voice_brain_max_tokens(cls, value: int) -> int:
        if not 1 <= value <= 4096:
            msg = "voice_brain_max_tokens must be between 1 and 4096"
            raise ValueError(msg)
        return value

    @field_validator("voice_brain_tool_choice")
    @classmethod
    def _validate_voice_brain_tool_choice(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"required", "auto"}:
            msg = "voice_brain_tool_choice must be 'required' or 'auto'"
            raise ValueError(msg)
        return normalized

    @field_validator("voice_brain_deferred_tools", mode="before")
    @classmethod
    def _validate_voice_brain_deferred_tools(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        msg = "voice_brain_deferred_tools must be one of 1/0, true/false, yes/no, on/off"
        raise ValueError(msg)

    @field_validator(
        "voice_brain_first_token_timeout_s",
        "voice_brain_request_timeout_s",
    )
    @classmethod
    def _validate_voice_brain_timeouts(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            msg = "voice_brain timeouts must be finite and greater than zero"
            raise ValueError(msg)
        return value

    @field_validator("voice_brain_max_utterance_chars")
    @classmethod
    def _validate_voice_brain_max_utterance_chars(cls, value: int) -> int:
        if value < 1:
            msg = "voice_brain_max_utterance_chars must be at least one"
            raise ValueError(msg)
        return value

    @property
    def device_tokens(self) -> tuple[SecretStr, ...]:
        """Return configured device tokens as secrets.

        ``DEVICE_TOKENS`` is a comma-separated list.  Empty entries are
        ignored; an input made only of blank entries and duplicate tokens are
        rejected.
        """
        raw = self.device_tokens_raw.strip()
        if not raw:
            return ()
        entries = [entry.strip() for entry in raw.split(",")]
        tokens = [entry for entry in entries if entry]
        if not tokens:
            msg = "DEVICE_TOKENS must contain at least one non-blank token"
            raise ValueError(msg)
        if len(set(tokens)) != len(tokens):
            msg = "DEVICE_TOKENS must not contain duplicate tokens"
            raise ValueError(msg)
        return tuple(SecretStr(token) for token in tokens)

    @property
    def configured_device_token_count(self) -> int:
        """Number of device tokens currently configured."""
        return len(self.device_tokens)

    def device_token_state_path(self) -> Path | None:
        """Return the configured token state path, if any.

        ``TOKEN_STATE_PATH`` wins; otherwise the default lives under
        ``DATA_DIR``.  With neither set there is no state file.
        """
        if self.token_state_path is not None:
            return self.token_state_path
        if self.data_dir is not None:
            return self.data_dir / DEFAULT_STATE_FILENAME
        return None

    @property
    def log_dir(self) -> Path | None:
        """Directory for relay logs, or ``None`` when DATA_DIR is unset."""
        if self.data_dir is None:
            return None
        return self.data_dir / "logs"

    @property
    def recordings_dir(self) -> Path | None:
        """Directory for optional audio debug recordings."""
        if self.data_dir is None:
            return None
        return self.data_dir / "recordings"

    @property
    def audit_log_path(self) -> Path | None:
        """Append-only audit log path, or ``None`` when DATA_DIR is unset."""
        if self.data_dir is None:
            return None
        return self.data_dir / "audit.log"

    def storage_paths(self) -> tuple[Path, ...]:
        """Configured storage directories, excluding unset entries."""
        paths = (self.inbox_dir, self.outbox_dir, self.data_dir)
        return tuple(path for path in paths if path is not None)

    def missing_required_settings(self) -> list[str]:
        """Sorted names of required settings that are unset or blank."""
        missing: list[str] = []
        if not self.hermes_base_url.strip():
            missing.append("hermes_base_url")
        if self.inbox_dir is None:
            missing.append("inbox_dir")
        if self.outbox_dir is None:
            missing.append("outbox_dir")
        try:
            has_device_tokens = self.configured_device_token_count > 0
        except ValueError:
            has_device_tokens = False
        if not has_device_tokens:
            missing.append("device_tokens")
        return sorted(missing)

    @classmethod
    def load(cls) -> Settings:
        """Return a fresh settings instance from the current environment."""
        return cls()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (intended for tests)."""
    get_settings.cache_clear()

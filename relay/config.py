"""Typed runtime configuration for the voice relay.

All values have non-secret defaults so the service can boot with an empty
environment.  Secrets are held in :class:`pydantic.SecretStr` so they are
masked in reprs and logs.  Nothing in this module touches the network.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_ALLOWED_LOG_FORMATS = frozenset({"json", "text"})
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


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
    localai_base_url: str = "http://127.0.0.1:8081"
    voice_brain_model: str = "lfm3-audio-2b"
    stt_model: str = "whisper-1"
    tts_model: str = "kokoro"
    vad_model: str = "silero-vad"
    device_tokens_raw: str = Field(default="", validation_alias="DEVICE_TOKENS")
    inbox_dir: Path | None = None
    outbox_dir: Path | None = None
    data_dir: Path | None = None
    max_attachment_mb: int = 50
    max_call_minutes: int = 60
    log_level: str = "INFO"
    log_format: str = "json"
    shutdown_timeout_seconds: float = 10.0

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

    @field_validator("hermes_base_url", "localai_base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES or not parsed.hostname:
            msg = "base URL must be an http(s) URL with a host"
            raise ValueError(msg)
        return value

    @field_validator("inbox_dir", "outbox_dir", "data_dir", mode="before")
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

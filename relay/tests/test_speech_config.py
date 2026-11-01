"""Offline tests for the speech settings added to relay.config."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from relay.config import Settings

_SPEECH_ENV_KEYS = (
    "STT_ENGINE",
    "STT_MODEL_DIR",
    "STT_NUM_THREADS",
    "STT_PARTIAL_INTERVAL_MS",
    "STT_FLUSH_PADDING_MS",
    "WHISPER_MODEL_DIR",
    "WHISPER_CHUNK_MS",
    "TTS_ENGINE",
    "TTS_MODEL_PATH",
    "TTS_VOICES_PATH",
    "TTS_VOICE",
    "TTS_SPEED",
    "TTS_MAX_PENDING_SENTENCES",
    "TTS_FIRST_SEGMENT_MAX_CHARS",
    "OPUS_BIT_RATE",
    "SPEECH_MAX_BUFFER_BYTES",
    "SPEECH_MAX_BUFFER_FRAMES",
    "SPEECH_BACKPRESSURE_TIMEOUT_S",
)


@pytest.fixture(autouse=True)
def _clear_speech_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _SPEECH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_speech_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.stt_engine == "sherpa"
    assert settings.stt_model_dir is None
    assert settings.stt_num_threads == 4
    assert settings.stt_partial_interval_ms == 120
    assert settings.stt_flush_padding_ms == 480
    assert settings.whisper_model_dir is None
    assert settings.whisper_chunk_ms == 2000
    assert settings.tts_engine == "kokoro"
    assert settings.tts_model_path is None
    assert settings.tts_voices_path is None
    assert settings.tts_voice == "af_sarah"
    assert settings.tts_speed == 1.0
    assert settings.tts_max_pending_sentences == 3
    assert settings.tts_first_segment_max_chars == 20
    assert settings.opus_bit_rate == 32000
    assert settings.speech_max_buffer_bytes == 480000
    assert settings.speech_max_buffer_frames == 250
    assert settings.speech_backpressure_timeout_s == 2.0


@pytest.mark.parametrize("field_name", ["stt_engine", "tts_engine"])
def test_engine_enum_is_rejected(field_name: str) -> None:
    with pytest.raises(ValidationError, match=field_name):
        Settings(_env_file=None, **{field_name: "cloud"})


def test_engine_values_are_normalized() -> None:
    settings = Settings(_env_file=None, stt_engine=" SHERPA ", tts_engine=" Kokoro ")
    assert settings.stt_engine == "sherpa"
    assert settings.tts_engine == "kokoro"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("stt_num_threads", 0),
        ("stt_num_threads", 65),
        ("stt_partial_interval_ms", 19),
        ("stt_partial_interval_ms", 2001),
        ("stt_flush_padding_ms", -1),
        ("stt_flush_padding_ms", 1001),
        ("whisper_chunk_ms", 499),
        ("whisper_chunk_ms", 30001),
        ("tts_speed", 0.49),
        ("tts_speed", 2.01),
        ("tts_max_pending_sentences", 0),
        ("tts_max_pending_sentences", 17),
        ("tts_first_segment_max_chars", 3),
        ("tts_first_segment_max_chars", 401),
        ("opus_bit_rate", 5999),
        ("opus_bit_rate", 510001),
        ("speech_max_buffer_bytes", 0),
        ("speech_max_buffer_frames", 0),
        ("speech_backpressure_timeout_s", -0.1),
    ],
)
def test_speech_bounds_are_enforced(field_name: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field_name: value})


def test_speech_bounds_accept_edges() -> None:
    settings = Settings(
        _env_file=None,
        stt_num_threads=64,
        stt_partial_interval_ms=2000,
        stt_flush_padding_ms=1000,
        whisper_chunk_ms=30000,
        tts_speed=2.0,
        tts_max_pending_sentences=16,
        tts_first_segment_max_chars=400,
        opus_bit_rate=510000,
        speech_max_buffer_bytes=1,
        speech_max_buffer_frames=1,
        speech_backpressure_timeout_s=0.0,
    )
    assert settings.stt_num_threads == 64
    assert settings.stt_flush_padding_ms == 1000
    assert settings.tts_speed == 2.0
    assert settings.speech_backpressure_timeout_s == 0.0


def test_tts_voice_must_not_be_empty() -> None:
    with pytest.raises(ValidationError, match="tts_voice"):
        Settings(_env_file=None, tts_voice="   ")


@pytest.mark.parametrize(
    "field_name",
    [
        "stt_model_dir",
        "whisper_model_dir",
        "tts_model_path",
        "tts_voices_path",
    ],
)
def test_optional_paths_expand_user(field_name: str) -> None:
    settings = Settings(_env_file=None, **{field_name: "~/models/speech"})
    value = getattr(settings, field_name)
    assert value == Path("~/models/speech").expanduser()
    assert value.is_absolute()


def test_blank_optional_path_is_none() -> None:
    settings = Settings(_env_file=None, stt_model_dir="   ")
    assert settings.stt_model_dir is None

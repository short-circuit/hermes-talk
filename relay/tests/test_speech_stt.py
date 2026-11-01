"""Offline tests for the streaming and fallback STT adapters."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from relay.config import Settings
from relay.speech import stt as stt_module
from relay.speech.audio import PcmChunk
from relay.speech.errors import SpeechError, SpeechModelUnavailable, SpeechStateError
from relay.speech.stt import SherpaStreamingStt, WhisperFallbackStt, create_stt


class FakeClock:
    def __init__(self) -> None:
        self._now = 100.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeStream:
    def __init__(self) -> None:
        self.ready = False
        self.text = ""
        self.endpoint = False
        self.accepted: list[tuple[int, int]] = []
        self.reset_count = 0

    def accept_waveform(self, sample_rate: int, samples: object) -> None:
        self.accepted.append((sample_rate, len(samples)))  # type: ignore[arg-type]
        self.ready = True


class FakeRecognizer:
    def __init__(self, script: list[tuple[str, bool]]) -> None:
        self.script = list(script)
        self.decode_calls = 0
        self.streams: list[FakeStream] = []

    def create_stream(self) -> FakeStream:
        stream = FakeStream()
        self.streams.append(stream)
        return stream

    def is_ready(self, stream: FakeStream) -> bool:
        return stream.ready

    def decode_stream(self, stream: FakeStream) -> None:
        self.decode_calls += 1
        stream.ready = False
        if self.script:
            text, endpoint = self.script.pop(0)
            stream.text = text
            stream.endpoint = endpoint

    def get_result(self, stream: FakeStream) -> str:
        return stream.text

    def is_endpoint(self, stream: FakeStream) -> bool:
        return stream.endpoint

    def reset(self, stream: FakeStream) -> None:
        stream.text = ""
        stream.endpoint = False
        stream.reset_count += 1


class PaddingFakeStream:
    """Fake decoder that withholds its last token until it sees padding silence."""

    def __init__(
        self,
        padding_samples: int,
        partial_text: str,
        final_text: str,
        *,
        endpoint_on_padding: bool = False,
    ) -> None:
        self.padding_samples = padding_samples
        self.partial_text = partial_text
        self.final_text = final_text
        self.endpoint_on_padding = endpoint_on_padding
        self.text = partial_text
        self.endpoint = False
        self.accepted: list[tuple[int, int]] = []
        self.silent_samples = 0
        self.had_speech = False
        self.reset_count = 0
        self.ready = False

    def accept_waveform(self, sample_rate: int, samples: object) -> None:
        values = list(samples)  # type: ignore[arg-type]
        self.accepted.append((sample_rate, len(values)))
        if any(float(value) != 0.0 for value in values):
            self.had_speech = True
        else:
            self.silent_samples += len(values)
        if self.had_speech and self.silent_samples >= self.padding_samples:
            self.text = self.final_text
            self.endpoint = self.endpoint_on_padding
        self.ready = True

    def reset(self) -> None:
        self.text = ""
        self.endpoint = False
        self.silent_samples = 0
        self.had_speech = False
        self.reset_count += 1


class PaddingFakeRecognizer:
    def __init__(
        self,
        padding_samples: int,
        partial_text: str,
        final_text: str,
        *,
        endpoint_on_padding: bool = False,
    ) -> None:
        self.padding_samples = padding_samples
        self.partial_text = partial_text
        self.final_text = final_text
        self.endpoint_on_padding = endpoint_on_padding
        self.streams: list[PaddingFakeStream] = []
        self.decode_calls = 0

    def create_stream(self) -> PaddingFakeStream:
        stream = PaddingFakeStream(
            self.padding_samples,
            self.partial_text,
            self.final_text,
            endpoint_on_padding=self.endpoint_on_padding,
        )
        self.streams.append(stream)
        return stream

    def is_ready(self, stream: PaddingFakeStream) -> bool:
        return stream.ready

    def decode_stream(self, stream: PaddingFakeStream) -> None:
        self.decode_calls += 1
        stream.ready = False

    def get_result(self, stream: PaddingFakeStream) -> str:
        return stream.text

    def is_endpoint(self, stream: PaddingFakeStream) -> bool:
        return stream.endpoint

    def reset(self, stream: PaddingFakeStream) -> None:
        stream.reset()


def _sherpa_model_dir(tmp_path: Path) -> Path:
    root = tmp_path / "sherpa"
    root.mkdir()
    (root / "tokens.txt").write_text("a\n", encoding="utf-8")
    for prefix in ("encoder", "decoder", "joiner"):
        (root / f"{prefix}-epoch.int8.onnx").write_bytes(b"0")
    return root


def _adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recognizer: FakeRecognizer,
    **overrides: object,
) -> SherpaStreamingStt:
    monkeypatch.setattr(
        SherpaStreamingStt,
        "_build_recognizer",
        lambda self: recognizer,
    )
    return SherpaStreamingStt(_sherpa_model_dir(tmp_path), **overrides)  # type: ignore[arg-type]


def _chunk(frames: int = 320, sample_rate: int = 16000) -> PcmChunk:
    return PcmChunk(data=bytes(frames * 2), sample_rate=sample_rate)


def _speech_chunk(frames: int = 320, sample_rate: int = 16000) -> PcmChunk:
    return PcmChunk(data=(b"\x01\x00") * frames, sample_rate=sample_rate)


def test_partial_rate_limit_deduplicates_and_emits_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(stt_module, "_monotonic", clock)
    recognizer = FakeRecognizer(
        [
            ("hello", False),
            ("hello", False),
            ("hello world", False),
            ("hello world", False),
            ("hello world again", False),
        ]
    )
    adapter = _adapter(
        monkeypatch,
        tmp_path,
        recognizer,
        partial_interval_ms=120,
    )

    async def scenario() -> None:
        session = await adapter.start_session()
        first = await session.feed(_chunk())
        assert len(first) == 1
        assert first[0].kind == "partial"
        assert first[0].text == "hello"
        assert first[0].latency_ms >= 0

        duplicate = await session.feed(_chunk())
        assert duplicate == ()

        clock.advance(0.2)
        changed = await session.feed(_chunk())
        assert len(changed) == 1
        assert changed[0].text == "hello world"

        duplicate_after_interval = await session.feed(_chunk())
        assert duplicate_after_interval == ()

        clock.advance(0.2)
        again = await session.feed(_chunk())
        assert len(again) == 1
        assert again[0].text == "hello world again"

    asyncio.run(scenario())


def test_endpoint_emits_final_and_resets_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(stt_module, "_monotonic", clock)
    recognizer = FakeRecognizer([("hello world", True), ("next words", False)])
    adapter = _adapter(monkeypatch, tmp_path, recognizer)

    async def scenario() -> None:
        session = await adapter.start_session()
        events = await session.feed(_chunk())
        assert len(events) == 1
        assert events[0].kind == "final"
        assert events[0].is_endpoint is True
        assert events[0].text == "hello world"
        stream = recognizer.streams[0]
        assert stream.reset_count == 1

        clock.advance(0.2)
        following = await session.feed(_chunk())
        assert len(following) == 1
        assert following[0].kind == "partial"
        assert following[0].text == "next words"

    asyncio.run(scenario())


def test_flush_returns_final_with_latency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(stt_module, "_monotonic", clock)
    recognizer = FakeRecognizer([("flush me", False)])
    adapter = _adapter(monkeypatch, tmp_path, recognizer)

    async def scenario() -> None:
        session = await adapter.start_session()
        await session.feed(_chunk())
        clock.advance(0.05)
        event = await session.flush()
        assert event is not None
        assert event.kind == "final"
        assert event.text == "flush me"
        assert isinstance(event.latency_ms, float)
        assert event.latency_ms >= 0
        assert event.audio_ms == pytest.approx(20.0)
        assert await session.flush() is None

    asyncio.run(scenario())


def test_flush_with_no_audio_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recognizer = FakeRecognizer([])
    adapter = _adapter(monkeypatch, tmp_path, recognizer)

    async def scenario() -> None:
        session = await adapter.start_session()
        assert await session.flush() is None

    asyncio.run(scenario())


def test_flush_with_padding_completes_trailing_words(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(stt_module, "_monotonic", clock)
    partial_text = "WHAT'S THE WEATHER IN ZURICH"
    final_text = "WHAT'S THE WEATHER IN ZURICH TOMORROW"
    recognizer = PaddingFakeRecognizer(
        padding_samples=7680,
        partial_text=partial_text,
        final_text=final_text,
    )
    adapter = _adapter(monkeypatch, tmp_path, recognizer, flush_padding_ms=480)
    assert adapter.describe()["flush_padding_ms"] == 480

    async def scenario() -> None:
        session = await adapter.start_session()
        await session.feed(_speech_chunk())
        clock.advance(0.01)
        event = await session.flush()
        assert event is not None
        assert event.kind == "final"
        assert event.text == final_text
        assert event.audio_ms == pytest.approx(20.0)
        assert event.latency_ms >= 0
        stream = recognizer.streams[0]
        assert stream.accepted == [(16000, 320), (16000, 7680)]
        assert stream.reset_count == 1
        assert await session.flush() is None

    asyncio.run(scenario())


def test_flush_padding_zero_keeps_truncated_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    partial_text = "WHAT'S THE WEATHER IN ZURICH"
    recognizer = PaddingFakeRecognizer(
        padding_samples=7680,
        partial_text=partial_text,
        final_text="WHAT'S THE WEATHER IN ZURICH TOMORROW",
    )
    adapter = _adapter(monkeypatch, tmp_path, recognizer, flush_padding_ms=0)

    async def scenario() -> None:
        session = await adapter.start_session()
        await session.feed(_speech_chunk())
        event = await session.flush()
        assert event is not None
        assert event.text == partial_text
        stream = recognizer.streams[0]
        assert stream.accepted == [(16000, 320)]
        assert stream.reset_count == 1

    asyncio.run(scenario())


def test_flush_padding_is_one_chunk_and_bypasses_feed_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recognizer = PaddingFakeRecognizer(
        padding_samples=16000,
        partial_text="WHAT'S THE WEATHER IN ZURICH",
        final_text="WHAT'S THE WEATHER IN ZURICH TOMORROW",
    )
    adapter = _adapter(monkeypatch, tmp_path, recognizer, flush_padding_ms=1000)

    async def scenario() -> None:
        session = await adapter.start_session()
        with pytest.raises(ValueError, match="500 ms"):
            await session.feed(_speech_chunk(frames=9600))
        await session.feed(_speech_chunk())
        event = await session.flush()
        assert event is not None
        assert event.text == "WHAT'S THE WEATHER IN ZURICH TOMORROW"
        stream = recognizer.streams[0]
        padding_chunks = [entry for entry in stream.accepted if entry[1] == 16000]
        assert padding_chunks == [(16000, 16000)]

    asyncio.run(scenario())


def test_flush_padding_endpoint_returns_single_final_and_resets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recognizer = PaddingFakeRecognizer(
        padding_samples=7680,
        partial_text="WHAT'S THE WEATHER IN ZURICH",
        final_text="WHAT'S THE WEATHER IN ZURICH TOMORROW",
        endpoint_on_padding=True,
    )
    adapter = _adapter(monkeypatch, tmp_path, recognizer, flush_padding_ms=480)

    async def scenario() -> None:
        session = await adapter.start_session()
        await session.feed(_speech_chunk())
        event = await session.flush()
        assert event is not None
        assert event.is_endpoint is True
        assert event.text == "WHAT'S THE WEATHER IN ZURICH TOMORROW"
        stream = recognizer.streams[0]
        assert stream.reset_count == 1
        assert await session.flush() is None

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [-1, 1001])
def test_flush_padding_out_of_range_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    value: int,
) -> None:
    with pytest.raises(ValueError, match="flush_padding_ms"):
        _adapter(monkeypatch, tmp_path, FakeRecognizer([]), flush_padding_ms=value)


def test_feed_after_close_raises_state_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = _adapter(monkeypatch, tmp_path, FakeRecognizer([]))

    async def scenario() -> None:
        session = await adapter.start_session()
        await session.close()
        await session.close()
        with pytest.raises(SpeechStateError, match="closed"):
            await session.feed(_chunk())
        with pytest.raises(SpeechStateError, match="closed"):
            await session.flush()

    asyncio.run(scenario())


def test_48k_input_is_resampled_before_decoding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recognizer = FakeRecognizer([("resampled", False)])
    adapter = _adapter(monkeypatch, tmp_path, recognizer)

    async def scenario() -> None:
        session = await adapter.start_session()
        await session.feed(_chunk(frames=480, sample_rate=48000))
        accepted = recognizer.streams[0].accepted
        assert accepted
        assert accepted[0][0] == 16000
        assert accepted[0][1] == pytest.approx(160, abs=2)

    asyncio.run(scenario())


def test_frame_longer_than_500ms_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = _adapter(monkeypatch, tmp_path, FakeRecognizer([]))

    async def scenario() -> None:
        session = await adapter.start_session()
        with pytest.raises(ValueError, match="500 ms"):
            await session.feed(_chunk(frames=9600, sample_rate=16000))

    asyncio.run(scenario())


def test_create_stt_passes_flush_padding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recognizer = FakeRecognizer([])
    monkeypatch.setattr(
        SherpaStreamingStt,
        "_build_recognizer",
        lambda self: recognizer,
    )
    settings = Settings(
        _env_file=None,
        stt_engine="sherpa",
        stt_model_dir=_sherpa_model_dir(tmp_path),
        stt_flush_padding_ms=240,
    )
    adapter = create_stt(settings)
    assert isinstance(adapter, SherpaStreamingStt)
    assert adapter.flush_padding_ms == 240


def test_create_stt_without_model_dir_names_environment_variable() -> None:
    settings = Settings(_env_file=None, stt_engine="sherpa", stt_model_dir=None)
    with pytest.raises(SpeechModelUnavailable, match="STT_MODEL_DIR"):
        create_stt(settings)


def test_create_stt_with_missing_tokens_lists_expected_files(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, stt_engine="sherpa", stt_model_dir=tmp_path)
    with pytest.raises(SpeechModelUnavailable, match=r"tokens\.txt") as caught:
        create_stt(settings)
    message = str(caught.value)
    assert "encoder-*.onnx" in message
    assert "decoder-*.onnx" in message
    assert "joiner-*.onnx" in message


def test_create_stt_unknown_engine_raises() -> None:
    settings = SimpleNamespace(
        stt_engine="cloud",
        stt_model_dir=None,
        whisper_model_dir=None,
        stt_num_threads=4,
        stt_partial_interval_ms=120,
        whisper_chunk_ms=2000,
    )
    with pytest.raises(SpeechError, match="unsupported"):
        create_stt(settings)


class FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeWhisperModel:
    instances: ClassVar[list[FakeWhisperModel]] = []

    def __init__(self, model_dir: str, **kwargs: object) -> None:
        self.model_dir = model_dir
        self.kwargs = kwargs
        self.calls: list[tuple[object, dict[str, object]]] = []
        FakeWhisperModel.instances.append(self)

    def transcribe(
        self,
        audio: object,
        **kwargs: object,
    ) -> tuple[list[FakeSegment], None]:
        self.calls.append((audio, kwargs))
        return ([FakeSegment(" hello "), FakeSegment("   "), FakeSegment("world")], None)


def _whisper_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **overrides: object,
) -> WhisperFallbackStt:
    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    FakeWhisperModel.instances.clear()
    return WhisperFallbackStt(tmp_path, **overrides)  # type: ignore[arg-type]


def test_whisper_model_is_configured_local_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _whisper_adapter(monkeypatch, tmp_path, chunk_ms=500, cpu_threads=3)
    model = FakeWhisperModel.instances[0]
    assert model.kwargs["device"] == "cpu"
    assert model.kwargs["compute_type"] == "int8"
    assert model.kwargs["cpu_threads"] == 3
    assert model.kwargs["local_files_only"] is True


def test_whisper_emits_no_partials_and_respects_chunk_ms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = _whisper_adapter(monkeypatch, tmp_path, chunk_ms=500)
    model = FakeWhisperModel.instances[0]

    async def scenario() -> None:
        session = await adapter.start_session()
        for _ in range(4):
            assert await session.feed(_chunk(frames=1600)) == ()
        events = await session.feed(_chunk(frames=1600))
        assert len(events) == 2
        assert all(event.kind == "final" for event in events)
        assert [event.text for event in events] == ["hello", "world"]
        assert all(event.latency_ms >= 0 for event in events)
        assert events[0].audio_ms == pytest.approx(500.0)
        assert model.calls
        audio, kwargs = model.calls[0]
        assert hasattr(audio, "dtype")
        assert kwargs["beam_size"] == 1
        assert kwargs["language"] == "en"

    asyncio.run(scenario())


def test_whisper_flush_transcribes_buffer_and_clears_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = _whisper_adapter(monkeypatch, tmp_path, chunk_ms=2000)

    async def scenario() -> None:
        session = await adapter.start_session()
        assert await session.feed(_chunk(frames=1600)) == ()
        event = await session.flush()
        assert event is not None
        assert event.kind == "final"
        assert event.text == "hello world"
        assert event.latency_ms >= 0
        assert await session.flush() is None

    asyncio.run(scenario())


def test_whisper_close_blocks_feed_and_flush(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = _whisper_adapter(monkeypatch, tmp_path)

    async def scenario() -> None:
        session = await adapter.start_session()
        await session.close()
        await session.close()
        with pytest.raises(SpeechStateError, match="closed"):
            await session.feed(_chunk())
        with pytest.raises(SpeechStateError, match="closed"):
            await session.flush()

    asyncio.run(scenario())


def test_create_stt_whisper_without_model_dir_names_environment_variable() -> None:
    settings = Settings(_env_file=None, stt_engine="whisper", whisper_model_dir=None)
    with pytest.raises(SpeechModelUnavailable, match="WHISPER_MODEL_DIR"):
        create_stt(settings)

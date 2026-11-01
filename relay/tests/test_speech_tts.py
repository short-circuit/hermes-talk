"""Offline tests for sentence splitting and streaming TTS backpressure."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from relay.config import Settings
from relay.speech.audio import PcmChunk
from relay.speech.errors import (
    SpeechBackpressureExceeded,
    SpeechError,
    SpeechModelUnavailable,
    SpeechStateError,
)
from relay.speech.tts import SentenceSplitter, StreamingTts, create_tts


def _silence(duration_ms: int = 100, sample_rate: int = 24000) -> PcmChunk:
    frames = int(sample_rate * duration_ms / 1000)
    return PcmChunk(data=bytes(frames * 2), sample_rate=sample_rate)


class FakeEngine:
    def __init__(self, duration_ms: int = 100) -> None:
        self.duration_ms = duration_ms

    async def synthesize(
        self,
        text: str,
        *,
        voice: str = "af_sarah",
        speed: float = 1.0,
    ) -> PcmChunk:
        await asyncio.sleep(0)
        return _silence(self.duration_ms)


class BlockedEngine:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def synthesize(
        self,
        text: str,
        *,
        voice: str = "af_sarah",
        speed: float = 1.0,
    ) -> PcmChunk:
        self.started.set()
        await self.release.wait()
        return _silence(100)


class FailingEngine:
    async def synthesize(
        self,
        text: str,
        *,
        voice: str = "af_sarah",
        speed: float = 1.0,
    ) -> PcmChunk:
        raise SpeechModelUnavailable("kokoro TTS model is not configured: set TTS_MODEL_PATH")


async def _collect(tts: StreamingTts) -> list[object]:
    return [chunk async for chunk in tts.chunks()]


def test_splitter_splits_sentences_without_abbreviation_false_positives() -> None:
    splitter = SentenceSplitter()
    sentences = splitter.push("Dr. Smith arrived. He paid 3.5 dollars.")
    assert sentences == ["Dr. Smith arrived.", "He paid 3.5 dollars."]


def test_splitter_does_not_split_abbreviation_at_end() -> None:
    splitter = SentenceSplitter()
    assert splitter.push("e.g. this") == []
    assert splitter.flush() == ["e.g. this"]


def test_splitter_does_not_split_url_token() -> None:
    splitter = SentenceSplitter()
    sentences = splitter.push("Visit example.com. Then leave.")
    assert sentences == ["Visit example.com.", "Then leave."]


def test_splitter_handles_multiple_abbreviations() -> None:
    splitter = SentenceSplitter()
    sentences = splitter.push("Mr. Jones is here. Ms. Lee left.")
    assert sentences == ["Mr. Jones is here.", "Ms. Lee left."]


def test_splitter_flush_returns_unpunctuated_text() -> None:
    splitter = SentenceSplitter()
    assert splitter.push("hello there") == []
    assert splitter.flush() == ["hello there"]


def test_splitter_emits_leading_clause_for_long_comma_text() -> None:
    splitter = SentenceSplitter(clause_split_chars=80)
    text = "alpha " * 20 + "beta, gamma delta."
    segments = splitter.push(text)
    assert segments
    assert segments[0].endswith(",")
    assert len(segments[0]) <= 160
    assert segments[-1] == "gamma delta."
    assert splitter.flush() == []


def test_splitter_caps_long_segments() -> None:
    splitter = SentenceSplitter(max_segment_chars=160)
    segments = splitter.push("word " * 50 + "end.")
    assert segments
    assert all(len(segment) <= 160 for segment in segments)


def test_splitter_ignores_empty_deltas() -> None:
    splitter = SentenceSplitter()
    assert splitter.push("") == []
    assert splitter.push("   ") == []
    assert splitter.flush() == []


def test_streaming_tts_yields_packets_and_metrics() -> None:
    async def scenario() -> None:
        tts = StreamingTts(FakeEngine(), backpressure_timeout_s=1.0)
        await tts.push_text("Hello there.")
        await tts.flush()
        chunks = await asyncio.wait_for(_collect(tts), 2.0)
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.opus_packets
        assert all(packet for packet in chunk.opus_packets)
        assert chunk.first_audio_ms is not None
        assert chunk.first_audio_ms >= 0
        metrics = tts.metrics()
        assert metrics.sentences == 1
        assert metrics.packets == len(chunk.opus_packets)
        assert metrics.audio_ms > 0
        assert len(metrics.first_audio_ms) == 1
        await tts.close()

    asyncio.run(scenario())


def test_warmup_is_positive_and_does_not_pollute_metrics_or_queue() -> None:
    async def scenario() -> None:
        tts = StreamingTts(FakeEngine(), backpressure_timeout_s=1.0)
        elapsed_ms = await tts.warmup()
        assert elapsed_ms > 0
        metrics = tts.metrics()
        assert metrics.first_audio_ms == []
        assert metrics.sentences == 0
        assert metrics.packets == 0
        assert tts._sentences.empty()
        assert not tts._output

        second_ms = await tts.warmup(text="Still ready.")
        assert second_ms > 0
        assert tts.metrics().first_audio_ms == []
        assert tts._sentences.empty()

        await tts.push_text("Hello there.")
        await tts.flush()
        chunks = await asyncio.wait_for(_collect(tts), 2.0)
        assert len(chunks) == 1
        assert chunks[0].opus_packets
        assert tts.metrics().sentences == 1
        await tts.close()

    asyncio.run(scenario())


def test_warmup_after_close_raises_state_error() -> None:
    async def scenario() -> None:
        tts = StreamingTts(FakeEngine())
        await tts.close()
        with pytest.raises(SpeechStateError):
            await tts.warmup()

    asyncio.run(scenario())


def test_push_text_raises_backpressure_when_queue_stays_full() -> None:
    async def scenario() -> None:
        engine = BlockedEngine()
        tts = StreamingTts(
            engine,
            max_pending_sentences=1,
            backpressure_timeout_s=0.05,
        )
        consumer = asyncio.create_task(_collect(tts))
        await tts.push_text("First sentence.")
        await engine.started.wait()
        await tts.push_text("Second sentence.")
        with pytest.raises(SpeechBackpressureExceeded) as caught:
            await tts.push_text("Third sentence.")
        assert caught.value.limit_name == "tts_sentence_queue"
        assert caught.value.limit_value == 1
        engine.release.set()
        await tts.flush()
        chunks = await asyncio.wait_for(consumer, 2.0)
        assert len(chunks) == 2
        await tts.close()

    asyncio.run(scenario())


def test_close_cancels_pending_synthesis_and_is_idempotent() -> None:
    async def scenario() -> None:
        engine = BlockedEngine()
        tts = StreamingTts(engine, backpressure_timeout_s=1.0)
        consumer = asyncio.create_task(_collect(tts))
        await tts.push_text("Hello there.")
        await engine.started.wait()
        await tts.close()
        await tts.close()
        chunks = await asyncio.wait_for(consumer, 2.0)
        assert chunks == []

    asyncio.run(scenario())


def test_clear_drops_queued_work() -> None:
    async def scenario() -> None:
        tts = StreamingTts(BlockedEngine(), max_pending_sentences=3)
        await tts.push_text("One sentence.")
        await tts.push_text("Two sentence.")
        tts.clear()
        assert tts.metrics().dropped_sentences == 2
        await tts.flush()
        chunks = await asyncio.wait_for(_collect(tts), 2.0)
        assert chunks == []
        await tts.close()

    asyncio.run(scenario())


def test_synthesis_failure_propagates_to_consumer() -> None:
    async def scenario() -> None:
        tts = StreamingTts(FailingEngine())
        await tts.push_text("Hello there.")
        await tts.flush()
        with pytest.raises(SpeechModelUnavailable):
            async for _chunk in tts.chunks():
                pass
        await tts.close()

    asyncio.run(scenario())


def test_create_tts_without_model_path_names_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TTS_MODEL_PATH", raising=False)
    monkeypatch.delenv("TTS_VOICES_PATH", raising=False)
    settings = Settings(_env_file=None, tts_model_path=None, tts_voices_path=None)
    with pytest.raises(SpeechModelUnavailable, match="TTS_MODEL_PATH"):
        create_tts(settings)


def test_create_tts_unknown_engine_raises() -> None:
    settings = SimpleNamespace(tts_engine="piper")
    with pytest.raises(SpeechError, match="unsupported"):
        create_tts(settings)

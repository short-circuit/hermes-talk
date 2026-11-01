"""Offline tests for PCM helpers, Opus encoding, and bounded buffers."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from relay.speech.audio import OpusEncoder, PcmChunk, pcm16_from_float32, resample_pcm16
from relay.speech.backpressure import BoundedAudioBuffer, BufferLimits
from relay.speech.errors import SpeechBackpressureExceeded, SpeechStateError

_FRAME_BYTES_20MS_16K = 320
_FRAME_BYTES_20MS_48K = 960


def _chunk(frames: int, sample_rate: int) -> PcmChunk:
    return PcmChunk(data=bytes(frames * 2), sample_rate=sample_rate)


def test_resample_16k_to_48k_triples_samples() -> None:
    source = pcm16_from_float32(np.zeros(16000, dtype=np.float32), 16000)
    resampled = resample_pcm16(source.data, 16000, 48000)
    chunk = PcmChunk(data=resampled, sample_rate=48000)
    chunk.validate()
    assert chunk.frames == pytest.approx(source.frames * 3, rel=0.01)


def test_resample_identity_returns_input_bytes() -> None:
    source = pcm16_from_float32(np.zeros(1600, dtype=np.float32), 16000)
    assert resample_pcm16(source.data, 16000, 16000) is source.data


def test_pcm_chunk_rejects_odd_byte_count() -> None:
    chunk = PcmChunk(data=b"\x00\x01\x02", sample_rate=16000)
    with pytest.raises(ValueError, match="even number"):
        chunk.validate()
    with pytest.raises(ValueError, match="even number"):
        resample_pcm16(b"\x00\x01\x02", 16000, 48000)


def test_pcm_chunk_rejects_empty_and_out_of_range_rate() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        PcmChunk(data=b"", sample_rate=16000).validate()
    with pytest.raises(ValueError, match="sample rate"):
        PcmChunk(data=b"\x00\x00", sample_rate=4000).validate()


def test_pcm16_from_float32_clips() -> None:
    chunk = pcm16_from_float32(np.array([2.0, -2.0, 0.0], dtype=np.float32), 16000)
    samples = np.frombuffer(chunk.data, dtype=np.int16)
    assert samples[0] == 32767
    assert samples[1] == -32767
    assert samples[2] == 0


def test_opus_encoder_emits_one_packet_per_frame() -> None:
    source = pcm16_from_float32(np.zeros(48000, dtype=np.float32), 48000)
    with OpusEncoder() as encoder:
        packets = encoder.encode(source)
        packets.extend(encoder.flush())
    expected = int(source.duration_ms / 20.0)
    assert len(packets) == pytest.approx(expected, abs=1)
    assert all(packet for packet in packets)


def test_opus_encoder_close_is_idempotent() -> None:
    encoder = OpusEncoder()
    encoder.close()
    encoder.close()
    with pytest.raises(SpeechStateError, match="closed"):
        encoder.encode(_chunk(960, 48000))


def test_drop_oldest_evicts_and_counts() -> None:
    limits = BufferLimits(max_bytes=200, max_frames=100, name="ingress")
    buffer = BoundedAudioBuffer(limits, policy="drop_oldest")

    async def scenario() -> None:
        for _ in range(6):
            result = await buffer.put(_chunk(20, 16000))
            assert result.accepted is True
        stats = buffer.stats()
        assert stats["pushed"] == 6
        assert stats["dropped"] == 1
        assert stats["backpressure_events"] == 1
        assert stats["frames"] == 100
        assert stats["bytes"] == 200
        assert buffer.qsize() == 5
        first = await buffer.get()
        assert first.frames == 20
        buffer.clear()
        assert buffer.stats()["frames"] == 0

    asyncio.run(scenario())


def test_block_policy_raises_after_timeout() -> None:
    limits = BufferLimits(max_bytes=40, max_frames=100, name="ingress")
    buffer = BoundedAudioBuffer(limits, policy="block")

    async def scenario() -> None:
        await buffer.put(_chunk(20, 16000))
        with pytest.raises(SpeechBackpressureExceeded) as caught:
            await buffer.put(_chunk(20, 16000), timeout=0.02)
        assert caught.value.limit_name == "ingress"
        assert caught.value.limit_value == 40
        assert caught.value.observed_value == 80

    asyncio.run(scenario())


def test_block_policy_accepts_after_consumer_frees_space() -> None:
    limits = BufferLimits(max_bytes=40, max_frames=100, name="ingress")
    buffer = BoundedAudioBuffer(limits, policy="block")

    async def scenario() -> None:
        await buffer.put(_chunk(20, 16000))

        async def consume() -> None:
            await asyncio.sleep(0.01)
            await buffer.get()

        consumer = asyncio.create_task(consume())
        result = await buffer.put(_chunk(20, 16000), timeout=1.0)
        assert result.accepted is True
        await consumer

    asyncio.run(scenario())


def test_oversized_single_chunk_raises_value_error() -> None:
    limits = BufferLimits(max_bytes=10, max_frames=100, name="ingress")
    buffer = BoundedAudioBuffer(limits, policy="drop_oldest")

    async def scenario() -> None:
        with pytest.raises(ValueError, match="exceeds"):
            await buffer.put(_chunk(20, 16000))

    asyncio.run(scenario())


def test_buffer_limits_reject_non_positive_values() -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        BufferLimits(max_bytes=0, max_frames=1, name="x")
    with pytest.raises(ValueError, match="max_frames"):
        BufferLimits(max_bytes=1, max_frames=0, name="x")
    with pytest.raises(ValueError, match="name"):
        BufferLimits(max_bytes=1, max_frames=1, name=" ")


def test_frame_constants_match_chunk_helpers() -> None:
    assert _chunk(160, 16000).data == bytes(_FRAME_BYTES_20MS_16K)
    assert _chunk(480, 48000).data == bytes(_FRAME_BYTES_20MS_48K)

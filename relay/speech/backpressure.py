"""Explicit backpressure limits for audio ingress.

The relay never lets an unbounded audio queue grow with a slow consumer.  This
module provides a small bounded buffer with two documented policies:

* ``drop_oldest`` keeps latency bounded by evicting the oldest chunks.
* ``block`` waits for the consumer and fails loudly when the timeout expires.

The buffer is single-event-loop only and is **not thread-safe**.  All counters
are updated in the event loop that owns the buffer.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Literal, NoReturn

from relay.speech.audio import PcmChunk
from relay.speech.errors import SpeechBackpressureExceeded


@dataclass(frozen=True)
class BufferLimits:
    """Hard caps for one bounded audio buffer."""

    max_bytes: int
    max_frames: int
    name: str

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            msg = "max_bytes must be greater than zero"
            raise ValueError(msg)
        if self.max_frames <= 0:
            msg = "max_frames must be greater than zero"
            raise ValueError(msg)
        if not self.name.strip():
            msg = "buffer name must not be empty"
            raise ValueError(msg)


@dataclass(frozen=True)
class PutResult:
    """Outcome of a :meth:`BoundedAudioBuffer.put` call."""

    accepted: bool
    dropped: tuple[PcmChunk, ...]
    reason: str | None


class BoundedAudioBuffer:
    """A bounded FIFO of PCM chunks with explicit drop or block behaviour.

    Not thread-safe: create and use one instance from a single event loop.
    """

    def __init__(
        self,
        limits: BufferLimits,
        *,
        policy: Literal["drop_oldest", "block"],
        timeout_s: float | None = None,
    ) -> None:
        if policy not in ("drop_oldest", "block"):
            msg = "policy must be 'drop_oldest' or 'block'"
            raise ValueError(msg)
        if timeout_s is not None and timeout_s < 0:
            msg = "timeout_s must be zero or greater"
            raise ValueError(msg)
        self._limits = limits
        self._policy = policy
        self._timeout_s = timeout_s
        self._chunks: deque[PcmChunk] = deque()
        self._bytes = 0
        self._frames = 0
        self._pushed = 0
        self._dropped = 0
        self._backpressure_events = 0
        self._high_water_frames = 0
        self._space_available = asyncio.Event()
        self._space_available.set()
        self._data_available = asyncio.Event()

    def _fits(self, chunk: PcmChunk) -> bool:
        return (
            self._bytes + len(chunk.data) <= self._limits.max_bytes
            and self._frames + chunk.frames <= self._limits.max_frames
        )

    def _append(self, chunk: PcmChunk) -> None:
        self._chunks.append(chunk)
        self._bytes += len(chunk.data)
        self._frames += chunk.frames
        self._pushed += 1
        self._high_water_frames = max(self._high_water_frames, self._frames)
        self._data_available.set()
        if self._bytes >= self._limits.max_bytes or self._frames >= self._limits.max_frames:
            self._space_available.clear()

    def _pop_oldest(self) -> PcmChunk:
        chunk = self._chunks.popleft()
        self._bytes -= len(chunk.data)
        self._frames -= chunk.frames
        self._space_available.set()
        if not self._chunks:
            self._data_available.clear()
        return chunk

    def _drop_oldest_put(self, chunk: PcmChunk) -> PutResult:
        dropped: list[PcmChunk] = []
        while self._chunks and not self._fits(chunk):
            dropped.append(self._pop_oldest())
        self._append(chunk)
        if dropped:
            self._dropped += len(dropped)
            self._backpressure_events += 1
        return PutResult(
            accepted=True,
            dropped=tuple(dropped),
            reason="evicted_oldest" if dropped else None,
        )

    async def _block_put(self, chunk: PcmChunk, wait_s: float | None) -> PutResult:
        if wait_s is None:
            while not self._fits(chunk):
                self._space_available.clear()
                if self._fits(chunk):
                    break
                await self._space_available.wait()
            self._append(chunk)
            return PutResult(accepted=True, dropped=(), reason=None)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while not self._fits(chunk):
            self._space_available.clear()
            if self._fits(chunk):
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                self._raise_backpressure(chunk)
            try:
                await asyncio.wait_for(self._space_available.wait(), remaining)
            except TimeoutError:
                self._raise_backpressure(chunk)
        self._append(chunk)
        return PutResult(accepted=True, dropped=(), reason=None)

    def _raise_backpressure(self, chunk: PcmChunk) -> NoReturn:
        self._backpressure_events += 1
        observed_bytes = self._bytes + len(chunk.data)
        observed_frames = self._frames + chunk.frames
        msg = (
            f"{self._limits.name} buffer is full: "
            f"limit={self._limits.max_bytes} bytes/{self._limits.max_frames} frames, "
            f"observed={observed_bytes} bytes/{observed_frames} frames"
        )
        raise SpeechBackpressureExceeded(
            self._limits.name,
            self._limits.max_bytes,
            observed_bytes,
            detail=msg,
        )

    async def put(self, chunk: PcmChunk, *, timeout: float | None = None) -> PutResult:  # noqa: ASYNC109
        """Try to enqueue a chunk under the configured policy.

        ``drop_oldest`` never blocks and evicts the oldest chunks until the new
        chunk fits.  ``block`` waits for space up to ``timeout`` (falling back
        to the buffer's ``timeout_s``) and raises
        :class:`SpeechBackpressureExceeded` when the deadline expires.
        """
        chunk.validate()
        if len(chunk.data) > self._limits.max_bytes:
            msg = (
                f"chunk of {len(chunk.data)} bytes exceeds {self._limits.name} "
                f"max_bytes={self._limits.max_bytes}"
            )
            raise ValueError(msg)
        if chunk.frames > self._limits.max_frames:
            msg = (
                f"chunk of {chunk.frames} frames exceeds {self._limits.name} "
                f"max_frames={self._limits.max_frames}"
            )
            raise ValueError(msg)

        if self._policy == "drop_oldest":
            return self._drop_oldest_put(chunk)
        effective_timeout = timeout if timeout is not None else self._timeout_s
        return await self._block_put(chunk, effective_timeout)

    async def get(self) -> PcmChunk:
        """Wait for and remove the oldest chunk."""
        while not self._chunks:
            self._data_available.clear()
            if self._chunks:
                break
            await self._data_available.wait()
        return self._pop_oldest()

    def qsize(self) -> int:
        """Number of chunks currently buffered."""
        return len(self._chunks)

    def clear(self) -> None:
        """Drop every buffered chunk and reset the queue occupancy."""
        self._chunks.clear()
        self._bytes = 0
        self._frames = 0
        self._data_available.clear()
        self._space_available.set()

    def stats(self) -> dict[str, object]:
        """Return counters and limits without exposing audio bytes."""
        return {
            "name": self._limits.name,
            "frames": self._frames,
            "bytes": self._bytes,
            "max_frames": self._limits.max_frames,
            "max_bytes": self._limits.max_bytes,
            "pushed": self._pushed,
            "dropped": self._dropped,
            "backpressure_events": self._backpressure_events,
            "high_water_frames": self._high_water_frames,
        }

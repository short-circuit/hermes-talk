"""Transport-agnostic admission control for the Hermes Talk relay.

The relay is single-user on a LAN but reachable by every host on that LAN, so
every request path that can cost time, memory, or bandwidth is admitted through
this module.  Excess load is rejected with ``429`` or ``503`` plus a short
``spoken`` sentence that the client renders through local text-to-speech, so
audio is never silently dropped.  All limits come from
:class:`relay.config.Settings`; nothing here makes a network call.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass

from relay.config import Settings

_LOGGER = logging.getLogger(__name__)

# Unit conversions only; every actual limit is read from Settings.
_SECONDS_PER_MINUTE = 60.0
_MILLISECONDS_PER_SECOND = 1000.0
_ONE_TOKEN = 1.0

_SPOKEN_RATE_LIMITED = "One moment — the relay is handling too many requests."
_SPOKEN_UPLOAD_QUEUE_FULL = "The upload queue is full. Try again in a moment."
_SPOKEN_SESSION_LIMIT = "The relay is already on a call. Try again shortly."
_SPOKEN_RUN_LIMIT = "Hermes is busy with another request. Try again shortly."
_SPOKEN_RECONNECT_BUDGET = "The connection kept dropping. Start a new call."
_SPOKEN_IDLE_STREAM = "The connection went quiet, so I stopped the run."

# Documented route-prefix table: the only path literals in this module.
RATE_LIMIT_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("POST", "/v1/runs", "run"),
    ("POST", "/v1/attachments", "upload"),
    ("PUT", "/v1/attachments", "upload"),
    ("GET", "/v1/artifacts", "download"),
    ("HEAD", "/v1/artifacts", "download"),
)


class LimitError(Exception):
    """Base class for an admission decision that maps to an HTTP error."""

    status_code: int = 503
    code: str = "limit_exceeded"
    spoken: str = ""
    detail: str = "request rejected by admission control"
    retry_after: int | None = None

    def __init__(
        self,
        *,
        detail: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        if detail is not None:
            self.detail = detail
        self.retry_after = retry_after
        super().__init__(self.detail)

    def to_body(self, correlation_id: str | None = None) -> dict[str, object]:
        """Render the client-facing error body; never contains token material."""
        body: dict[str, object] = {
            "error": self.code,
            "detail": self.detail,
            "spoken": self.spoken,
            "retry_after": self.retry_after,
        }
        if correlation_id:
            body["correlation_id"] = correlation_id
        return body

    def headers(self) -> dict[str, str]:
        """Return HTTP headers, including ``Retry-After`` when a delay is known."""
        if self.retry_after is None:
            return {}
        return {"Retry-After": str(self.retry_after)}


class RateLimitExceeded(LimitError):
    """A device exceeded its per-minute budget for a category."""

    status_code = 429
    code = "rate_limited"
    spoken = _SPOKEN_RATE_LIMITED
    detail = "per-device rate limit exceeded"


class UploadQueueFull(LimitError):
    """The bounded upload queue cannot accept another lease."""

    status_code = 429
    code = "upload_queue_full"
    spoken = _SPOKEN_UPLOAD_QUEUE_FULL
    detail = "the upload queue is full"


class SessionLimitExceeded(LimitError):
    """No capacity remains for another concurrent call session."""

    status_code = 503
    code = "session_limit_reached"
    spoken = _SPOKEN_SESSION_LIMIT
    detail = "the concurrent session limit is reached"


class RunLimitExceeded(LimitError):
    """No capacity remains for another concurrent run."""

    status_code = 503
    code = "run_limit_reached"
    spoken = _SPOKEN_RUN_LIMIT
    detail = "the concurrent run limit is reached"


class ReconnectBudgetExceeded(LimitError):
    """A stream reconnect exceeded its bounded retry budget."""

    status_code = 429
    code = "reconnect_budget_exhausted"
    spoken = _SPOKEN_RECONNECT_BUDGET
    detail = "the stream reconnect budget is exhausted"


class StreamIdleTimeout(Exception):
    """An SSE stream produced no frame within its idle timeout."""

    def __init__(self, idle_timeout_seconds: float) -> None:
        self.idle_timeout_seconds = idle_timeout_seconds
        self.spoken = _SPOKEN_IDLE_STREAM
        super().__init__(self.spoken)


class FrameTooLargeError(Exception):
    """An SSE frame would exceed the configured byte cap."""

    def __init__(self, *, size: int, max_bytes: int) -> None:
        self.size = size
        self.max_bytes = max_bytes
        super().__init__(f"SSE frame is {size} bytes; the cap is {max_bytes}")


class LimitsConfigError(ValueError):
    """The configured limits are internally inconsistent."""


@dataclass(frozen=True)
class RateLimit:
    """A per-device token budget expressed per minute and as a burst."""

    per_minute: int
    burst: int


class TokenBucket:
    """A monotonic-clock token bucket with fractional refill."""

    def __init__(
        self,
        *,
        capacity: float,
        refill_per_second: float,
        clock: Callable[[], float],
    ) -> None:
        if capacity <= 0 or refill_per_second <= 0:
            msg = "token bucket capacity and refill rate must be positive"
            raise LimitsConfigError(msg)
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._clock = clock
        self._tokens = capacity
        self._updated = clock()

    def try_consume(self, *, now: float | None = None) -> tuple[bool, float]:
        """Consume one token; return ``(ok, seconds_until_one_refills)``."""
        moment = self._clock() if now is None else now
        self._refill(moment)
        if self._tokens >= _ONE_TOKEN:
            self._tokens -= _ONE_TOKEN
            return True, 0.0
        missing = _ONE_TOKEN - self._tokens
        return False, missing / self._refill_per_second

    def full_at(self, now: float) -> bool:
        """Return whether the bucket would be full again at ``now``."""
        projected = self._tokens + max(now - self._updated, 0.0) * self._refill_per_second
        return projected >= self._capacity

    def _refill(self, now: float) -> None:
        elapsed = max(now - self._updated, 0.0)
        if elapsed:
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * self._refill_per_second,
            )
            self._updated = now


class DeviceRateLimiter:
    """Per-device-token-id token buckets with a bounded LRU of tracked keys.

    Buckets are keyed by ``(device_id, category)`` where ``device_id`` is the
    public token id (8 hex characters), never the plaintext token.  The mapping
    is an LRU capped at ``max_devices`` keys, and a bucket that has refilled
    fully after an idle period is dropped rather than kept in memory.
    """

    def __init__(
        self,
        limits: Mapping[str, RateLimit],
        *,
        max_devices: int,
        clock: Callable[[], float],
    ) -> None:
        if max_devices < 1:
            msg = "max_devices must be at least one"
            raise LimitsConfigError(msg)
        self._limits = dict(limits)
        self._max_devices = max_devices
        self._clock = clock
        self._buckets: OrderedDict[tuple[str, str], TokenBucket] = OrderedDict()

    def check(self, device_id: str, category: str, *, now: float | None = None) -> None:
        """Consume one request token or raise :class:`RateLimitExceeded`."""
        limit = self._limits.get(category)
        if limit is None:
            return
        moment = self._clock() if now is None else now
        self._prune_full(moment)
        key = (device_id, category)
        bucket = self._buckets.get(key)
        if bucket is None:
            while len(self._buckets) >= self._max_devices:
                self._buckets.popitem(last=False)
            bucket = TokenBucket(
                capacity=float(limit.burst),
                refill_per_second=limit.per_minute / _SECONDS_PER_MINUTE,
                clock=self._clock,
            )
            self._buckets[key] = bucket
        else:
            self._buckets.move_to_end(key)
        ok, retry_after = bucket.try_consume(now=moment)
        if not ok:
            raise RateLimitExceeded(
                detail=f"rate limit for {category!r} exceeded",
                retry_after=math.ceil(retry_after),
            )

    def _prune_full(self, now: float) -> None:
        stale = [key for key, bucket in self._buckets.items() if bucket.full_at(now)]
        for key in stale:
            del self._buckets[key]

    def snapshot(self) -> dict[str, int]:
        """Return bounded counters only; no device ids or tokens."""
        return {
            "tracked_devices": len(self._buckets),
            "max_devices": self._max_devices,
        }


class SessionLease:
    """One admitted call session with an idle deadline and stop hooks."""

    def __init__(
        self,
        *,
        session_id: str,
        device_id: str,
        idle_timeout_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        self.session_id = session_id
        self.device_id = device_id
        self._clock = clock
        self._idle_timeout_seconds = idle_timeout_seconds
        self._idle_deadline = clock() + idle_timeout_seconds
        self._closed = False
        self._hooks: list[Callable[[], object]] = []

    def touch(self, *, now: float | None = None) -> None:
        """Refresh the idle deadline while the session is still open."""
        if self._closed:
            return
        moment = self._clock() if now is None else now
        self._idle_deadline = moment + self._idle_timeout_seconds

    def register_stop(self, hook: Callable[[], object]) -> None:
        """Register a sync or async hook run exactly once when the session ends."""
        if not self._closed:
            self._hooks.append(hook)

    @property
    def idle_deadline(self) -> float:
        """Monotonic time at which the session becomes reapable."""
        return self._idle_deadline

    @property
    def closed(self) -> bool:
        """Return whether the lease has been closed."""
        return self._closed

    async def _close(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        hooks = self._hooks
        self._hooks = []
        for hook in hooks:
            try:
                result = hook()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                _LOGGER.exception(
                    "limits.stop_hook_failed",
                    extra={
                        "event": "limits.stop_hook_failed",
                        "session_id": self.session_id,
                        "reason": reason,
                    },
                )


class RunLease:
    """One admitted concurrent run; releasing it frees global and device slots."""

    def __init__(self, *, device_id: str, registry: _RunRegistry) -> None:
        self.device_id = device_id
        self._registry = registry
        self._released = False

    def release(self) -> None:
        """Release the run slot; idempotent."""
        if self._released:
            return
        self._released = True
        self._registry._release(self)

    @property
    def released(self) -> bool:
        """Return whether the lease has been released."""
        return self._released


class UploadLease:
    """One upload slot or queue position, promoted FIFO as slots free up."""

    def __init__(self, *, device_id: str, registry: _UploadRegistry, waiting: bool) -> None:
        self.device_id = device_id
        self._registry = registry
        self._waiting = waiting
        self._released = False

    @property
    def waiting(self) -> bool:
        """Return whether the lease is queued rather than in flight."""
        return self._waiting and not self._released

    def start(self) -> None:
        """Promote this queued lease when an in-flight slot is free."""
        self._registry._start(self)

    def release(self) -> None:
        """Release the lease; idempotent and promotes the next queued lease."""
        self._registry._release(self)

    def cancel(self) -> None:
        """Abandon a queued place, or release an in-flight slot."""
        self._registry._cancel(self)

    @property
    def released(self) -> bool:
        """Return whether the lease has been released or cancelled."""
        return self._released


class BargeInGate:
    """Debounce barge-in so a noisy room produces one stop, not fifty."""

    def __init__(
        self,
        *,
        min_speech_ms: int,
        cooldown_ms: int,
        clock: Callable[[], float],
    ) -> None:
        if min_speech_ms < 1:
            msg = "min_speech_ms must be at least one"
            raise LimitsConfigError(msg)
        if cooldown_ms < 0:
            msg = "cooldown_ms must be zero or greater"
            raise LimitsConfigError(msg)
        self._min_speech_ms = min_speech_ms
        self._cooldown_seconds = cooldown_ms / _MILLISECONDS_PER_SECOND
        self._clock = clock
        self._last_accepted: float | None = None
        self._accepted = 0
        self._suppressed_short = 0
        self._suppressed_cooldown = 0

    def consider(self, speech_ms: int, *, now: float | None = None) -> bool:
        """Accept or suppress an interruption; ``True`` stops the run."""
        moment = self._clock() if now is None else now
        if speech_ms < self._min_speech_ms:
            self._suppressed_short += 1
            return False
        if (
            self._last_accepted is not None
            and moment - self._last_accepted < self._cooldown_seconds
        ):
            self._suppressed_cooldown += 1
            return False
        self._accepted += 1
        self._last_accepted = moment
        return True

    def snapshot(self) -> dict[str, int]:
        """Return counters only; no transcript or device data."""
        return {
            "accepted": self._accepted,
            "suppressed_short": self._suppressed_short,
            "suppressed_cooldown": self._suppressed_cooldown,
        }


class SseFrameCodec:
    """Encode SSE frames under a byte cap; errors always fit."""

    def __init__(self, *, max_frame_bytes: int) -> None:
        if max_frame_bytes < 1:
            msg = "max_frame_bytes must be at least one"
            raise LimitsConfigError(msg)
        self._max_frame_bytes = max_frame_bytes

    def encode(self, event: str, data: str) -> bytes:
        """Encode one event frame or raise :class:`FrameTooLargeError`."""
        frame = f"event: {event}\ndata: {data}\n\n".encode()
        if len(frame) > self._max_frame_bytes:
            raise FrameTooLargeError(size=len(frame), max_bytes=self._max_frame_bytes)
        return frame

    def encode_error(self, spoken: str, code: str) -> bytes:
        """Encode a small final error frame; trimmed if the cap is very small."""
        frame = self._error_frame(spoken, code)
        while len(frame) > self._max_frame_bytes and spoken:
            spoken = spoken[: len(spoken) - 1]
            frame = self._error_frame(spoken, code)
        return frame

    def _error_frame(self, spoken: str, code: str) -> bytes:
        payload = json.dumps({"error": code, "spoken": spoken}, ensure_ascii=False)
        return f"event: error\ndata: {payload}\n\n".encode()


class SseStreamGuard:
    """Apply a per-frame idle timeout to an SSE source and stop the run."""

    def __init__(
        self,
        *,
        idle_timeout_seconds: float,
        on_idle: Callable[[], object] | None = None,
    ) -> None:
        if idle_timeout_seconds <= 0:
            msg = "idle_timeout_seconds must be positive"
            raise LimitsConfigError(msg)
        self._idle_timeout_seconds = idle_timeout_seconds
        self._on_idle = on_idle
        self._idle_handled = False

    async def guard(self, source: AsyncIterator[str]) -> AsyncIterator[str]:
        """Yield frames until the source ends or idles out."""
        iterator = source.__aiter__()
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(
                        iterator.__anext__(),
                        timeout=self._idle_timeout_seconds,
                    )
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    await self._handle_idle()
                    raise StreamIdleTimeout(self._idle_timeout_seconds) from None
                yield frame
        finally:
            await self._close_source(source)

    async def _handle_idle(self) -> None:
        if self._idle_handled:
            return
        self._idle_handled = True
        _LOGGER.warning(
            "stream.idle_timeout",
            extra={
                "event": "stream.idle_timeout",
                "idle_timeout_seconds": self._idle_timeout_seconds,
            },
        )
        if self._on_idle is None:
            return
        try:
            result = self._on_idle()
            if inspect.isawaitable(result):
                await result
        except Exception:
            _LOGGER.exception(
                "stream.idle_stop_hook_failed",
                extra={"event": "stream.idle_stop_hook_failed"},
            )

    async def _close_source(self, source: AsyncIterator[str]) -> None:
        aclose = getattr(source, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except Exception:
            _LOGGER.warning(
                "stream.close_failed",
                extra={"event": "stream.close_failed"},
            )


class ReconnectBudget:
    """Bound reconnects per session; forgetting a session frees its counter."""

    def __init__(self, *, max_attempts: int) -> None:
        if max_attempts < 1:
            msg = "max_attempts must be at least one"
            raise LimitsConfigError(msg)
        self._max_attempts = max_attempts
        self._counts: dict[str, int] = {}

    def allow(self, session_id: str) -> None:
        """Record one reconnect attempt or raise :class:`ReconnectBudgetExceeded`."""
        count = self._counts.get(session_id, 0) + 1
        self._counts[session_id] = count
        if count > self._max_attempts:
            raise ReconnectBudgetExceeded(
                detail="the stream reconnect budget is exhausted",
            )

    def reset(self, session_id: str) -> None:
        """Clear the attempt counter for one session."""
        self._counts.pop(session_id, None)

    def forget(self, session_id: str) -> None:
        """Drop all budget state for a closed session."""
        self._counts.pop(session_id, None)

    def snapshot(self) -> dict[str, int]:
        """Return bounded counters only."""
        return {
            "tracked_sessions": len(self._counts),
            "max_attempts": self._max_attempts,
        }


class _SessionRegistry:
    def __init__(
        self,
        *,
        max_sessions: int,
        idle_timeout_seconds: float,
        clock: Callable[[], float],
        retry_after: int,
        reconnect_budget: ReconnectBudget,
    ) -> None:
        self._max_sessions = max_sessions
        self._idle_timeout_seconds = idle_timeout_seconds
        self._clock = clock
        self._retry_after = retry_after
        self._reconnect_budget = reconnect_budget
        self._sessions: dict[str, SessionLease] = {}

    async def acquire(self, device_id: str) -> SessionLease:
        if len(self._sessions) >= self._max_sessions:
            raise SessionLimitExceeded(retry_after=self._retry_after)
        session_id = uuid.uuid4().hex
        lease = SessionLease(
            session_id=session_id,
            device_id=device_id,
            idle_timeout_seconds=self._idle_timeout_seconds,
            clock=self._clock,
        )
        self._sessions[session_id] = lease
        return lease

    async def close(self, session_id: str, *, reason: str) -> bool:
        lease = self._sessions.pop(session_id, None)
        if lease is None:
            return False
        await lease._close(reason)
        self._reconnect_budget.forget(session_id)
        _LOGGER.info(
            "limits.session_closed",
            extra={
                "event": "limits.session_closed",
                "session_id": session_id,
                "device_id": lease.device_id,
                "reason": reason,
            },
        )
        return True

    async def reap(self, *, now: float | None = None) -> tuple[str, ...]:
        moment = self._clock() if now is None else now
        expired = tuple(
            session_id
            for session_id, lease in self._sessions.items()
            if not lease.closed and lease.idle_deadline <= moment
        )
        closed: list[str] = []
        for session_id in expired:
            if await self.close(session_id, reason="idle_timeout"):
                closed.append(session_id)
        return tuple(closed)

    async def close_all(self, *, reason: str) -> tuple[str, ...]:
        closed: list[str] = []
        for session_id in tuple(self._sessions):
            if await self.close(session_id, reason=reason):
                closed.append(session_id)
        return tuple(closed)

    def snapshot(self) -> dict[str, int]:
        return {"active": len(self._sessions), "max": self._max_sessions}


class _RunRegistry:
    def __init__(
        self,
        *,
        max_runs: int,
        max_runs_per_device: int,
        retry_after: int,
    ) -> None:
        self._max_runs = max_runs
        self._max_runs_per_device = max_runs_per_device
        self._retry_after = retry_after
        self._active: set[RunLease] = set()
        self._per_device: dict[str, int] = {}

    async def acquire(self, device_id: str) -> RunLease:
        if len(self._active) >= self._max_runs:
            raise RunLimitExceeded(
                detail="the global concurrent run limit is reached",
                retry_after=self._retry_after,
            )
        device_count = self._per_device.get(device_id, 0)
        if device_count >= self._max_runs_per_device:
            raise RunLimitExceeded(
                detail="the per-device concurrent run limit is reached",
                retry_after=self._retry_after,
            )
        lease = RunLease(device_id=device_id, registry=self)
        self._active.add(lease)
        self._per_device[device_id] = device_count + 1
        return lease

    def _release(self, lease: RunLease) -> None:
        if lease not in self._active:
            return
        self._active.discard(lease)
        device_count = self._per_device.get(lease.device_id, 0) - 1
        if device_count <= 0:
            self._per_device.pop(lease.device_id, None)
        else:
            self._per_device[lease.device_id] = device_count

    def close_all(self) -> tuple[str, ...]:
        for lease in tuple(self._active):
            lease.release()
        return ()

    def snapshot(self) -> dict[str, int]:
        return {
            "active": len(self._active),
            "max": self._max_runs,
            "per_device_max": self._max_runs_per_device,
        }


class _UploadRegistry:
    def __init__(
        self,
        *,
        max_in_flight: int,
        max_queued: int,
        retry_after: int,
    ) -> None:
        if max_queued < 1:
            msg = "max_queued must be at least one"
            raise LimitsConfigError(msg)
        self._max_in_flight = max_in_flight
        self._max_queued = max_queued
        self._retry_after = retry_after
        self._in_flight: set[UploadLease] = set()
        self._queue: deque[UploadLease] = deque()

    def acquire(self, device_id: str) -> UploadLease:
        if len(self._in_flight) < self._max_in_flight:
            lease = UploadLease(device_id=device_id, registry=self, waiting=False)
            self._in_flight.add(lease)
            return lease
        if len(self._queue) >= self._max_queued:
            raise UploadQueueFull(retry_after=self._retry_after)
        lease = UploadLease(device_id=device_id, registry=self, waiting=True)
        self._queue.append(lease)
        return lease

    def _start(self, lease: UploadLease) -> None:
        if lease._released or not lease._waiting:
            return
        if len(self._in_flight) >= self._max_in_flight:
            return
        if lease not in self._queue:
            return
        self._queue.remove(lease)
        lease._waiting = False
        self._in_flight.add(lease)

    def _release(self, lease: UploadLease) -> None:
        if lease._released:
            return
        lease._released = True
        if lease in self._in_flight:
            self._in_flight.discard(lease)
            lease._waiting = False
            self._promote()
        elif lease in self._queue:
            self._queue.remove(lease)
            lease._waiting = False

    def _cancel(self, lease: UploadLease) -> None:
        self._release(lease)

    def _promote(self) -> None:
        while self._queue and len(self._in_flight) < self._max_in_flight:
            lease = self._queue.popleft()
            lease._waiting = False
            self._in_flight.add(lease)

    def close_all(self) -> None:
        for lease in tuple(self._queue):
            lease.cancel()
        for lease in tuple(self._in_flight):
            lease.release()

    def snapshot(self) -> dict[str, int]:
        return {
            "in_flight": len(self._in_flight),
            "queued": len(self._queue),
            "max_in_flight": self._max_in_flight,
            "max_queued": self._max_queued,
        }


class _SsePolicy:
    def __init__(self, *, max_frame_bytes: int, idle_timeout_seconds: float, max_attempts: int):
        self.codec = SseFrameCodec(max_frame_bytes=max_frame_bytes)
        self.reconnect_budget = ReconnectBudget(max_attempts=max_attempts)
        self._idle_timeout_seconds = idle_timeout_seconds

    def guard(self, on_idle: Callable[[], object] | None = None) -> SseStreamGuard:
        """Build a guard that closes a stalled stream and stops the run."""
        return SseStreamGuard(
            idle_timeout_seconds=self._idle_timeout_seconds,
            on_idle=on_idle,
        )


def _validate_settings(settings: Settings) -> None:
    if settings.max_concurrent_runs_per_device > settings.max_concurrent_runs:
        msg = "max_concurrent_runs_per_device must not exceed max_concurrent_runs"
        raise LimitsConfigError(msg)
    categories = (
        ("run", settings.rate_limit_run_per_minute, settings.rate_limit_run_burst),
        ("upload", settings.rate_limit_upload_per_minute, settings.rate_limit_upload_burst),
        ("download", settings.rate_limit_download_per_minute, settings.rate_limit_download_burst),
    )
    for category, per_minute, burst in categories:
        if burst > per_minute:
            msg = f"rate_limit_{category}_burst must not exceed rate_limit_{category}_per_minute"
            raise LimitsConfigError(msg)
    if settings.max_queued_uploads < 1:
        msg = "max_queued_uploads must be at least one"
        raise LimitsConfigError(msg)


class AdmissionController:
    """Admission decisions for sessions, runs, uploads, and rate limits."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock if clock is not None else time.monotonic
        retry_after = settings.reject_retry_after_seconds
        rate_limits = {
            "run": RateLimit(
                per_minute=settings.rate_limit_run_per_minute,
                burst=settings.rate_limit_run_burst,
            ),
            "upload": RateLimit(
                per_minute=settings.rate_limit_upload_per_minute,
                burst=settings.rate_limit_upload_burst,
            ),
            "download": RateLimit(
                per_minute=settings.rate_limit_download_per_minute,
                burst=settings.rate_limit_download_burst,
            ),
        }
        self._rate_limiter = DeviceRateLimiter(
            rate_limits,
            max_devices=settings.rate_limit_max_devices,
            clock=self._clock,
        )
        self._reconnect_budget = ReconnectBudget(max_attempts=settings.sse_max_reconnect_attempts)
        self._sessions = _SessionRegistry(
            max_sessions=settings.max_concurrent_sessions,
            idle_timeout_seconds=settings.session_idle_timeout_seconds,
            clock=self._clock,
            retry_after=retry_after,
            reconnect_budget=self._reconnect_budget,
        )
        self._runs = _RunRegistry(
            max_runs=settings.max_concurrent_runs,
            max_runs_per_device=settings.max_concurrent_runs_per_device,
            retry_after=retry_after,
        )
        self._uploads = _UploadRegistry(
            max_in_flight=settings.max_concurrent_uploads,
            max_queued=settings.max_queued_uploads,
            retry_after=retry_after,
        )
        self._barge_in = BargeInGate(
            min_speech_ms=settings.barge_in_min_speech_ms,
            cooldown_ms=settings.barge_in_cooldown_ms,
            clock=self._clock,
        )
        self._sse = _SsePolicy(
            max_frame_bytes=settings.max_sse_frame_bytes,
            idle_timeout_seconds=settings.sse_idle_timeout_seconds,
            max_attempts=settings.sse_max_reconnect_attempts,
        )

    @property
    def sessions(self) -> _SessionRegistry:
        """Session capacity registry."""
        return self._sessions

    @property
    def runs(self) -> _RunRegistry:
        """Run capacity registry."""
        return self._runs

    @property
    def uploads(self) -> _UploadRegistry:
        """Upload slot and queue registry."""
        return self._uploads

    @property
    def barge_in(self) -> BargeInGate:
        """Barge-in debounce gate."""
        return self._barge_in

    @property
    def sse(self) -> _SsePolicy:
        """SSE frame, idle, and reconnect policy."""
        return self._sse

    def category_for(self, method: str, path: str) -> str | None:
        """Return the rate-limit category for a method and path prefix, if any."""
        normalized_method = method.upper()
        for route_method, prefix, category in RATE_LIMIT_ROUTES:
            if normalized_method != route_method:
                continue
            if path == prefix or path.startswith(prefix + "/"):
                return category
        return None

    def check_rate_limit(self, device_id: str, category: str) -> None:
        """Consume one request token for a device and category."""
        self._rate_limiter.check(device_id, category)

    async def acquire_session(self, device_id: str) -> SessionLease:
        """Admit one call session or raise :class:`SessionLimitExceeded`."""
        return await self._sessions.acquire(device_id)

    async def acquire_run(self, device_id: str) -> RunLease:
        """Admit one run or raise :class:`RunLimitExceeded`."""
        return await self._runs.acquire(device_id)

    def acquire_upload(self, device_id: str) -> UploadLease:
        """Admit or queue one upload, or raise :class:`UploadQueueFull`."""
        return self._uploads.acquire(device_id)

    async def close_session(self, session_id: str, *, reason: str) -> bool:
        """Close one session and run its stop hooks exactly once."""
        return await self._sessions.close(session_id, reason=reason)

    async def reap_idle_sessions(self, *, now: float | None = None) -> tuple[str, ...]:
        """Close every session whose idle deadline has passed."""
        return await self._sessions.reap(now=now)

    async def close_all(self, *, reason: str) -> tuple[str, ...]:
        """Close every open session and run, and clear the upload queues."""
        closed = await self._sessions.close_all(reason=reason)
        self._runs.close_all()
        self._uploads.close_all()
        return closed

    def snapshot(self) -> dict[str, object]:
        """Return counters and limits only; never tokens or paths."""
        return {
            "sessions": self._sessions.snapshot(),
            "runs": self._runs.snapshot(),
            "uploads": self._uploads.snapshot(),
            "rate_limit": self._rate_limiter.snapshot(),
            "barge_in": self._barge_in.snapshot(),
        }


def build_limits(
    settings: Settings,
    *,
    clock: Callable[[], float] | None = None,
) -> AdmissionController:
    """Build an admission controller, rejecting inconsistent settings."""
    _validate_settings(settings)
    return AdmissionController(settings, clock=clock)


__all__ = [
    "RATE_LIMIT_ROUTES",
    "AdmissionController",
    "BargeInGate",
    "DeviceRateLimiter",
    "FrameTooLargeError",
    "LimitError",
    "LimitsConfigError",
    "RateLimit",
    "RateLimitExceeded",
    "ReconnectBudget",
    "ReconnectBudgetExceeded",
    "RunLease",
    "RunLimitExceeded",
    "SessionLease",
    "SessionLimitExceeded",
    "SseFrameCodec",
    "SseStreamGuard",
    "StreamIdleTimeout",
    "TokenBucket",
    "UploadLease",
    "UploadQueueFull",
    "build_limits",
]

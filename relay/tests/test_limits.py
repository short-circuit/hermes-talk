"""Offline tests for relay admission control, rate limits, and stream hygiene.

Everything runs in-process through ``httpx.ASGITransport`` and a fake monotonic
clock; no sockets are opened and no network calls are made.
"""

from __future__ import annotations

import asyncio
import gc
import tracemalloc
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from pydantic import ValidationError
from relay.app import require_device_token
from relay.auth.tokens import DeviceToken
from relay.config import Settings
from relay.limits import (
    BargeInGate,
    DeviceRateLimiter,
    FrameTooLargeError,
    LimitError,
    LimitsConfigError,
    RateLimit,
    RateLimitExceeded,
    ReconnectBudget,
    ReconnectBudgetExceeded,
    RunLimitExceeded,
    SessionLimitExceeded,
    SseFrameCodec,
    SseStreamGuard,
    StreamIdleTimeout,
    TokenBucket,
    UploadQueueFull,
    build_limits,
)

_BASE_URL = "http://relay.test"
_SPOKEN_RATE_LIMITED = "One moment — the relay is handling too many requests."
_SPOKEN_UPLOAD_QUEUE_FULL = "The upload queue is full. Try again in a moment."
_SPOKEN_SESSION_LIMIT = "The relay is already on a call. Try again shortly."
_SPOKEN_RUN_LIMIT = "Hermes is busy with another request. Try again shortly."
_SPOKEN_RECONNECT_BUDGET = "The connection kept dropping. Start a new call."
_SPOKEN_IDLE_STREAM = "The connection went quiet, so I stopped the run."


class FakeClock:
    """Deterministic monotonic clock for admission-control tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as client:
        return await client.request(method, path, headers=headers)


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _runs_app(make_app: Callable[..., FastAPI], **overrides: Any) -> FastAPI:
    app = make_app(**overrides)

    @app.post("/v1/runs")
    async def create_run(
        device: Annotated[DeviceToken, Depends(require_device_token)],
    ) -> dict[str, str]:
        return {"token_id": device.token_id}

    return app


def test_settings_defaults_are_the_documented_safe_values() -> None:
    settings = _settings()

    assert settings.max_concurrent_sessions == 3
    assert settings.max_concurrent_runs == 2
    assert settings.max_concurrent_runs_per_device == 1
    assert settings.max_concurrent_uploads == 2
    assert settings.max_queued_uploads == 8
    assert settings.session_idle_timeout_seconds == 120.0
    assert settings.session_reaper_interval_seconds == 5.0
    assert settings.rate_limit_run_per_minute == 20
    assert settings.rate_limit_run_burst == 5
    assert settings.rate_limit_upload_per_minute == 30
    assert settings.rate_limit_upload_burst == 10
    assert settings.rate_limit_download_per_minute == 120
    assert settings.rate_limit_download_burst == 30
    assert settings.rate_limit_max_devices == 64
    assert settings.max_sse_frame_bytes == 65536
    assert settings.sse_idle_timeout_seconds == 30.0
    assert settings.sse_max_reconnect_attempts == 5
    assert settings.barge_in_min_speech_ms == 300
    assert settings.barge_in_cooldown_ms == 1000
    assert settings.reject_retry_after_seconds == 2


@pytest.mark.parametrize(
    "field",
    [
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
        "barge_in_min_speech_ms",
        "reject_retry_after_seconds",
    ],
)
def test_zero_and_negative_integer_limits_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: 0})
    with pytest.raises(ValidationError):
        _settings(**{field: -1})


@pytest.mark.parametrize(
    "field",
    [
        "session_idle_timeout_seconds",
        "session_reaper_interval_seconds",
        "sse_idle_timeout_seconds",
    ],
)
def test_zero_negative_and_infinite_timeouts_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: 0})
    with pytest.raises(ValidationError):
        _settings(**{field: -1})
    with pytest.raises(ValidationError):
        _settings(**{field: float("inf")})


def test_barge_in_cooldown_may_be_zero_but_min_speech_may_not() -> None:
    settings = _settings(barge_in_cooldown_ms=0)
    assert settings.barge_in_cooldown_ms == 0
    with pytest.raises(ValidationError):
        _settings(barge_in_min_speech_ms=0)


def test_inconsistent_limits_are_rejected() -> None:
    with pytest.raises(LimitsConfigError):
        build_limits(_settings(max_concurrent_runs=2, max_concurrent_runs_per_device=3))
    with pytest.raises(LimitsConfigError):
        build_limits(_settings(rate_limit_run_per_minute=4, rate_limit_run_burst=5))
    with pytest.raises(LimitsConfigError):
        build_limits(_settings(rate_limit_upload_per_minute=4, rate_limit_upload_burst=5))
    with pytest.raises(LimitsConfigError):
        build_limits(_settings(rate_limit_download_per_minute=4, rate_limit_download_burst=5))
    settings = _settings()
    settings.max_queued_uploads = 0
    with pytest.raises(LimitsConfigError):
        build_limits(settings)


def test_limit_error_body_headers_and_spoken_contract() -> None:
    exc = RateLimitExceeded(retry_after=2)

    body = exc.to_body()
    assert body == {
        "error": "rate_limited",
        "detail": exc.detail,
        "spoken": _SPOKEN_RATE_LIMITED,
        "retry_after": 2,
    }
    assert "correlation_id" not in body
    assert exc.headers() == {"Retry-After": "2"}

    with_correlation = exc.to_body("abc123")
    assert with_correlation["correlation_id"] == "abc123"

    assert SessionLimitExceeded().headers() == {}
    assert UploadQueueFull(retry_after=2).to_body()["error"] == "upload_queue_full"
    assert RunLimitExceeded().to_body()["error"] == "run_limit_reached"
    assert ReconnectBudgetExceeded().to_body()["error"] == "reconnect_budget_exhausted"
    assert not issubclass(StreamIdleTimeout, LimitError)


def test_token_bucket_consumes_burst_then_refills() -> None:
    clock = FakeClock()
    bucket = TokenBucket(capacity=2, refill_per_second=0.5, clock=clock)

    assert bucket.try_consume() == (True, 0.0)
    assert bucket.try_consume() == (True, 0.0)
    ok, retry_after = bucket.try_consume()
    assert ok is False
    assert retry_after == pytest.approx(2.0)

    clock.advance(2.0)
    ok, _ = bucket.try_consume()
    assert ok is True


def test_rate_limit_burst_then_429_and_refill() -> None:
    clock = FakeClock()
    limiter = DeviceRateLimiter(
        {"run": RateLimit(per_minute=60, burst=3)},
        max_devices=8,
        clock=clock,
    )

    for _ in range(3):
        limiter.check("device-a", "run")

    with pytest.raises(RateLimitExceeded) as exc_info:
        limiter.check("device-a", "run")
    exc = exc_info.value
    assert exc.status_code == 429
    assert exc.code == "rate_limited"
    assert exc.spoken == _SPOKEN_RATE_LIMITED
    assert exc.retry_after is not None
    assert exc.retry_after >= 1
    assert exc.headers()["Retry-After"] == str(exc.retry_after)

    clock.advance(1.0)
    limiter.check("device-a", "run")


def test_rate_limits_are_isolated_per_device_and_category() -> None:
    clock = FakeClock()
    limiter = DeviceRateLimiter(
        {
            "run": RateLimit(per_minute=60, burst=1),
            "upload": RateLimit(per_minute=60, burst=1),
        },
        max_devices=8,
        clock=clock,
    )

    limiter.check("device-a", "run")
    with pytest.raises(RateLimitExceeded):
        limiter.check("device-a", "run")

    limiter.check("device-b", "run")
    limiter.check("device-a", "upload")


def test_tracked_devices_are_lru_bounded() -> None:
    clock = FakeClock()
    limiter = DeviceRateLimiter(
        {"run": RateLimit(per_minute=60, burst=5)},
        max_devices=4,
        clock=clock,
    )

    for index in range(500):
        limiter.check(f"device-{index:04d}", "run")
        assert limiter.snapshot()["tracked_devices"] <= 4

    snapshot = limiter.snapshot()
    assert snapshot["tracked_devices"] == 4
    assert snapshot["max_devices"] == 4


def test_session_cap_refuses_next_and_release_admits_it() -> None:
    clock = FakeClock()
    limits = build_limits(_settings(max_concurrent_sessions=2), clock=clock)

    async def scenario() -> None:
        first = await limits.acquire_session("device-a")
        second = await limits.acquire_session("device-b")

        with pytest.raises(SessionLimitExceeded) as exc_info:
            await limits.acquire_session("device-c")
        exc = exc_info.value
        assert exc.status_code == 503
        assert exc.code == "session_limit_reached"
        assert exc.spoken == _SPOKEN_SESSION_LIMIT
        assert exc.retry_after is not None

        first.touch()
        second.touch()
        assert first.closed is False
        assert second.closed is False
        assert first.idle_deadline > clock()

        assert await limits.close_session(first.session_id, reason="test") is True
        third = await limits.acquire_session("device-c")
        assert third.closed is False
        assert limits.snapshot()["sessions"] == {"active": 2, "max": 2}

    asyncio.run(scenario())


def test_run_caps_global_and_per_device_and_counters_are_deleted() -> None:
    limits = build_limits(
        _settings(max_concurrent_runs=2, max_concurrent_runs_per_device=1),
        clock=FakeClock(),
    )

    async def scenario() -> None:
        first = await limits.acquire_run("device-a")

        with pytest.raises(RunLimitExceeded) as per_device:
            await limits.acquire_run("device-a")
        assert per_device.value.status_code == 503
        assert per_device.value.code == "run_limit_reached"
        assert per_device.value.spoken == _SPOKEN_RUN_LIMIT

        second = await limits.acquire_run("device-b")

        with pytest.raises(RunLimitExceeded) as global_cap:
            await limits.acquire_run("device-c")
        assert global_cap.value.code == "run_limit_reached"

        first.release()
        assert "device-a" not in limits.runs._per_device

        third = await limits.acquire_run("device-c")
        assert limits.snapshot()["runs"] == {
            "active": 2,
            "max": 2,
            "per_device_max": 1,
        }

        second.release()
        third.release()
        assert limits.runs._per_device == {}
        assert limits.snapshot()["runs"]["active"] == 0

    asyncio.run(scenario())


def test_session_stop_hooks_run_exactly_once_on_close_reap_and_close_all() -> None:
    clock = FakeClock()
    limits = build_limits(
        _settings(max_concurrent_sessions=3, session_idle_timeout_seconds=10.0),
        clock=clock,
    )
    calls: list[str] = []

    def sync_hook() -> None:
        calls.append("sync")

    async def async_hook() -> None:
        calls.append("async")

    async def scenario() -> None:
        lease = await limits.acquire_session("device-a")
        lease.register_stop(sync_hook)
        lease.register_stop(async_hook)

        assert await limits.close_session(lease.session_id, reason="test") is True
        assert lease.closed is True
        assert calls == ["sync", "async"]

        assert await limits.close_session(lease.session_id, reason="duplicate") is False
        assert calls == ["sync", "async"]

        reaped_lease = await limits.acquire_session("device-b")
        reaped_lease.register_stop(sync_hook)
        clock.advance(11.0)
        reaped = await limits.reap_idle_sessions()
        assert reaped == (reaped_lease.session_id,)
        assert reaped_lease.closed is True
        assert calls == ["sync", "async", "sync"]

        run = await limits.acquire_run("device-b")
        last_lease = await limits.acquire_session("device-c")
        last_lease.register_stop(sync_hook)
        closed = await limits.close_all(reason="shutdown")
        assert closed == (last_lease.session_id,)
        assert last_lease.closed is True
        assert run.released is True
        assert calls == ["sync", "async", "sync", "sync"]
        assert limits.snapshot()["sessions"]["active"] == 0
        assert limits.snapshot()["runs"]["active"] == 0

    asyncio.run(scenario())


def test_upload_flood_is_bounded_and_release_promotes_fifo() -> None:
    limits = build_limits(
        _settings(max_concurrent_uploads=2, max_queued_uploads=3),
        clock=FakeClock(),
    )

    in_flight = [limits.acquire_upload("device-a") for _ in range(2)]
    assert all(lease.waiting is False for lease in in_flight)

    queued = [limits.acquire_upload("device-b") for _ in range(3)]
    assert all(lease.waiting is True for lease in queued)

    with pytest.raises(UploadQueueFull) as exc_info:
        limits.acquire_upload("device-c")
    exc = exc_info.value
    assert exc.status_code == 429
    assert exc.code == "upload_queue_full"
    assert exc.spoken == _SPOKEN_UPLOAD_QUEUE_FULL
    assert exc.retry_after is not None

    snapshot = limits.uploads.snapshot()
    assert snapshot["in_flight"] == 2
    assert snapshot["queued"] == 3

    in_flight[0].release()
    in_flight[0].release()
    promoted = queued[0]
    assert promoted.waiting is False
    assert limits.uploads.snapshot()["queued"] == 2
    assert limits.uploads.snapshot()["in_flight"] == 2

    queued[1].cancel()
    assert limits.uploads.snapshot()["queued"] == 1

    asyncio.run(limits.close_all(reason="test"))
    assert limits.uploads.snapshot()["in_flight"] == 0
    assert limits.uploads.snapshot()["queued"] == 0


def test_ten_minute_soak_stays_bounded() -> None:
    clock = FakeClock()
    limits = build_limits(
        _settings(
            rate_limit_max_devices=16,
            max_concurrent_uploads=2,
            max_queued_uploads=4,
            rate_limit_run_per_minute=10,
            rate_limit_run_burst=2,
            rate_limit_upload_per_minute=10,
            rate_limit_upload_burst=2,
            rate_limit_download_per_minute=10,
            rate_limit_download_burst=2,
        ),
        clock=clock,
    )
    attempts = 200_000
    step = 600.0 / attempts
    uploads: list[Any] = []
    rejections = 0

    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    try:
        for index in range(attempts):
            clock.advance(step)
            device_id = f"device-{index % 4:04d}"
            category = ("run", "upload", "download")[index % 3]
            try:
                limits.check_rate_limit(device_id, category)
            except LimitError as exc:
                assert exc.status_code == 429
                rejections += 1

            if index % 20 == 0:
                try:
                    uploads.append(limits.acquire_upload(device_id))
                except LimitError as exc:
                    assert exc.status_code == 429
                    rejections += 1
                if len(uploads) > 4:
                    uploads.pop(0).release()

        gc.collect()
        current = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()

    snapshot = limits.snapshot()
    rate_limit = snapshot["rate_limit"]
    uploads_snapshot = snapshot["uploads"]
    assert rejections > 0
    assert rate_limit["tracked_devices"] <= 16
    assert uploads_snapshot["queued"] <= 4
    assert uploads_snapshot["in_flight"] <= 2
    assert current - baseline < 512 * 1024


class _StallingSource:
    def __init__(self) -> None:
        self._yielded = False
        self.closed = False

    def __aiter__(self) -> AsyncIterator[str]:
        return self

    async def __anext__(self) -> str:
        if not self._yielded:
            self._yielded = True
            return "event: message\ndata: hello\n\n"
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


def test_stalled_stream_times_out_stops_run_and_closes_source() -> None:
    source = _StallingSource()
    calls: list[str] = []
    guard = SseStreamGuard(
        idle_timeout_seconds=0.05,
        on_idle=lambda: calls.append("stop"),
    )

    async def consume() -> tuple[list[str], StreamIdleTimeout]:
        frames: list[str] = []
        with pytest.raises(StreamIdleTimeout) as exc_info:
            async for frame in guard.guard(source):
                frames.append(frame)
        return frames, exc_info.value

    frames, exc = asyncio.run(consume())
    assert frames == ["event: message\ndata: hello\n\n"]
    assert calls == ["stop"]
    assert source.closed is True
    assert exc.idle_timeout_seconds == 0.05
    assert exc.spoken == _SPOKEN_IDLE_STREAM


def test_sse_frame_cap_allows_limit_and_rejects_limit_plus_one() -> None:
    event = "message"
    max_frame_bytes = 64
    overhead = len(f"event: {event}\ndata: \n\n")
    data = "x" * (max_frame_bytes - overhead)
    codec = SseFrameCodec(max_frame_bytes=max_frame_bytes)

    frame = codec.encode(event, data)
    assert len(frame) == max_frame_bytes

    with pytest.raises(FrameTooLargeError) as exc_info:
        codec.encode(event, data + "x")
    exc = exc_info.value
    assert exc.size == max_frame_bytes + 1
    assert exc.max_bytes == max_frame_bytes

    error_frame = codec.encode_error("One moment.", "rate_limited")
    assert len(error_frame) <= max_frame_bytes
    assert error_frame.startswith(b"event: error\ndata: ")


def test_reconnect_budget_is_bounded_and_resettable() -> None:
    budget = ReconnectBudget(max_attempts=5)

    for _ in range(5):
        budget.allow("session-1")

    with pytest.raises(ReconnectBudgetExceeded) as exc_info:
        budget.allow("session-1")
    exc = exc_info.value
    assert exc.status_code == 429
    assert exc.code == "reconnect_budget_exhausted"
    assert exc.spoken == _SPOKEN_RECONNECT_BUDGET
    assert budget.snapshot() == {"tracked_sessions": 1, "max_attempts": 5}

    budget.reset("session-1")
    assert budget.snapshot()["tracked_sessions"] == 0

    for _ in range(5):
        budget.allow("session-1")
    budget.forget("session-1")
    assert budget.snapshot()["tracked_sessions"] == 0


def test_barge_in_storm_produces_exactly_one_stop() -> None:
    clock = FakeClock()
    gate = BargeInGate(min_speech_ms=300, cooldown_ms=1000, clock=clock)

    for _ in range(50):
        assert gate.consider(200) is False
    assert gate.snapshot()["suppressed_short"] == 50
    assert gate.snapshot()["accepted"] == 0

    assert gate.consider(400) is True
    for _ in range(49):
        assert gate.consider(400) is False
    snapshot = gate.snapshot()
    assert snapshot["accepted"] == 1
    assert snapshot["suppressed_cooldown"] == 49

    clock.advance(1.0)
    assert gate.consider(400) is True
    assert gate.snapshot()["accepted"] == 2


def test_category_for_uses_the_documented_route_prefixes() -> None:
    limits = build_limits(_settings(), clock=FakeClock())

    assert limits.category_for("POST", "/v1/runs") == "run"
    assert limits.category_for("POST", "/v1/runs/abc") == "run"
    assert limits.category_for("POST", "/v1/attachments") == "upload"
    assert limits.category_for("PUT", "/v1/attachments/file") == "upload"
    assert limits.category_for("GET", "/v1/artifacts") == "download"
    assert limits.category_for("HEAD", "/v1/artifacts/report") == "download"
    assert limits.category_for("GET", "/v1/runs") is None
    assert limits.category_for("DELETE", "/v1/runs/abc") is None
    assert limits.category_for("GET", "/health") is None


def test_snapshot_exposes_only_counters_and_limits() -> None:
    limits = build_limits(_settings(), clock=FakeClock())

    assert limits.snapshot() == {
        "sessions": {"active": 0, "max": 3},
        "runs": {"active": 0, "max": 2, "per_device_max": 1},
        "uploads": {"in_flight": 0, "queued": 0, "max_in_flight": 2, "max_queued": 8},
        "rate_limit": {"tracked_devices": 0, "max_devices": 64},
        "barge_in": {"accepted": 0, "suppressed_short": 0, "suppressed_cooldown": 0},
    }


def test_middleware_rate_limits_one_device_without_throttling_others(
    make_app: Callable[..., FastAPI],
) -> None:
    app = _runs_app(
        make_app,
        device_tokens_raw="token-a, token-b",
        rate_limit_run_per_minute=2,
        rate_limit_run_burst=2,
    )
    headers_a = {"Authorization": "Bearer token-a"}
    headers_b = {"Authorization": "Bearer token-b"}

    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        first = await _request(app, "POST", "/v1/runs", headers=headers_a)
        second = await _request(app, "POST", "/v1/runs", headers=headers_a)
        third = await _request(app, "POST", "/v1/runs", headers=headers_a)
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    body = third.json()
    assert body["error"] == "rate_limited"
    assert body["spoken"] == _SPOKEN_RATE_LIMITED
    assert "correlation_id" in body
    assert int(third.headers["Retry-After"]) >= 1

    second_device = asyncio.run(_request(app, "POST", "/v1/runs", headers=headers_b))
    assert second_device.status_code == 200

    health = asyncio.run(_request(app, "GET", "/health"))
    assert health.status_code == 200

    ready = asyncio.run(_request(app, "GET", "/ready"))
    assert ready.status_code in {200, 503}
    assert ready.status_code != 429

    missing_token = asyncio.run(_request(app, "POST", "/v1/runs"))
    assert missing_token.status_code == 401
    assert missing_token.json()["error"] == "unauthorized"


def test_http_session_limit_refuses_next_call_while_existing_calls_continue(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app(max_concurrent_sessions=1, device_tokens_raw="token-a")

    @app.post("/smoke/sessions")
    async def open_session(request: Request) -> dict[str, str]:
        lease = await request.app.state.limits.acquire_session("device-a")
        lease.register_stop(lambda: None)
        return {"session_id": lease.session_id}

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        first = await _request(app, "POST", "/smoke/sessions")
        second = await _request(app, "POST", "/smoke/sessions")
        return first, second

    first, second = asyncio.run(scenario())
    assert first.status_code == 200
    assert second.status_code == 503
    body = second.json()
    assert body["error"] == "session_limit_reached"
    assert body["spoken"] == _SPOKEN_SESSION_LIMIT
    assert int(second.headers["Retry-After"]) >= 1

    ready = asyncio.run(_request(app, "GET", "/ready"))
    limits_check = next(check for check in ready.json()["checks"] if check["name"] == "limits")
    assert "sessions 1/1" in limits_check["detail"]


def test_ready_reports_limits_without_leaking_token_values(
    make_app: Callable[..., FastAPI],
    tmp_path: Any,
) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    token = "super-secret-device-token"
    app = make_app(
        inbox_dir=str(inbox),
        outbox_dir=str(outbox),
        data_dir=str(tmp_path),
        device_tokens_raw=token,
    )

    response = asyncio.run(_request(app, "GET", "/ready"))
    assert response.status_code == 200
    body = response.json()
    checks = {check["name"]: check for check in body["checks"]}
    assert checks["limits"]["ok"] is True
    assert "sessions 0/3" in checks["limits"]["detail"]
    assert "runs 0/2" in checks["limits"]["detail"]
    assert "uploads 0 in flight, 0/8 queued" in checks["limits"]["detail"]
    assert token not in response.text

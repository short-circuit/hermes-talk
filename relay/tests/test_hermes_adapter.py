"""Offline tests for the Hermes run adapter against a loopback stub."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from relay.config import Settings
from relay.hermes.client import (
    HermesAuthError,
    HermesClient,
    HermesConfigError,
    HermesConflictError,
    HermesProtocolError,
    HermesRunGoneError,
    HermesTimeoutError,
)
from relay.hermes.events import EventGate, EventKind, HermesEventError, RunEvent
from relay.tests._hermes_stub import HermesStub, make_stub

_API_KEY = "stub-test-key-0123456789"
_SESSION_KEY = "voice-relay-test"


def _client(stub: HermesStub, **overrides: object) -> HermesClient:
    return HermesClient(
        stub.base_url,
        _API_KEY,
        session_key=_SESSION_KEY,
        timeout_s=2.0,
        connect_timeout_s=1.0,
        stop_timeout_s=2.0,
        stream_timeout_s=2.0,
        reconnect_backoff_s=0.01,
        max_reconnect_backoff_s=0.05,
        **overrides,
    )


async def _collect(events: AsyncIterator[RunEvent]) -> list[RunEvent]:
    return [event async for event in events]


@pytest.fixture
def stub() -> HermesStub:
    server = HermesStub()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_fixture_replay_orders_events_and_yields_terminal_once(stub: HermesStub) -> None:
    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("Use the terminal tool to run uname -s")
            events = await _collect(client.events(admission.run_id))

            assert [event.kind for event in events] == [
                EventKind.TOOL_STARTED,
                EventKind.TOOL_COMPLETED,
                EventKind.MESSAGE_INTERIM,
                EventKind.RUN_COMPLETED,
            ]
            assert [event.seq for event in events] == [0, 1, 2, 3]
            assert events[0].tool == "terminal"
            assert events[0].tool_call_id == "call_305c3106aab2477a9b0c1f1e"
            assert events[1].tool == "terminal"
            assert events[1].payload["duration"] == 0
            assert events[1].payload["error"] is False
            text = "".join(
                event.text_delta or ""
                for event in events
                if event.kind is EventKind.MESSAGE_INTERIM
            )
            assert text.strip() == "Linux"
            assert events[-1].terminal is True
            assert events[-1].output == "Linux"
            assert sum(1 for event in events if event.terminal) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_submit_run_sends_session_and_idempotency_headers(stub: HermesStub) -> None:
    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run(
                "hello",
                session_id="session-from-client",
                idempotency_key="idem-test-1",
            )
            assert admission.run_id == stub.run_id
            assert admission.session_key == _SESSION_KEY
            assert not hasattr(admission, "session_id")

            recorded = [
                request
                for request in stub.requests
                if request["method"] == "POST" and request["path"] == "/v1/runs"
            ]
            assert len(recorded) == 1
            assert recorded[0]["json"]["session_id"] == "session-from-client"
            assert recorded[0]["headers"]["x-hermes-session-key"] == _SESSION_KEY
            assert recorded[0]["headers"]["idempotency-key"] == "idem-test-1"
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_submit_run_sends_validated_content_array(stub: HermesStub) -> None:
    async def scenario() -> None:
        client = _client(stub)
        try:
            await client.submit_run(
                [
                    {"type": "text", "text": "hi"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ]
            )

            recorded = [
                request
                for request in stub.requests
                if request["method"] == "POST" and request["path"] == "/v1/runs"
            ]
            assert len(recorded) == 1
            assert recorded[0]["json"] == {
                "input": [
                    {"type": "text", "text": "hi"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ]
            }
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_submit_run_preserves_unknown_part_keys(stub: HermesStub) -> None:
    async def scenario() -> None:
        client = _client(stub)
        try:
            parts = [
                {"type": "text", "text": "hi", "note": "keep"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA", "detail": "high"},
                    "cache": True,
                },
            ]
            await client.submit_run(parts)

            recorded = [
                request
                for request in stub.requests
                if request["method"] == "POST" and request["path"] == "/v1/runs"
            ]
            assert len(recorded) == 1
            assert recorded[0]["json"] == {"input": parts}
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_submit_run_rejects_empty_content_array_before_any_request(
    stub: HermesStub,
) -> None:
    async def scenario() -> None:
        client = _client(stub)
        try:
            with pytest.raises(HermesConfigError):
                await client.submit_run([])
            assert stub.requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "parts",
    [
        ["hi"],
        [{"type": "audio", "data": "x"}],
        [{"type": "text", "text": 123}],
        [{"type": "text"}],
        [{"type": "image_url", "image_url": {}}],
        [{"type": "image_url", "image_url": {"url": 123}}],
        [{"type": "image_url", "image_url": "data:image/png;base64,AAAA"}],
        b"hi",
        bytearray(b"hi"),
    ],
)
def test_submit_run_rejects_invalid_content_array_before_any_request(
    stub: HermesStub,
    parts: object,
) -> None:
    async def scenario() -> None:
        client = _client(stub)
        try:
            with pytest.raises(HermesConfigError):
                await client.submit_run(parts)  # type: ignore[arg-type]
            assert stub.requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_submit_run_sends_plain_string_unchanged(stub: HermesStub) -> None:
    async def scenario() -> None:
        client = _client(stub)
        try:
            await client.submit_run("Use the terminal tool to run uname -s")

            recorded = [
                request
                for request in stub.requests
                if request["method"] == "POST" and request["path"] == "/v1/runs"
            ]
            assert len(recorded) == 1
            assert recorded[0]["json"] == {"input": "Use the terminal tool to run uname -s"}
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_stop_cancels_stream_promptly(stub: HermesStub) -> None:
    stub.hold_open = True

    async def scenario() -> None:
        client = _client(stub)
        observed: list[RunEvent] = []

        async def consume() -> None:
            async for event in client.events(admission.run_id):
                observed.append(event)

        admission = await client.submit_run("wait for stop")
        task = asyncio.create_task(consume())
        try:
            await asyncio.sleep(0.05)
            stop_started = time.monotonic()
            result = await client.stop(admission.run_id)
            assert result.elapsed_s < 0.5
            assert time.monotonic() - stop_started < 0.5

            await asyncio.wait_for(task, timeout=1.0)
            cancelled = [event for event in observed if event.kind is EventKind.RUN_CANCELLED]
            assert len(cancelled) == 1
            assert cancelled[0].interrupted is True
            assert time.monotonic() - stop_started < 0.5
        finally:
            if not task.done():
                task.cancel()
            await client.aclose()

    asyncio.run(scenario())


def test_dropped_stream_reconnects_from_last_seq(stub: HermesStub) -> None:
    stub.drop_after_seq = 1

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("resume me")
            events = await _collect(client.events(admission.run_id))

            assert [event.seq for event in events] == [0, 1, 2, 3]
            assert sum(1 for event in events if event.terminal) == 1

            event_requests = [
                request
                for request in stub.requests
                if request["path"] == f"/v1/runs/{admission.run_id}/events"
            ]
            assert len(event_requests) >= 2
            assert event_requests[0]["last_seq"] is None
            assert event_requests[0]["last_event_id"] is None
            assert event_requests[1]["last_event_id"] == "1"
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_abrupt_stream_abort_reconnects_and_yields_terminal_once(stub: HermesStub) -> None:
    stub.abort_after_seq = 1

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("abort mid-stream")
            events = await _collect(client.events(admission.run_id))

            assert [event.seq for event in events] == [0, 1, 2, 3]
            assert sum(1 for event in events if event.terminal) == 1

            event_requests = [
                request
                for request in stub.requests
                if request["path"] == f"/v1/runs/{admission.run_id}/events"
            ]
            assert len(event_requests) >= 2
            assert event_requests[1]["last_event_id"] == "1"
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_status_fallback_when_stream_ends_without_terminal(stub: HermesStub) -> None:
    stub.drop_after_seq = 1
    stub.hold_open = False

    async def scenario() -> None:
        client = _client(stub, max_reconnects=0)
        try:
            admission = await client.submit_run("fall back to status")
            events = await _collect(client.events(admission.run_id))

            assert [event.seq for event in events] == [0, 1, 2]
            assert sum(1 for event in events if event.terminal) == 1
            assert events[-1].kind is EventKind.RUN_COMPLETED
            assert events[-1].output == "Linux"

            status_requests = [
                request
                for request in stub.requests
                if request["method"] == "GET" and request["path"] == f"/v1/runs/{admission.run_id}"
            ]
            assert len(status_requests) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_gate_run_id_mismatch_becomes_protocol_error(stub: HermesStub) -> None:
    frame = stub.make_frame("message.delta", 0, delta="hi")
    frame["data"]["run_id"] = "run_other"
    stub.frames = [frame]

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("foreign run id")
            with pytest.raises(HermesProtocolError) as raised:
                async for _event in client.events(admission.run_id):
                    pass

            assert str(raised.value).startswith("run stream contract violation")
            assert "run_other" in str(raised.value)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_gate_drops_duplicates_and_post_terminal_events() -> None:
    run_id = "run_gate"
    gate = EventGate(run_id)

    first = RunEvent(EventKind.TOOL_STARTED, "tool.started", 0, run_id, 1.0, {})
    assert gate.observe(first) is True
    assert gate.last_seq == 0
    assert gate.observe(first) is False

    terminal = RunEvent(
        EventKind.RUN_COMPLETED,
        "run.completed",
        3,
        run_id,
        2.0,
        {"output": "done"},
    )
    assert gate.observe(terminal) is True
    assert gate.terminal_seen is True
    assert gate.observe(terminal) is False
    assert (
        gate.observe(RunEvent(EventKind.MESSAGE_INTERIM, "message.delta", 4, run_id, 3.0, {}))
        is False
    )

    with pytest.raises(HermesEventError):
        gate.observe(RunEvent(EventKind.UNKNOWN, "x", 5, "run_other", 4.0, {}))

    resumed = EventGate(run_id, start_after_seq=2)
    assert (
        resumed.observe(RunEvent(EventKind.TOOL_STARTED, "tool.started", 2, run_id, 5.0, {}))
        is False
    )
    assert (
        resumed.observe(RunEvent(EventKind.TOOL_COMPLETED, "tool.completed", 3, run_id, 6.0, {}))
        is True
    )


def test_client_drops_duplicate_frames_from_stub(stub: HermesStub) -> None:
    stub.resend_seq = 2
    stub.duplicate_terminal = True

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("duplicate me")
            events = await _collect(client.events(admission.run_id))
            assert [event.seq for event in events] == [0, 1, 2, 3]
            assert sum(1 for event in events if event.terminal) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_malformed_frame_raises_protocol_error(stub: HermesStub) -> None:
    stub.malformed_frame = True

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("break the stream")
            with pytest.raises(HermesProtocolError):
                async for _event in client.events(admission.run_id):
                    pass
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_permission_denied_is_conflict_without_retry(stub: HermesStub) -> None:
    stub.foreign_sessions.add("foreign-session")

    async def scenario() -> None:
        client = _client(stub)
        try:
            with pytest.raises(HermesConflictError) as raised:
                await client.submit_run("hello", session_id="foreign-session")
            assert raised.value.code == "permission_denied"

            posts = [
                request
                for request in stub.requests
                if request["method"] == "POST" and request["path"] == "/v1/runs"
            ]
            assert len(posts) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_bad_bearer_raises_auth_error(stub: HermesStub) -> None:
    async def scenario() -> None:
        client = HermesClient(stub.base_url, "not-the-key", timeout_s=2.0)
        try:
            with pytest.raises(HermesAuthError):
                await client.submit_run("hello")
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_overlong_session_key_is_rejected_before_any_request(stub: HermesStub) -> None:
    async def scenario() -> None:
        client = _client(stub)
        try:
            with pytest.raises(ValueError):
                await client.submit_run("hello", session_key="k" * 257)
            assert stub.requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())

    with pytest.raises(ValueError):
        HermesClient(stub.base_url, _API_KEY, session_key="k" * 257)
    assert stub.requests == []


def test_unknown_event_is_yielded_and_stream_continues(stub: HermesStub) -> None:
    stub.frames = [
        stub.make_frame("message.unknown", 0, note="opaque"),
        stub.make_frame(
            "run.completed",
            1,
            output="done",
            completed=True,
            partial=False,
            interrupted=False,
        ),
    ]

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("unknown event")
            events = await _collect(client.events(admission.run_id))
            assert [event.kind for event in events] == [
                EventKind.UNKNOWN,
                EventKind.RUN_COMPLETED,
            ]
            assert events[0].raw_name == "message.unknown"
            assert events[-1].terminal is True
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_swept_buffer_raises_run_gone_but_status_survives(stub: HermesStub) -> None:
    stub.events_gone = True
    stub.initial_status = "completed"
    stub.initial_output = "Linux"

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("already finished")
            with pytest.raises(HermesRunGoneError):
                async for _event in client.events(admission.run_id):
                    pass

            status = await client.get_run(admission.run_id)
            assert status.status == "completed"
            assert status.output == "Linux"
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_configured_timeout_is_enforced_on_submit_and_status() -> None:
    stub = make_stub(submit_delay_s=0.5, get_delay_s=0.5)
    stub.start()

    async def scenario() -> None:
        client = HermesClient(
            stub.base_url,
            _API_KEY,
            timeout_s=0.1,
            connect_timeout_s=0.1,
        )
        try:
            submit_started = time.monotonic()
            with pytest.raises(HermesTimeoutError):
                await client.submit_run("hi")
            assert time.monotonic() - submit_started < 0.45

            status_started = time.monotonic()
            with pytest.raises(HermesTimeoutError):
                await client.get_run(stub.run_id)
            assert time.monotonic() - status_started < 0.45
        finally:
            await client.aclose()

    try:
        asyncio.run(scenario())
    finally:
        stub.stop()


def test_from_settings_builds_client_without_request() -> None:
    settings = Settings(
        _env_file=None,
        hermes_base_url="http://127.0.0.1:1",
        hermes_api_key="settings-api-key",
        hermes_session_key="settings-session-key",
    )

    async def scenario() -> None:
        client = HermesClient.from_settings(settings, timeout_s=1.0)
        try:
            assert client is not None
        finally:
            await client.aclose()

    asyncio.run(scenario())

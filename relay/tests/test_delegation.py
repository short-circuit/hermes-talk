"""Offline tests for the delegation lifecycle service."""

from __future__ import annotations

import asyncio
import base64
import io
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from relay.brain.delegation import (
    DELEGATION_CANCELLED_PHRASES,
    DELEGATION_FAILURE_PHRASES,
    MAX_IDEMPOTENCY_KEY_CHARS,
    MAX_INSTRUCTION_CHARS,
    DelegationConfigError,
    DelegationService,
    DelegationState,
    SpokenUpdate,
    delegation_ack,
    delegation_cancelled,
    delegation_failure,
)
from relay.config import Settings
from relay.hermes import (
    EventKind,
    HermesAuthError,
    HermesClient,
    HermesConflictError,
    HermesError,
    HermesNotFoundError,
    HermesStreamError,
    HermesUnavailableError,
    RunAdmission,
    RunEvent,
    StopResult,
)
from relay.storage.inbox import InboxStore
from relay.tests import media_fixtures
from relay.tests._hermes_stub import HermesStub

_API_KEY = "stub-test-key-0123456789"
_SESSION_KEY = "voice-relay-test"


def _event(
    kind: EventKind,
    *,
    run_id: str,
    seq: int = 0,
    **payload: object,
) -> RunEvent:
    return RunEvent(
        kind=kind,
        raw_name=kind.value,
        seq=seq,
        run_id=run_id,
        timestamp=1_800_000_000.0 + seq,
        payload=payload,
    )


def _completed_events(run_id: str) -> list[RunEvent]:
    return [_event(EventKind.RUN_COMPLETED, run_id=run_id, output="Linux")]


class _BytesStream:
    """An async byte source over in-memory attachment bytes."""

    def __init__(self, data: bytes, chunk_size: int = 8192) -> None:
        self._data = data
        self._offset = 0
        self._chunk_size = chunk_size

    async def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._data):
            return b""
        remaining = len(self._data) - self._offset
        wanted = remaining if size < 0 else min(size, remaining)
        wanted = min(wanted, self._chunk_size)
        end = self._offset + wanted
        chunk = self._data[self._offset : end]
        self._offset = end
        return chunk


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, inbox_dir=tmp_path / "inbox")


def _run_requests(stub: HermesStub) -> list[dict[str, Any]]:
    return [
        request
        for request in stub.requests
        if request["method"] == "POST" and request["path"] == "/v1/runs"
    ]


def _hermes_client(stub: HermesStub) -> HermesClient:
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
    )


class FakeHermesClient:
    """Scripted HermesClient double with explicit stream release gates."""

    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.stop_calls: list[str] = []
        self.stream_starts = 0
        self.submit_error: HermesError | None = None
        self.stop_error: HermesError | None = None
        self.stream_error: Exception | None = None
        self.replayed = False
        self.auto_release = True
        self.submit_delay_s = 0.0
        self.event_factory: Callable[[str], list[RunEvent]] = _completed_events
        self._run_counter = 0
        self._scripted: dict[str, list[RunEvent]] = {}
        self._started: dict[str, asyncio.Event] = {}
        self._parked: dict[str, asyncio.Event] = {}
        self._released: dict[str, asyncio.Event] = {}
        self._finished: dict[str, asyncio.Event] = {}

    async def submit_run(
        self,
        input: str | Sequence[Mapping[str, object]],
        *,
        session_id: str | None = None,
        session_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunAdmission:
        self.submissions.append(
            {
                "input": input,
                "session_id": session_id,
                "session_key": session_key,
                "idempotency_key": idempotency_key,
            }
        )
        if self.submit_delay_s:
            await asyncio.sleep(self.submit_delay_s)
        if self.submit_error is not None:
            raise self.submit_error
        self._run_counter += 1
        run_id = f"run-fake-{self._run_counter:04d}"
        self._scripted[run_id] = self.event_factory(run_id)
        return RunAdmission(
            run_id=run_id,
            status="started",
            replayed=self.replayed,
            session_key=session_key,
        )

    async def events(
        self,
        run_id: str,
        *,
        last_seq: int | None = None,
        reconnect: bool = True,
    ) -> AsyncIterator[RunEvent]:
        self.stream_starts += 1
        self._started.setdefault(run_id, asyncio.Event()).set()
        try:
            if not self.auto_release:
                self._parked.setdefault(run_id, asyncio.Event()).set()
                await self._released.setdefault(run_id, asyncio.Event()).wait()
            for event in self._scripted.get(run_id, []):
                yield event
            if self.stream_error is not None:
                raise self.stream_error
        finally:
            self._finished.setdefault(run_id, asyncio.Event()).set()

    async def stop(self, run_id: str) -> StopResult:
        self.stop_calls.append(run_id)
        if self.stop_error is not None:
            raise self.stop_error
        return StopResult(
            run_id=run_id,
            status="stopping",
            admission_id=None,
            elapsed_s=0.0,
        )

    def release(self, run_id: str) -> None:
        self._released.setdefault(run_id, asyncio.Event()).set()

    async def wait_started(self, run_id: str) -> None:
        await self._started.setdefault(run_id, asyncio.Event()).wait()

    async def wait_parked(self, run_id: str) -> None:
        await self._parked.setdefault(run_id, asyncio.Event()).wait()

    async def wait_finished(self, run_id: str) -> None:
        await self._finished.setdefault(run_id, asyncio.Event()).wait()


class RecordingSink:
    """Collect spoken updates, optionally raising like a broken voice sink."""

    def __init__(self, *, fail: bool = False) -> None:
        self.updates: list[SpokenUpdate] = []
        self.attempts = 0
        self.fail = fail

    async def __call__(self, update: SpokenUpdate) -> None:
        self.attempts += 1
        if self.fail:
            raise RuntimeError("sink unavailable")
        self.updates.append(update)


@pytest.fixture
def stub() -> Iterator[HermesStub]:
    server = HermesStub()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_delegate_returns_before_stream_terminal() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.auto_release = False
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="**Linux**")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            started = time.perf_counter()
            handle = await service.delegate("uname -s")
            elapsed_ms = (time.perf_counter() - started) * 1000.0

            assert elapsed_ms < 100.0
            assert handle.run_id is not None
            assert handle.state is DelegationState.RUNNING
            assert handle.spoken_ack == delegation_ack()
            assert sink.updates == []

            await client.wait_parked(handle.run_id)
            assert sink.updates == []

            client.release(handle.run_id)
            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)
            assert snapshot.state is DelegationState.COMPLETED
            assert snapshot.deliveries == 1
            assert len(sink.updates) == 1
            update = sink.updates[0]
            assert update.kind == "answer"
            assert update.text == "Linux"
            assert update.final is True
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_final_answer_delivered_exactly_once() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="**Linux**")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("uname -s", idempotency_key="answer-once")
            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)

            assert snapshot.state is DelegationState.COMPLETED
            assert snapshot.deliveries == 1
            assert snapshot.delivery_error is None
            assert len(sink.updates) == 1
            update = sink.updates[0]
            assert update.delegation_id == handle.delegation_id
            assert update.run_id == handle.run_id
            assert update.kind == "answer"
            assert update.text == "Linux"
            assert update.final is True

            await asyncio.sleep(0.01)
            assert len(sink.updates) == 1
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_empty_output_falls_back_to_done() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("quiet task", idempotency_key="empty-output")
            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)

            assert snapshot.state is DelegationState.COMPLETED
            assert snapshot.deliveries == 1
            assert len(sink.updates) == 1
            assert sink.updates[0].kind == "answer"
            assert sink.updates[0].text == "Done."
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_run_failed_delivers_one_spoken_failure() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_FAILED, run_id=run_id, error="boom")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("fail task", idempotency_key="failed-run")
            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)

            assert snapshot.state is DelegationState.FAILED
            assert snapshot.failure == "boom"
            assert snapshot.deliveries == 1
            assert len(sink.updates) == 1
            update = sink.updates[0]
            assert update.kind == "failure"
            assert update.text == delegation_failure()
            assert update.final is True
            assert delegation_failure() == DELEGATION_FAILURE_PHRASES[0]
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_failed_event_without_reason_uses_run_failed() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [_event(EventKind.RUN_FAILED, run_id=run_id)]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("fail silently", idempotency_key="failed-nocode")
            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)

            assert snapshot.state is DelegationState.FAILED
            assert snapshot.failure == "run.failed"
            assert snapshot.deliveries == 1
            assert len(sink.updates) == 1
            assert sink.updates[0].text == delegation_failure()
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_failure_redacts_home_path_and_token() -> None:
    async def scenario() -> None:
        home_path = "/" + "home/alice/secret.txt"
        token = "ghp_" + "a" * 36
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_FAILED, run_id=run_id, error=f"{token} {home_path}")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("redact task", idempotency_key="redact-key")
            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)

            assert snapshot.failure is not None
            assert token not in snapshot.failure
            assert "home/" not in snapshot.failure
            assert "[redacted]" in snapshot.failure
            assert "~" in snapshot.failure
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_answer_redacts_home_path() -> None:
    async def scenario() -> None:
        home_path = "/" + "home/alice/report.txt"
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(
                EventKind.RUN_COMPLETED,
                run_id=run_id,
                output=f"Saved to {home_path}",
            )
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("save task", idempotency_key="answer-redact")
            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)

            assert snapshot.state is DelegationState.COMPLETED
            assert len(sink.updates) == 1
            assert sink.updates[0].text == "Saved to ~/report.txt"
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_stream_error_delivers_one_spoken_failure() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.MESSAGE_INTERIM, run_id=run_id, delta="partial")
        ]
        client.stream_error = HermesStreamError("stream ended without a terminal event")
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("stream task", idempotency_key="stream-error")
            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)

            assert snapshot.state is DelegationState.FAILED
            assert snapshot.failure == "HermesStreamError"
            assert snapshot.deliveries == 1
            assert len(sink.updates) == 1
            assert sink.updates[0].kind == "failure"
            assert sink.updates[0].text == delegation_failure()
        finally:
            await service.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error",
    [
        HermesUnavailableError("gateway down", code="gateway_unavailable"),
        HermesAuthError("bad token", code="gateway_auth_failed"),
        HermesConflictError("conflict", code="permission_denied"),
    ],
)
def test_submit_failure_returns_failed_handle(error: HermesError) -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.submit_error = error
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            started = time.perf_counter()
            handle = await service.delegate("do it", idempotency_key="submit-failure")
            elapsed_ms = (time.perf_counter() - started) * 1000.0

            assert elapsed_ms < 100.0
            assert handle.state is DelegationState.FAILED
            assert handle.run_id is None
            assert handle.failure == error.code
            assert handle.deduplicated is False

            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)
            assert snapshot.state is DelegationState.FAILED
            assert snapshot.terminal is True
            assert snapshot.failure == error.code
            assert snapshot.deliveries == 1
            assert len(sink.updates) == 1
            update = sink.updates[0]
            assert update.kind == "failure"
            assert update.text == delegation_failure()
            assert update.final is True
            assert len(client.submissions) == 1
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_same_key_sequential_deduplicates() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="first answer")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            first = await service.delegate("first", idempotency_key="dup-key")
            snapshot = await service.wait(first.delegation_id, timeout_s=2.0)
            assert snapshot.deliveries == 1

            second = await service.delegate("second", idempotency_key="dup-key")
            assert second.deduplicated is True
            assert second.delegation_id == first.delegation_id
            assert second.run_id == first.run_id
            assert second.state is DelegationState.COMPLETED
            assert len(client.submissions) == 1
            assert client.stream_starts == 1
            assert len(sink.updates) == 1

            snapshot = service.snapshot(first.delegation_id)
            assert snapshot.deduplicated is True
            assert snapshot.deliveries == 1
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_same_key_concurrent_submits_once() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.submit_delay_s = 0.02
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="shared answer")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            first, second = await asyncio.gather(
                service.delegate("one", idempotency_key="concurrent-key"),
                service.delegate("two", idempotency_key="concurrent-key"),
            )

            assert len(client.submissions) == 1
            assert first.delegation_id == second.delegation_id
            assert first.run_id == second.run_id
            assert {first.deduplicated, second.deduplicated} == {False, True}

            snapshot = await service.wait(first.delegation_id, timeout_s=2.0)
            assert snapshot.state is DelegationState.COMPLETED
            assert snapshot.deliveries == 1
            assert client.stream_starts == 1
            assert len(sink.updates) == 1
            assert sink.updates[0].text == "shared answer"
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_replayed_admission_is_adopted() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.replayed = True
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="replayed answer")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("replay me", idempotency_key="replay-key")
            assert handle.replayed is True
            assert handle.run_id is not None

            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)
            assert snapshot.replayed is True
            assert snapshot.run_id == handle.run_id
            assert snapshot.state is DelegationState.COMPLETED
            assert snapshot.deliveries == 1
            assert len(client.submissions) == 1
            assert len(sink.updates) == 1
            assert sink.updates[0].text == "replayed answer"
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_different_keys_submit_and_deliver_separately() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output=f"answer {run_id}")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            first = await service.delegate("one", idempotency_key="key-a")
            second = await service.delegate("two", idempotency_key="key-b")

            assert first.delegation_id != second.delegation_id
            assert first.run_id != second.run_id

            first_snapshot = await service.wait(first.delegation_id, timeout_s=2.0)
            second_snapshot = await service.wait(second.delegation_id, timeout_s=2.0)
            assert first_snapshot.deliveries == 1
            assert second_snapshot.deliveries == 1
            assert len(client.submissions) == 2
            assert len(sink.updates) == 2
            assert {update.text for update in sink.updates} == {
                f"answer {first.run_id}",
                f"answer {second.run_id}",
            }
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_retry_after_completion_does_not_resubmit_or_redeliver() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="done already")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            first = await service.delegate("finish", idempotency_key="retry-key")
            await service.wait(first.delegation_id, timeout_s=2.0)
            assert len(sink.updates) == 1

            retry = await service.delegate("finish again", idempotency_key="retry-key")
            assert retry.deduplicated is True
            assert retry.delegation_id == first.delegation_id
            assert retry.state is DelegationState.COMPLETED

            await asyncio.sleep(0.01)
            assert len(client.submissions) == 1
            assert client.stream_starts == 1
            assert len(sink.updates) == 1
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_tracking_lookups_and_unknown_id() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="tracked")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("track me", idempotency_key="track-key")
            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)

            assert snapshot.delegation_id == handle.delegation_id
            assert snapshot.idempotency_key == "track-key"
            assert snapshot.run_id == handle.run_id
            assert snapshot.state is DelegationState.COMPLETED
            assert snapshot.terminal is True
            assert snapshot.created_at > 0.0
            assert snapshot.enqueue_ms >= 0.0
            assert snapshot.deduplicated is False
            assert snapshot.replayed is False
            assert snapshot.failure is None
            assert snapshot.delivery_error is None
            assert snapshot.deliveries == 1

            assert service.by_run_id(handle.run_id) == snapshot
            assert service.by_idempotency_key("track-key") == snapshot
            assert service.by_run_id("run-missing") is None
            assert service.by_idempotency_key("missing") is None

            with pytest.raises(KeyError):
                service.snapshot("dlg-missing")
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_stop_delivers_cancelled_once_and_racing_terminal_adds_nothing() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.auto_release = False
        client.event_factory = lambda run_id: [_event(EventKind.RUN_CANCELLED, run_id=run_id)]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("stop me", idempotency_key="stop-key")
            assert handle.run_id is not None
            await client.wait_started(handle.run_id)
            await client.wait_parked(handle.run_id)

            stopped = await service.stop(handle.delegation_id)
            assert stopped.state is DelegationState.CANCELLED
            assert stopped.run_id == handle.run_id
            assert client.stop_calls == [handle.run_id]
            assert len(sink.updates) == 1
            update = sink.updates[0]
            assert update.kind == "cancelled"
            assert update.text == delegation_cancelled()
            assert update.final is True
            assert delegation_cancelled() == DELEGATION_CANCELLED_PHRASES[0]
            assert service.snapshot(handle.delegation_id).deliveries == 1

            client.release(handle.run_id)
            await client.wait_finished(handle.run_id)
            await asyncio.sleep(0.01)

            assert len(sink.updates) == 1
            snapshot = service.snapshot(handle.delegation_id)
            assert snapshot.state is DelegationState.CANCELLED
            assert snapshot.deliveries == 1
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_stop_on_terminal_record_is_noop() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="finished")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("finish", idempotency_key="terminal-key")
            await service.wait(handle.delegation_id, timeout_s=2.0)
            assert len(sink.updates) == 1

            stopped = await service.stop(handle.delegation_id)
            assert stopped.state is DelegationState.COMPLETED
            assert client.stop_calls == []
            assert len(sink.updates) == 1
            assert service.snapshot(handle.delegation_id).deliveries == 1
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_stop_not_found_finishes_cancelled() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.auto_release = False
        client.event_factory = lambda run_id: [_event(EventKind.RUN_CANCELLED, run_id=run_id)]
        client.stop_error = HermesNotFoundError("run gone", code="run_not_found")
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("gone task", idempotency_key="gone-key")
            assert handle.run_id is not None
            await client.wait_parked(handle.run_id)

            stopped = await service.stop(handle.delegation_id)
            assert stopped.state is DelegationState.CANCELLED
            assert len(sink.updates) == 1
            assert sink.updates[0].text == delegation_cancelled()

            client.release(handle.run_id)
            await client.wait_finished(handle.run_id)
            assert len(sink.updates) == 1
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_stop_failure_leaves_running_for_watcher() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.auto_release = False
        client.event_factory = lambda run_id: [_event(EventKind.RUN_CANCELLED, run_id=run_id)]
        client.stop_error = HermesUnavailableError("stop unavailable", code="unavailable")
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("still running", idempotency_key="stop-fail")
            assert handle.run_id is not None
            await client.wait_parked(handle.run_id)

            stopped = await service.stop(handle.delegation_id)
            assert stopped.state is DelegationState.RUNNING
            assert client.stop_calls == [handle.run_id]
            assert sink.updates == []

            client.release(handle.run_id)
            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)
            assert snapshot.state is DelegationState.CANCELLED
            assert snapshot.deliveries == 1
            assert len(sink.updates) == 1
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_empty_whitespace_instruction_and_invalid_keys_raise() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            for instruction in ("", "   \n\t ", 123):
                with pytest.raises(DelegationConfigError):
                    await service.delegate(instruction)  # type: ignore[arg-type]

            invalid_keys: list[object] = [
                "",
                "a" * (MAX_IDEMPOTENCY_KEY_CHARS + 1),
                "bad\nkey",
                "é",
                123,
            ]
            for key in invalid_keys:
                with pytest.raises(DelegationConfigError):
                    await service.delegate(  # type: ignore[arg-type]
                        "hello",
                        idempotency_key=key,
                    )

            assert client.submissions == []
            assert sink.updates == []
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_long_instruction_is_truncated_before_submit() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="done")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            instruction = "x" * (MAX_INSTRUCTION_CHARS + 1000)
            handle = await service.delegate(instruction, idempotency_key="long-key")
            await service.wait(handle.delegation_id, timeout_s=2.0)

            assert len(client.submissions) == 1
            submitted = client.submissions[0]["input"]
            assert isinstance(submitted, str)
            assert len(submitted) <= MAX_INSTRUCTION_CHARS + 100
            assert submitted.startswith("x" * MAX_INSTRUCTION_CHARS)
            assert "TRUNCATED" in submitted
            assert len(submitted) < len(instruction)
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_aclose_cancels_watcher_without_delivery() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.auto_release = False
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="never heard")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("close me", idempotency_key="close-key")
            assert handle.run_id is not None
            await client.wait_parked(handle.run_id)
            assert sink.updates == []

            await service.aclose()
            assert sink.updates == []

            client.release(handle.run_id)
            await asyncio.sleep(0.01)

            assert sink.updates == []
            snapshot = service.snapshot(handle.delegation_id)
            assert snapshot.state is DelegationState.RUNNING
            assert snapshot.deliveries == 0
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_registry_prunes_expired_records() -> None:
    async def scenario() -> None:
        now = [100.0]
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="ttl")
        ]
        sink = RecordingSink()
        service = DelegationService(
            client,
            sink,
            clock=lambda: now[0],
            max_records=10,
            record_ttl_s=10.0,
        )
        try:
            first = await service.delegate("first", idempotency_key="ttl-first")
            await service.wait(first.delegation_id, timeout_s=2.0)
            assert service.by_idempotency_key("ttl-first") is not None

            now[0] += 11.0
            second = await service.delegate("second", idempotency_key="ttl-second")
            await service.wait(second.delegation_id, timeout_s=2.0)

            assert service.by_idempotency_key("ttl-first") is None
            assert service.by_run_id(first.run_id) is None
            with pytest.raises(KeyError):
                service.snapshot(first.delegation_id)
            assert service.by_idempotency_key("ttl-second") is not None
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_registry_evicts_oldest_terminal_records() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="evict")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink, max_records=2)
        try:
            handles = []
            for index in range(3):
                handle = await service.delegate(
                    f"task {index}",
                    idempotency_key=f"evict-{index}",
                )
                await service.wait(handle.delegation_id, timeout_s=2.0)
                handles.append(handle)

            with pytest.raises(KeyError):
                service.snapshot(handles[0].delegation_id)
            assert service.by_idempotency_key("evict-0") is None
            assert service.snapshot(handles[1].delegation_id).state is DelegationState.COMPLETED
            assert service.snapshot(handles[2].delegation_id).state is DelegationState.COMPLETED
            assert len(sink.updates) == 3
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_registry_never_evicts_running_records() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.auto_release = False
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="running")
        ]
        sink = RecordingSink()
        service = DelegationService(client, sink, max_records=2)
        try:
            handles = []
            for index in range(3):
                handle = await service.delegate(
                    f"task {index}",
                    idempotency_key=f"run-{index}",
                )
                assert handle.run_id is not None
                await client.wait_parked(handle.run_id)
                handles.append(handle)

            for handle in handles:
                snapshot = service.snapshot(handle.delegation_id)
                assert snapshot.state is DelegationState.RUNNING

            first = handles[0]
            assert first.run_id is not None
            client.release(first.run_id)
            await service.wait(first.delegation_id, timeout_s=2.0)

            fourth = await service.delegate("fourth", idempotency_key="run-3")
            assert fourth.run_id is not None
            await client.wait_parked(fourth.run_id)

            with pytest.raises(KeyError):
                service.snapshot(first.delegation_id)
            for handle in handles[1:]:
                snapshot = service.snapshot(handle.delegation_id)
                assert snapshot.state is DelegationState.RUNNING
            assert service.snapshot(fourth.delegation_id).state is DelegationState.RUNNING
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_sink_exception_is_recorded_without_retry() -> None:
    async def scenario() -> None:
        client = FakeHermesClient()
        client.event_factory = lambda run_id: [
            _event(EventKind.RUN_COMPLETED, run_id=run_id, output="will not be heard")
        ]
        sink = RecordingSink(fail=True)
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate("broken sink", idempotency_key="sink-key")
            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)

            assert snapshot.state is DelegationState.COMPLETED
            assert snapshot.deliveries == 1
            assert snapshot.delivery_error == "RuntimeError"
            assert sink.attempts == 1
            assert sink.updates == []

            await asyncio.sleep(0.01)
            assert sink.attempts == 1
            assert service.snapshot(handle.delegation_id).deliveries == 1
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_end_to_end_with_real_client_and_stub(stub: HermesStub) -> None:
    async def scenario() -> None:
        client = HermesClient(
            stub.base_url,
            _API_KEY,
            session_key=_SESSION_KEY,
            timeout_s=2.0,
            connect_timeout_s=1.0,
            stop_timeout_s=2.0,
            stream_timeout_s=2.0,
            reconnect_backoff_s=0.01,
            max_reconnect_backoff_s=0.05,
        )
        sink = RecordingSink()
        service = DelegationService(
            client,
            sink,
            session_id="session-e2e",
            session_key=_SESSION_KEY,
        )
        try:
            handle = await service.delegate(
                "Use the terminal tool to run uname -s",
                idempotency_key="e2e-key",
            )
            assert handle.run_id == stub.run_id
            assert handle.spoken_ack == delegation_ack()
            assert handle.enqueue_ms < 100.0

            snapshot = await service.wait(handle.delegation_id, timeout_s=5.0)
            assert snapshot.state is DelegationState.COMPLETED
            assert snapshot.run_id == stub.run_id
            assert snapshot.deliveries == 1
            assert len(sink.updates) == 1
            update = sink.updates[0]
            assert update.kind == "answer"
            assert update.text == "Linux"
            assert update.final is True

            submits = [
                request
                for request in stub.requests
                if request["method"] == "POST" and request["path"] == "/v1/runs"
            ]
            assert len(submits) == 1
            assert submits[0]["json"]["session_id"] == "session-e2e"
            assert submits[0]["headers"]["idempotency-key"] == "e2e-key"
            assert submits[0]["headers"]["x-hermes-session-key"] == _SESSION_KEY
            assert isinstance(submits[0]["json"]["input"], str)
            assert submits[0]["json"]["input"] == "Use the terminal tool to run uname -s"
        finally:
            await service.aclose()
            await client.aclose()

    asyncio.run(scenario())


def test_delegate_with_image_attachment_submits_content_array(
    stub: HermesStub,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = InboxStore(_settings(tmp_path))
        attachment = await store.ingest(
            filename="photo.png",
            declared_content_type="image/png",
            stream=_BytesStream(_png_bytes()),
        )
        client = _hermes_client(stub)
        sink = RecordingSink()
        service = DelegationService(client, sink, session_id="session-attach")
        try:
            handle = await service.delegate(
                "Look at this photo",
                attachments=[attachment],
                store=store,
                idempotency_key="image-attachment",
            )
            assert handle.run_id == stub.run_id

            snapshot = await service.wait(handle.delegation_id, timeout_s=5.0)
            assert snapshot.state is DelegationState.COMPLETED
            assert snapshot.deliveries == 1
            assert len(sink.updates) == 1
            assert sink.updates[0].kind == "answer"

            submits = _run_requests(stub)
            assert len(submits) == 1
            parts = submits[0]["json"]["input"]
            assert isinstance(parts, list)
            assert len(parts) == 3
            assert parts[0] == {"type": "text", "text": "Look at this photo"}

            attachment_text = parts[1]
            assert attachment_text["type"] == "text"
            assert f"path={attachment.path}" in attachment_text["text"]
            assert f"sha256={attachment.sha256}" in attachment_text["text"]

            expected_url = "data:image/png;base64," + base64.b64encode(
                attachment.path.read_bytes()
            ).decode("ascii")
            assert parts[2] == {
                "type": "image_url",
                "image_url": {"url": expected_url},
            }
            assert sum(1 for part in parts if part["type"] == "image_url") == 1
        finally:
            await service.aclose()
            await client.aclose()

    asyncio.run(scenario())


def test_delegate_with_pdf_attachment_submits_text_part_only(
    stub: HermesStub,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = InboxStore(_settings(tmp_path))
        attachment = await store.ingest(
            filename="report.pdf",
            declared_content_type="application/pdf",
            stream=_BytesStream(media_fixtures.build_pdf()),
        )
        client = _hermes_client(stub)
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate(
                "Read this report",
                attachments=[attachment],
                store=store,
                idempotency_key="pdf-attachment",
            )
            assert handle.run_id == stub.run_id

            snapshot = await service.wait(handle.delegation_id, timeout_s=5.0)
            assert snapshot.state is DelegationState.COMPLETED
            assert snapshot.deliveries == 1
            assert len(sink.updates) == 1
            assert sink.updates[0].kind == "answer"

            submits = _run_requests(stub)
            assert len(submits) == 1
            parts = submits[0]["json"]["input"]
            assert isinstance(parts, list)
            assert len(parts) == 2
            assert parts[0] == {"type": "text", "text": "Read this report"}

            attachment_text = parts[1]
            assert attachment_text["type"] == "text"
            assert f"path={attachment.path}" in attachment_text["text"]
            assert f"sha256={attachment.sha256}" in attachment_text["text"]
            assert all(part["type"] != "image_url" for part in parts)
        finally:
            await service.aclose()
            await client.aclose()

    asyncio.run(scenario())


def test_delegate_with_tampered_attachment_fails_without_submit(
    stub: HermesStub,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = InboxStore(_settings(tmp_path))
        attachment = await store.ingest(
            filename="photo.png",
            declared_content_type="image/png",
            stream=_BytesStream(_png_bytes()),
        )
        attachment.path.write_bytes(attachment.path.read_bytes() + b"\x00")

        client = _hermes_client(stub)
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            handle = await service.delegate(
                "Look at this photo",
                attachments=[attachment],
                store=store,
                idempotency_key="tampered-attachment",
            )
            assert handle.state is DelegationState.FAILED
            assert handle.run_id is None
            assert handle.failure == "attachment_changed"

            snapshot = await service.wait(handle.delegation_id, timeout_s=2.0)
            assert snapshot.state is DelegationState.FAILED
            assert snapshot.failure == "attachment_changed"
            assert snapshot.deliveries == 1
            assert len(sink.updates) == 1
            assert sink.updates[0].kind == "failure"
            assert sink.updates[0].text == delegation_failure()
            assert _run_requests(stub) == []
        finally:
            await service.aclose()
            await client.aclose()

    asyncio.run(scenario())


def test_attachments_without_store_raise_config_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = InboxStore(_settings(tmp_path))
        attachment = await store.ingest(
            filename="photo.png",
            declared_content_type="image/png",
            stream=_BytesStream(_png_bytes()),
        )
        client = FakeHermesClient()
        sink = RecordingSink()
        service = DelegationService(client, sink)
        try:
            with pytest.raises(DelegationConfigError):
                await service.delegate(
                    "Look at this photo",
                    attachments=[attachment],
                )
            assert client.submissions == []
            assert sink.updates == []
        finally:
            await service.aclose()

    asyncio.run(scenario())

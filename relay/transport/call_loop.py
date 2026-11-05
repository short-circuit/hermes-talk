"""Transport-agnostic RTVI call loop.

The loop composes the existing turn controller, speech adapters, voice-brain
router, delegation lifecycle, narration, and spoken approval gate into one
call.  It deliberately does not import ``aiortc``: microphone audio arrives as
:class:`~relay.speech.audio.PcmChunk` and synthesized audio leaves as
:class:`~relay.speech.tts.AudioChunk` plus RTVI message dictionaries through
injected sinks.  That keeps the offline unit tests free of any audio transport.

Everything here runs on one event loop and is not thread-safe.  The only
network calls are the explicitly injected Hermes client methods.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any

from relay.brain.approvals import (
    ApprovalGate,
    ApprovalGateError,
    ApprovalRequest,
    Decision,
    Provenance,
    StaleControlIdError,
    Utterance,
)
from relay.brain.delegation import DelegationService, SpokenUpdate
from relay.brain.narration import ToolNarrator
from relay.hermes.client import HermesConflictError, HermesError
from relay.hermes.events import EventKind, RunEvent
from relay.speech.audio import PcmChunk
from relay.speech.tts import AudioChunk
from relay.transport.turns import (
    BargeInResult,
    RunStopResult,
    TurnController,
    TurnState,
    TurnStateError,
)

_LOGGER = logging.getLogger(__name__)

CALL_CAP_WARNING_TEXT = "You have {seconds} seconds left."
CALL_CAP_GOODBYE = "Time's up. Goodbye."
ROUTER_FAILURE_TEXT = "Sorry, I couldn't reach the voice brain."
DEFAULT_CALL_CAP_WARNING_SECONDS = 30.0
_SECONDS_PER_MINUTE = 60.0
_MILLISECONDS_PER_SECOND = 1000.0
_MAX_TEXT_CHARS = 4000
_DELEGATION_STATE = {
    "answer": "completed",
    "failure": "failed",
    "cancelled": "cancelled",
}


def _default_scheduler(delay_s: float, callback: Callable[[], None]) -> Any:
    """Schedule a callback on the running loop (injectable for tests)."""
    return asyncio.get_running_loop().call_later(delay_s, callback)


def _message(message_type: str, **data: Any) -> dict[str, Any]:
    """Build one RTVI envelope."""
    return {"label": "rtvi", "type": message_type, "data": data}


def _bounded_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()[:_MAX_TEXT_CHARS]


class _CallPlayback:
    """Playback port that clears the active TTS and the outbound queue."""

    def __init__(self, loop: CallLoop) -> None:
        self._loop = loop

    def flush(self) -> None:
        self._loop._flush_playback()


class _HermesStopper:
    """Run-stopper port backed by :meth:`HermesClient.stop`."""

    def __init__(self, client: Any, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._clock = clock

    async def stop(self, run_id: str) -> RunStopResult:
        started = self._clock()
        try:
            result = await self._client.stop(run_id)
        except HermesConflictError as exc:
            code = exc.code or "conflict"
            ok = "run_not_active" in code
            return self._result(run_id, started, ok=ok, status=None, error=code)
        except HermesError as exc:
            return self._result(
                run_id,
                started,
                ok=False,
                status=None,
                error=exc.code or type(exc).__name__,
            )
        except Exception as exc:
            return self._result(
                run_id,
                started,
                ok=False,
                status=None,
                error=type(exc).__name__,
            )
        status = getattr(result, "status", None)
        return self._result(
            run_id,
            started,
            ok=True,
            status=status if isinstance(status, str) else None,
            error=None,
        )

    def _result(
        self,
        run_id: str,
        started: float,
        *,
        ok: bool,
        status: str | None,
        error: str | None,
    ) -> RunStopResult:
        latency_ms = max((self._clock() - started) * _MILLISECONDS_PER_SECOND, 0.0)
        return RunStopResult(
            ok=ok,
            run_id=run_id,
            status=status,
            latency_ms=latency_ms,
            error=error,
        )


class _TeeHermesClient:
    """Forward Hermes calls while observing run events for the call loop."""

    def __init__(self, client: Any, loop: CallLoop) -> None:
        self._client = client
        self._loop = loop

    async def submit_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self._client.submit_run(*args, **kwargs)

    async def stop(self, run_id: str) -> Any:
        return await self._client.stop(run_id)

    async def approval(self, run_id: str, **kwargs: Any) -> Any:
        return await self._client.approval(run_id, **kwargs)

    async def events(self, run_id: str, **kwargs: Any) -> Any:
        async for event in self._client.events(run_id, **kwargs):
            try:
                await self._loop.handle_run_event(event)
            except Exception:
                _LOGGER.exception(
                    "call_loop.run_event_failed",
                    extra={
                        "event": "call_loop.run_event_failed",
                        "run_id": run_id,
                        "kind": event.kind.value,
                    },
                )
            yield event

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


class CallLoop:
    """One call's mic-to-speaker pipeline, independent of any transport.

    The public surface is intentionally small: :meth:`start`, :meth:`handle_mic`,
    :meth:`handle_transcript`, :meth:`handle_run_event`, :meth:`bind_run`,
    :meth:`request_close`, :meth:`close`, and :meth:`wait_idle`.  The RTVI
    session owns the peer connection and passes the injected sinks and close
    callback; tests can drive the same loop with fakes and no sockets.
    """

    def __init__(
        self,
        settings: Any,
        *,
        emit_message: Callable[[Mapping[str, Any]], Awaitable[None] | None],
        emit_audio: Callable[[AudioChunk], Awaitable[None] | None],
        tts_factory: Callable[[], Any],
        router: Any,
        hermes: Any,
        stt_adapter: Any | None = None,
        delegation_factory: (
            Callable[[Any, Callable[[SpokenUpdate], Awaitable[None]]], Any] | None
        ) = None,
        narrator: Any | None = None,
        approval_gate_factory: Callable[[], ApprovalGate] | None = None,
        outbound_clear: Callable[[], None] | None = None,
        scheduler: Callable[[float, Callable[[], None]], Any] | None = None,
        call_cap_seconds: float | None = None,
        call_cap_warning_seconds: float | None = None,
        on_close: Callable[[str], Awaitable[None] | None] | None = None,
        session_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        if not callable(emit_message):
            msg = "emit_message must be callable"
            raise ValueError(msg)
        if not callable(emit_audio):
            msg = "emit_audio must be callable"
            raise ValueError(msg)
        if not callable(tts_factory):
            msg = "tts_factory must be callable"
            raise ValueError(msg)
        if router is None or not callable(getattr(router, "decide", None)):
            msg = "router must provide decide()"
            raise ValueError(msg)
        if hermes is None:
            msg = "hermes client is required"
            raise ValueError(msg)

        self._settings = settings
        self._emit_message_fn = emit_message
        self._emit_audio_fn = emit_audio
        self._tts_factory = tts_factory
        self._router = router
        self._hermes = hermes
        self._stt_adapter = stt_adapter
        self._delegation_factory = delegation_factory
        self._narrator = narrator
        self._approval_gate_factory = approval_gate_factory
        self._outbound_clear = outbound_clear
        self._scheduler = scheduler if scheduler is not None else _default_scheduler
        self._on_close = on_close
        self._session_id = session_id
        self._session_key = session_key

        configured_cap = float(getattr(settings, "max_call_minutes", 60)) * _SECONDS_PER_MINUTE
        self._call_cap_seconds = (
            float(call_cap_seconds) if call_cap_seconds is not None else configured_cap
        )
        if call_cap_warning_seconds is not None:
            self._call_cap_warning_seconds = float(call_cap_warning_seconds)
        else:
            self._call_cap_warning_seconds = min(
                DEFAULT_CALL_CAP_WARNING_SECONDS,
                max(self._call_cap_seconds / 2.0, 1.0),
            )

        self._controller = TurnController.from_settings(
            settings,
            playback=_CallPlayback(self),
            stopper=_HermesStopper(hermes),
        )

        self._started = False
        self._closed = False
        self._stt_session: Any | None = None
        self._turn: Any | None = None
        self._response_task: asyncio.Task[None] | None = None
        self._readout_task: asyncio.Task[None] | None = None
        self._approval_task: asyncio.Task[None] | None = None
        self._warning_task: asyncio.Task[None] | None = None
        self._cap_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._speak_lock = asyncio.Lock()
        self._active_tts: Any | None = None
        self._tts_open = False
        self._run_id: str | None = None
        self._latest_tool: str | None = None
        self._approval_gate: ApprovalGate | None = None
        self._approval_run_id: str | None = None
        self._approval_deadline: float | None = None
        self._approval_timer: Any | None = None
        self._warning_timer: Any | None = None
        self._cap_timer: Any | None = None
        self._delegation: Any | None = None

    @property
    def closed(self) -> bool:
        """Return whether the loop has been closed."""
        return self._closed

    async def start(self) -> None:
        """Start the call: open one STT session and arm the call cap."""
        if self._started or self._closed:
            return
        self._started = True
        if self._stt_adapter is not None:
            self._stt_session = await self._stt_adapter.start_session()
        self._turn = self._controller.begin_user_turn()
        self._schedule_call_cap()

    async def handle_mic(self, chunk: PcmChunk) -> None:
        """Process one microphone chunk for barge-in and transcription."""
        if self._closed:
            return
        outcome = await self._controller.feed_mic(chunk)
        barge_in = outcome.barge_in
        if barge_in is not None and barge_in.stop_accepted:
            await self._handle_barge_in(barge_in)
        if self._stt_session is not None:
            events = await self._stt_session.feed(chunk)
            for event in events:
                await self.handle_transcript(event)

    async def handle_transcript(self, event: Any) -> None:
        """Handle one partial or final transcript event."""
        text = _bounded_text(getattr(event, "text", ""))
        is_final = getattr(event, "kind", "") == "final"
        await self._emit_message(_message("user-transcription", text=text, final=is_final))
        if not is_final or not text or self._closed:
            return

        gate = self._approval_gate
        if gate is not None and gate.pending is not None:
            await self._answer_approval(text)
            return

        if self._response_task is not None and not self._response_task.done():
            await self._interrupt_for_new_utterance()
        await self._start_response(text)

    async def handle_run_event(self, event: RunEvent) -> None:
        """Observe one Hermes run event for narration, approval, and clarify."""
        if self._closed:
            return
        if event.kind is EventKind.TOOL_STARTED:
            if event.tool is not None:
                self._latest_tool = event.tool
            narrator = self._narrator
            if narrator is None:
                narrator = ToolNarrator(run_id=event.run_id)
                self._narrator = narrator
            update = narrator.observe(event)
            if update is not None:
                await self._speak_text(update.text)
        elif event.kind is EventKind.APPROVAL_REQUEST:
            await self._handle_approval_request(event)
        elif event.kind is EventKind.CLARIFY_REQUEST:
            await self._handle_clarify(event)

    def bind_run(self, run_id: str) -> Any:
        """Attach a run id to the active THINKING turn."""
        self._turn = self._controller.bind_run(run_id)
        return self._turn

    async def request_close(self, reason: str = "requested") -> None:
        """Close the call, preferring the transport's close callback."""
        if self._closed:
            return
        if self._on_close is not None:
            result = self._on_close(reason)
            if inspect.isawaitable(result):
                await result
            return
        await self.close(reason)

    async def close(self, reason: str = "closed") -> None:
        """Close the loop exactly once and emit ``session-closed``."""
        if self._closed:
            return
        self._closed = True
        self._cancel_timers()
        current = asyncio.current_task()
        pending = [task for task in self._background if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        tts = self._active_tts
        if tts is not None:
            await self._close_resource(tts)
            self._active_tts = None
        await self._emit_tts_stopped()

        stt_session = self._stt_session
        if stt_session is not None:
            await self._close_resource(stt_session)
            self._stt_session = None

        delegation = self._delegation
        if delegation is not None and callable(getattr(delegation, "aclose", None)):
            await self._close_resource(delegation)
        self._controller.close()
        self._cancel_approval_timer()
        self._approval_gate = None
        await self._emit_message(_message("session-closed", reason=reason))

    async def wait_idle(self) -> None:
        """Wait until every background call task has finished."""
        while True:
            pending = [task for task in self._background if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    # --- response pipeline -------------------------------------------------

    async def _start_response(self, text: str) -> None:
        turn = self._turn
        if turn is None or turn.state is not TurnState.LISTENING:
            self._turn = self._controller.begin_user_turn()
        self._turn = self._controller.commit_utterance(text)
        self._response_task = self._spawn(self._respond(text), "call_loop.response")

    async def _respond(self, utterance: str) -> None:
        try:
            decision = await self._router.decide(utterance)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "call_loop.router_failed",
                extra={"event": "call_loop.router_failed", "error": type(exc).__name__},
            )
            await self._speak_text(ROUTER_FAILURE_TEXT)
            await self._finish_turn()
            return

        try:
            await self._dispatch_decision(decision, utterance)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.exception(
                "call_loop.response_failed",
                extra={"event": "call_loop.response_failed", "error": type(exc).__name__},
            )
            await self._speak_text(ROUTER_FAILURE_TEXT)
            await self._finish_turn()

    async def _dispatch_decision(self, decision: Any, utterance: str) -> None:
        tool = getattr(decision, "tool", "")
        spoken = _bounded_text(getattr(decision, "spoken_text", ""))
        arguments = getattr(decision, "arguments", {}) or {}

        if tool == "answer_direct":
            self._turn = self._controller.begin_reply()
            await self._emit_message(_message("bot-llm-text", text=spoken))
            self._controller.note_spoken(spoken)
            await self._speak_text(spoken)
            await self._finish_turn()
            return

        if tool == "delegate_to_hermes":
            self._turn = self._controller.begin_reply()
            instruction = _bounded_text(arguments.get("instruction")) or utterance
            delegation = self._ensure_delegation()
            handle = await delegation.delegate(
                instruction,
                session_id=self._session_id,
                session_key=self._session_key,
            )
            run_id = getattr(handle, "run_id", None)
            if isinstance(run_id, str) and run_id:
                self.bind_run(run_id)
                self._run_id = run_id
                if self._narrator is None:
                    self._narrator = ToolNarrator(run_id=run_id)
            state = getattr(handle, "state", "running")
            state_value = getattr(state, "value", str(state))
            await self._emit_message(
                _message(
                    "delegation-status",
                    delegation_id=getattr(handle, "delegation_id", ""),
                    state=state_value,
                    run_id=run_id,
                )
            )
            ack = _bounded_text(getattr(handle, "spoken_ack", ""))
            await self._speak_text(ack)
            return

        self._turn = self._controller.begin_reply()
        self._controller.note_spoken(spoken)
        duration_s = await self._speak_text(spoken)
        await self._finish_turn()
        if tool == "end_call":
            await asyncio.sleep(min(duration_s + 0.1, 5.0))
            await self.request_close("end_call")

    async def _deliver_update(self, update: SpokenUpdate) -> None:
        state = _DELEGATION_STATE.get(update.kind, update.kind)
        await self._emit_message(
            _message(
                "delegation-status",
                delegation_id=update.delegation_id,
                state=state,
                run_id=update.run_id,
            )
        )
        await self._speak_text(update.text)
        turn = self._turn
        if turn is not None and (
            update.run_id is None or turn.run_id is None or turn.run_id == update.run_id
        ):
            self._run_id = None
            await self._finish_turn()

    def _ensure_delegation(self) -> Any:
        if self._delegation is not None:
            return self._delegation
        tee = _TeeHermesClient(self._hermes, self)
        if self._delegation_factory is None:
            self._delegation = DelegationService(
                tee,
                self._deliver_update,
                session_id=self._session_id,
                session_key=self._session_key,
            )
        else:
            self._delegation = self._delegation_factory(tee, self._deliver_update)
        return self._delegation

    async def _finish_turn(self) -> None:
        turn = self._turn
        if turn is None:
            return
        try:
            if turn.state in (TurnState.THINKING, TurnState.SPEAKING, TurnState.INTERRUPTED):
                self._controller.end_reply()
        except TurnStateError:
            pass
        if not self._closed:
            self._turn = self._controller.begin_user_turn()

    async def _interrupt_for_new_utterance(self) -> None:
        turn = self._turn
        if turn is not None and turn.state in (TurnState.THINKING, TurnState.SPEAKING):
            try:
                interruption = await self._controller.stop_reply(reason="new_utterance")
            except TurnStateError:
                interruption = None
            if interruption is not None:
                stop_ok = True
                if interruption.run_id is not None:
                    stop_ok = bool(interruption.run_stop is not None and interruption.run_stop.ok)
                await self._emit_message(
                    _message(
                        "barge-in",
                        turn_id=interruption.turn_id,
                        run_id=interruption.run_id,
                        stop_ok=stop_ok,
                    )
                )
        self._tts_open = False
        await self._cancel_response()
        self._flush_playback()
        await self._emit_message(_message("bot-tts-stopped"))
        await self._finish_turn()

    async def _handle_barge_in(self, barge_in: BargeInResult) -> None:
        turn_id = None
        snapshot = self._controller.snapshot()
        value = snapshot.get("turn_id")
        if isinstance(value, str):
            turn_id = value
        run_id = self._run_id
        stop_ok = True
        if run_id is not None:
            stop_ok = bool(barge_in.run_stop is not None and barge_in.run_stop.ok)
        self._tts_open = False
        await self._cancel_response()
        self._flush_playback()
        await self._emit_message(_message("bot-tts-stopped"))
        await self._emit_message(
            _message(
                "barge-in",
                turn_id=turn_id,
                run_id=run_id,
                stop_ok=stop_ok,
            )
        )
        self._run_id = None
        await self._finish_turn()

    async def _cancel_response(self) -> None:
        task = self._response_task
        self._response_task = None
        if task is None or task is asyncio.current_task() or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    # --- approval gate -----------------------------------------------------

    async def _handle_approval_request(self, event: RunEvent) -> None:
        prompt_id = event.prompt_id
        generation = event.execution_generation
        if prompt_id is None or generation is None:
            return
        payload = event.payload
        command = payload.get("command")
        command_text = command if isinstance(command, str) else None
        description = payload.get("description")
        description_text = description if isinstance(description, str) else None
        tool = event.tool or self._latest_tool or "tool"
        action = description_text or f"{tool} wants to run"
        phrase = str(getattr(self._settings, "approval_confirmation_phrase", "yes run it"))
        timeout_s = float(getattr(self._settings, "approval_timeout_s", 20.0))
        request = ApprovalRequest(
            prompt_id=prompt_id,
            execution_generation=generation,
            tool=tool,
            action_summary=action,
            required_phrase=phrase,
            timeout_s=timeout_s,
            command=command_text,
            description=description_text,
            run_id=event.run_id,
        )

        gate = (
            self._approval_gate_factory()
            if self._approval_gate_factory is not None
            else ApprovalGate.from_settings(self._settings)
        )
        try:
            result = gate.request(request)
        except ApprovalGateError:
            _LOGGER.warning(
                "call_loop.approval_already_pending",
                extra={"event": "call_loop.approval_already_pending"},
            )
            return
        if isinstance(result, Decision):
            await self._post_decision(event.run_id, result)
            return

        self._approval_gate = gate
        self._approval_run_id = event.run_id
        readout = gate.readout()
        self._approval_deadline = readout.deadline_at
        await self._emit_approval_request(readout)
        self._schedule_approval_expire(readout.deadline_at)
        gate.playback_started()
        self._readout_task = self._spawn(
            self._speak_approval_readout(gate, readout.text),
            "call_loop.approval_readout",
        )

    async def _speak_approval_readout(self, gate: ApprovalGate, text: str) -> None:
        try:
            await self._speak_text(text)
        finally:
            gate.playback_finished()

    async def _answer_approval(self, text: str) -> None:
        gate = self._approval_gate
        if gate is None:
            return
        run_id = self._approval_run_id
        utterance = Utterance(
            text=text,
            provenance=Provenance.SPOKEN,
            confidence=1.0,
            onset_at=time.monotonic(),
        )
        try:
            decision = gate.answer(utterance)
        except StaleControlIdError:
            await self._reask_approval()
            return
        if decision is not None:
            await self._post_decision(run_id, decision)
            self._clear_approval()
            return
        if gate.needs_reask:
            await self._reask_approval()

    async def _reask_approval(self) -> None:
        gate = self._approval_gate
        if gate is None or gate.pending is None:
            self._clear_approval()
            return
        try:
            readout = gate.reask()
        except ApprovalGateError:
            self._clear_approval()
            return
        await self._emit_approval_request(readout)
        gate.playback_started()
        self._readout_task = self._spawn(
            self._speak_approval_readout(gate, readout.text),
            "call_loop.approval_reask",
        )

    async def _emit_approval_request(self, readout: Any) -> None:
        deadline_s = max(0.0, readout.deadline_at - time.monotonic())
        await self._emit_message(
            _message(
                "approval-request",
                prompt_id=readout.prompt_id,
                execution_generation=readout.execution_generation,
                text=readout.text,
                deadline_s=round(deadline_s, 3),
            )
        )

    def _schedule_approval_expire(self, deadline_at: float) -> None:
        self._cancel_approval_timer()
        delay = max(0.0, deadline_at - time.monotonic())
        self._approval_timer = self._scheduler(delay, self._on_approval_expire)

    def _on_approval_expire(self) -> None:
        self._approval_task = self._spawn(self._expire_approval(), "call_loop.approval_expire")

    async def _expire_approval(self) -> None:
        gate = self._approval_gate
        if gate is None:
            return
        deadline = self._approval_deadline
        decision = gate.expire(now=deadline + 1e-6 if deadline is not None else None)
        if decision is None:
            return
        await self._post_decision(self._approval_run_id, decision)
        self._clear_approval()

    async def _post_decision(self, run_id: str | None, decision: Decision) -> None:
        if run_id is not None:
            try:
                await self._hermes.approval(
                    run_id,
                    request_id=decision.prompt_id,
                    execution_generation=decision.execution_generation,
                    choice=decision.choice,
                )
            except HermesError as exc:
                _LOGGER.warning(
                    "call_loop.approval_post_failed",
                    extra={
                        "event": "call_loop.approval_post_failed",
                        "error": type(exc).__name__,
                    },
                )
        await self._emit_message(
            _message(
                "approval-decision",
                prompt_id=decision.prompt_id,
                choice=decision.choice,
                reason=decision.reason,
            )
        )

    def _clear_approval(self) -> None:
        self._cancel_approval_timer()
        self._approval_gate = None
        self._approval_run_id = None
        self._approval_deadline = None

    def _cancel_approval_timer(self) -> None:
        handle = self._approval_timer
        self._approval_timer = None
        cancel = getattr(handle, "cancel", None)
        if callable(cancel):
            cancel()

    async def _handle_clarify(self, event: RunEvent) -> None:
        payload = event.payload
        text = _bounded_text(payload.get("question") or payload.get("text"))
        await self._emit_message(
            _message(
                "clarify-request",
                text=text,
                prompt_id=event.prompt_id,
                execution_generation=event.execution_generation,
            )
        )
        if text:
            await self._speak_text(text)

    # --- call cap ----------------------------------------------------------

    def _schedule_call_cap(self) -> None:
        cap = self._call_cap_seconds
        if cap <= 0:
            return
        warning = self._call_cap_warning_seconds
        if 0 < warning < cap:
            self._warning_timer = self._scheduler(cap - warning, self._on_cap_warning)
        self._cap_timer = self._scheduler(cap, self._on_cap)

    def _on_cap_warning(self) -> None:
        self._warning_task = self._spawn(self._call_cap_warning(), "call_loop.cap_warning")

    def _on_cap(self) -> None:
        self._cap_task = self._spawn(self._call_cap(), "call_loop.cap")

    async def _call_cap_warning(self) -> None:
        seconds = max(1, round(self._call_cap_warning_seconds))
        await self._emit_message(_message("call-cap-warning", seconds_left=seconds))
        await self._speak_text(CALL_CAP_WARNING_TEXT.format(seconds=seconds))

    async def _call_cap(self) -> None:
        duration_s = await self._speak_text(CALL_CAP_GOODBYE)
        await asyncio.sleep(min(duration_s + 0.1, 5.0))
        await self.request_close("call_cap")

    # --- speech and sinks --------------------------------------------------

    async def _speak_text(self, text: str) -> float:
        """Speak one utterance, returning the emitted audio duration in seconds.

        All speech paths share one lock so concurrent narration, delegation
        updates, and replies never overlap two TTS streams.  Cancellation while
        the lock is held releases it and still closes the active TTS.
        """
        normalized = _bounded_text(text)
        if not normalized or self._closed:
            return 0.0
        emitted_s = 0.0
        async with self._speak_lock:
            if self._closed:
                return emitted_s
            tts = self._tts_factory()
            self._active_tts = tts
            await self._emit_tts_started()
            try:
                await tts.push_text(normalized)
                await tts.flush()
                async for chunk in tts.chunks():
                    await self._emit_audio(chunk)
                    emitted_s += chunk.duration_ms / _MILLISECONDS_PER_SECOND
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.exception(
                    "call_loop.tts_failed",
                    extra={"event": "call_loop.tts_failed", "error": type(exc).__name__},
                )
            finally:
                await self._close_resource(tts)
                if self._active_tts is tts:
                    self._active_tts = None
                await self._emit_tts_stopped()
        return emitted_s

    async def _emit_tts_started(self) -> None:
        if self._tts_open:
            return
        self._tts_open = True
        await self._emit_message(_message("bot-tts-started"))

    async def _emit_tts_stopped(self) -> None:
        if not self._tts_open:
            return
        self._tts_open = False
        await self._emit_message(_message("bot-tts-stopped"))

    def _flush_playback(self) -> None:
        tts = self._active_tts
        if tts is not None:
            clear = getattr(tts, "clear", None)
            if callable(clear):
                try:
                    clear()
                except Exception:
                    _LOGGER.exception(
                        "call_loop.tts_clear_failed",
                        extra={"event": "call_loop.tts_clear_failed"},
                    )
        if self._outbound_clear is not None:
            try:
                self._outbound_clear()
            except Exception:
                _LOGGER.exception(
                    "call_loop.outbound_clear_failed",
                    extra={"event": "call_loop.outbound_clear_failed"},
                )
        gate = self._approval_gate
        if gate is not None:
            try:
                gate.barge_in()
            except Exception:
                _LOGGER.exception(
                    "call_loop.approval_barge_in_failed",
                    extra={"event": "call_loop.approval_barge_in_failed"},
                )

    async def _emit_message(self, message: Mapping[str, Any]) -> None:
        result = self._emit_message_fn(message)
        if inspect.isawaitable(result):
            await result

    async def _emit_audio(self, chunk: AudioChunk) -> None:
        result = self._emit_audio_fn(chunk)
        if inspect.isawaitable(result):
            await result

    async def _close_resource(self, resource: Any) -> None:
        close = getattr(resource, "close", None)
        if not callable(close):
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "call_loop.close_failed",
                extra={"event": "call_loop.close_failed"},
            )

    def _spawn(self, coroutine: Awaitable[Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)  # type: ignore[arg-type]
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _cancel_timers(self) -> None:
        for handle in (self._warning_timer, self._cap_timer):
            cancel = getattr(handle, "cancel", None)
            if callable(cancel):
                cancel()
        self._warning_timer = None
        self._cap_timer = None
        self._cancel_approval_timer()


__all__ = [
    "CALL_CAP_GOODBYE",
    "CALL_CAP_WARNING_TEXT",
    "DEFAULT_CALL_CAP_WARNING_SECONDS",
    "ROUTER_FAILURE_TEXT",
    "CallLoop",
]

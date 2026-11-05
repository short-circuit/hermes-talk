"""RTVI/SmallWebRTC signalling and audio bridging for the production relay.

This module mirrors the signalling contract that ``android/stub-relay/relay.py``
implements for the Android client:

* ``POST /start`` admits a call session and returns an empty ICE server list.
* ``POST /sessions/{id}/api/offer`` answers an SDP offer.
* ``PATCH /sessions/{id}/api/offer`` accepts trickle ICE candidates.
* ``DELETE /sessions/{id}`` closes the session and releases its lease.

The peer connection, audio track, and data channel come from ``aiortc``.  Those
imports happen lazily, inside the functions that need them, so the relay shell
still boots when the optional ``webrtc`` extra is not installed.  In that case
``/start`` answers ``503 {"error": "rtvi_unavailable"}`` and readiness reports
``rtvi`` as not ok.

The call pipeline itself lives in :mod:`relay.transport.call_loop` and never
imports ``aiortc``.  This module only converts WebRTC audio frames to
:class:`~relay.speech.audio.PcmChunk`, feeds decoded Opus packets back into an
outbound track, and forwards RTVI envelopes over the ``chat`` data channel.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from fractions import Fraction
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from relay.auth.deps import require_device_token
from relay.auth.tokens import DeviceToken
from relay.brain.router import VoiceBrainRouter
from relay.hermes.client import HermesClient
from relay.limits import AdmissionController, LimitError, SessionLease
from relay.speech.audio import PcmChunk
from relay.speech.stt import create_stt
from relay.speech.tts import AudioChunk, create_tts_factory
from relay.transport.call_loop import CallLoop

_LOGGER = logging.getLogger(__name__)

_INBOUND_SAMPLE_RATE = 16000
_OUTBOUND_SAMPLE_RATE = 48000
_OUTBOUND_FRAME_MS = 20
_OUTBOUND_SAMPLES = _OUTBOUND_SAMPLE_RATE * _OUTBOUND_FRAME_MS // 1000
_OUTBOUND_FRAME_SECONDS = _OUTBOUND_FRAME_MS / 1000.0
_BYTES_PER_SAMPLE = 2
_MIC_QUEUE_MAX = 64
_ICE_GATHER_TIMEOUT_S = 2.0
_RTVI_VERSION = "2.0.0"
_REQUIRED_MODULES = ("aiortc", "av", "numpy")

_TRACK_CLASS: Any | None = None


class RtviError(RuntimeError):
    """Base class for RTVI transport failures."""


class RtviUnavailable(RtviError):
    """The optional WebRTC dependencies are not installed."""


class RtviUnknownSession(RtviError):
    """A signalling request named a session that does not exist."""


def rtvi_available() -> tuple[bool, str]:
    """Return whether the WebRTC extra is installed and a human-readable detail."""
    missing = [name for name in _REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        return False, f"missing webrtc dependencies: {', '.join(missing)}"
    return True, "aiortc, av, and numpy installed"


def _import_aiortc() -> Any:
    try:
        import aiortc
        import aiortc.mediastreams
    except ImportError as exc:
        msg = (
            "aiortc is required for RTVI calls; install the relay 'webrtc' extra (./relay[webrtc])"
        )
        raise RtviUnavailable(msg) from exc
    return aiortc


def _import_av() -> Any:
    try:
        import av
    except ImportError as exc:
        msg = (
            "PyAV is required for RTVI audio bridging; "
            "install the relay 'webrtc' extra (./relay[webrtc])"
        )
        raise RtviUnavailable(msg) from exc
    return av


def _secret_text(value: Any) -> str | None:
    if value is None:
        return None
    get_secret = getattr(value, "get_secret_value", None)
    if callable(get_secret):
        return str(get_secret())
    return str(value)


def _message(message_type: str, **data: Any) -> dict[str, Any]:
    return {"label": "rtvi", "type": message_type, "data": data}


def _close_awaitable(result: Any) -> Awaitable[None]:
    async def _wait() -> None:
        if inspect.isawaitable(result):
            await result

    return _wait()


def _outbound_track_class() -> Any:
    """Build and cache the aiortc track subclass on first use."""
    global _TRACK_CLASS
    if _TRACK_CLASS is not None:
        return _TRACK_CLASS

    aiortc = _import_aiortc()
    MediaStreamTrack = aiortc.mediastreams.MediaStreamTrack
    MediaStreamError = aiortc.mediastreams.MediaStreamError

    class _OutboundAudioTrack(MediaStreamTrack):
        """A 48 kHz mono s16 track that plays decoded Opus or paced silence."""

        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self._av = _import_av()
            self._decoder = self._av.CodecContext.create("opus", "r")
            self._resampler = self._av.AudioResampler(
                format="s16",
                layout="mono",
                rate=_OUTBOUND_SAMPLE_RATE,
            )
            self._pending = bytearray()
            self._pts = 0
            self._closed = False

        def push_chunk(self, chunk: AudioChunk) -> None:
            """Decode one Opus chunk into the outbound PCM buffer."""
            if self._closed:
                return
            for packet in chunk.opus_packets:
                try:
                    frames = self._decoder.decode(self._av.Packet(packet))
                except Exception:
                    _LOGGER.warning(
                        "rtvi.opus_decode_failed",
                        extra={"event": "rtvi.opus_decode_failed"},
                    )
                    continue
                for frame in frames:
                    for resampled in self._resampler.resample(frame):
                        self._pending.extend(
                            bytes(resampled.planes[0])[: resampled.samples * _BYTES_PER_SAMPLE]
                        )

        def clear(self) -> None:
            """Drop buffered audio immediately for barge-in."""
            self._pending.clear()

        async def recv(self) -> Any:
            if self._closed:
                raise MediaStreamError
            await asyncio.sleep(_OUTBOUND_FRAME_SECONDS)
            if self._closed:
                raise MediaStreamError
            frame_bytes = _OUTBOUND_SAMPLES * _BYTES_PER_SAMPLE
            if len(self._pending) >= frame_bytes:
                data = bytes(self._pending[:frame_bytes])
                del self._pending[:frame_bytes]
            else:
                data = bytes(frame_bytes)
            frame = self._av.AudioFrame(
                format="s16",
                layout="mono",
                samples=_OUTBOUND_SAMPLES,
            )
            frame.sample_rate = _OUTBOUND_SAMPLE_RATE
            frame.pts = self._pts
            frame.time_base = Fraction(1, _OUTBOUND_SAMPLE_RATE)
            frame.planes[0].update(data)
            self._pts += _OUTBOUND_SAMPLES
            return frame

        def stop(self) -> None:
            self._closed = True
            super().stop()

    _TRACK_CLASS = _OutboundAudioTrack
    return _TRACK_CLASS


def create_outbound_track() -> Any:
    """Create one outbound audio track; requires the WebRTC extra."""
    return _outbound_track_class()()


class _InboundAudioConverter:
    """Convert arbitrary WebRTC audio frames to 16 kHz mono PCM16."""

    def __init__(self) -> None:
        self._av = _import_av()
        self._resampler = self._av.AudioResampler(
            format="s16",
            layout="mono",
            rate=_INBOUND_SAMPLE_RATE,
        )

    def convert(self, frame: Any) -> PcmChunk | None:
        try:
            resampled_frames = self._resampler.resample(frame)
        except Exception:
            _LOGGER.warning(
                "rtvi.inbound_resample_failed",
                extra={"event": "rtvi.inbound_resample_failed"},
            )
            return None
        parts: list[bytes] = []
        for resampled in resampled_frames:
            parts.append(bytes(resampled.planes[0])[: resampled.samples * _BYTES_PER_SAMPLE])
        data = b"".join(parts)
        if not data:
            return None
        return PcmChunk(data=data, sample_rate=_INBOUND_SAMPLE_RATE)


class _LazySttAdapter:
    """Build the configured STT adapter once, on the first call."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._adapter: Any | None = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        adapter = self._adapter
        return str(getattr(adapter, "name", "lazy")) if adapter is not None else "lazy"

    def _get(self) -> Any:
        with self._lock:
            if self._adapter is None:
                self._adapter = self._factory()
            return self._adapter

    def describe(self) -> dict[str, object]:
        adapter = self._adapter
        if adapter is None:
            return {"engine": "lazy", "loaded": False}
        describe = getattr(adapter, "describe", None)
        if callable(describe):
            return dict(describe())
        return {"engine": self.name, "loaded": True}

    async def start_session(self) -> Any:
        adapter = await asyncio.to_thread(self._get)
        return await adapter.start_session()


class RtviSession:
    """One RTVI call: peer connection, audio bridge, data channel, and call loop."""

    def __init__(
        self,
        manager: RtviSessionManager,
        session_id: str,
        lease: SessionLease,
        settings: Any,
    ) -> None:
        self.manager = manager
        self.session_id = session_id
        self.lease = lease
        self.settings = settings
        self._pc: Any | None = None
        self._track: Any | None = None
        self._channel: Any | None = None
        self._inbound: _InboundAudioConverter | None = None
        self._call_loop: CallLoop | None = None
        self._hermes: Any | None = None
        self._mic_queue: asyncio.Queue[PcmChunk] = asyncio.Queue(maxsize=_MIC_QUEUE_MAX)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._bot_ready_sent = False
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def offer(self, sdp: str, offer_type: str) -> dict[str, str]:
        """Create the peer connection and return the SDP answer."""
        aiortc = _import_aiortc()
        if self._pc is not None:
            await self._pc.close()
            self._pc = None

        pc = aiortc.RTCPeerConnection(aiortc.RTCConfiguration(iceServers=[]))
        self._pc = pc
        ice_complete = asyncio.Event()

        @pc.on("icegatheringstatechange")
        def _on_icegatheringstatechange() -> None:
            if pc.iceGatheringState == "complete":
                ice_complete.set()

        @pc.on("datachannel")
        def _on_datachannel(channel: Any) -> None:
            self._attach_channel(channel)

        @pc.on("track")
        def _on_track(track: Any) -> None:
            if track.kind != "audio":
                return
            self._spawn(self._consume_track(track), "rtvi.inbound")

        @pc.on("connectionstatechange")
        def _on_connectionstatechange() -> None:
            state = pc.connectionState
            if state in {"failed", "closed"} and not self._closed:
                self._spawn(
                    self.manager.close_session(self.session_id, reason=f"pc_{state}"),
                    "rtvi.pc_close",
                )

        self._track = create_outbound_track()
        pc.addTrack(self._track)
        self._inbound = _InboundAudioConverter()

        await pc.setRemoteDescription(aiortc.RTCSessionDescription(sdp=sdp, type=offer_type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        if pc.iceGatheringState == "complete":
            ice_complete.set()
        with suppress(TimeoutError):
            await asyncio.wait_for(ice_complete.wait(), timeout=_ICE_GATHER_TIMEOUT_S)

        description = pc.localDescription
        if description is None:
            msg = "peer connection has no local description"
            raise RtviError(msg)

        await self._ensure_call_loop()
        self._spawn(self._mic_worker(), "rtvi.mic")

        return {
            "sdp": description.sdp,
            "type": description.type,
            "pc_id": self.session_id,
        }

    async def add_candidates(self, candidates: list[Any]) -> None:
        """Add trickle ICE candidates; duplicates and bad entries are ignored."""
        if self._pc is None:
            return
        try:
            from aioice import Candidate as AioiceCandidate
            from aiortc.rtcicetransport import candidate_from_aioice
        except ImportError as exc:
            raise RtviUnavailable("aioice is required for trickle ICE") from exc

        for item in candidates:
            if not isinstance(item, Mapping):
                continue
            raw = item.get("candidate")
            if not isinstance(raw, str) or not raw:
                continue
            try:
                candidate = candidate_from_aioice(AioiceCandidate.from_sdp(raw))
                candidate.sdpMid = item.get("sdpMid")
                candidate.sdpMLineIndex = item.get("sdpMLineIndex")
                await self._pc.addIceCandidate(candidate)
            except Exception:
                _LOGGER.debug(
                    "rtvi.candidate_ignored",
                    extra={"event": "rtvi.candidate_ignored", "session_id": self.session_id},
                )

    async def close(self, reason: str) -> None:
        """Close the call loop, peer connection, and outbound track exactly once."""
        if self._closed:
            return
        self._closed = True
        current = asyncio.current_task()
        pending = [task for task in self._tasks if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        loop = self._call_loop
        if loop is not None:
            await loop.close(reason)
        else:
            await self._send_message(_message("session-closed", reason=reason))

        track = self._track
        if track is not None:
            track.stop()
            self._track = None

        pc = self._pc
        self._pc = None
        if pc is not None:
            await pc.close()

        hermes = self._hermes
        self._hermes = None
        if hermes is not None:
            close = getattr(hermes, "aclose", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result

    def send_message(self, message: Mapping[str, Any]) -> None:
        """Send one RTVI envelope; never raise out of a send."""
        channel = self._channel
        if channel is None:
            return
        try:
            if getattr(channel, "readyState", None) != "open":
                return
            channel.send(json.dumps(dict(message), separators=(",", ":")))
        except Exception:
            _LOGGER.warning(
                "rtvi.send_failed",
                extra={"event": "rtvi.send_failed", "session_id": self.session_id},
            )

    def _attach_channel(self, channel: Any) -> None:
        self._channel = channel

        @channel.on("open")
        def _on_open() -> None:
            self._send_bot_ready()

        @channel.on("message")
        def _on_message(message: Any) -> None:
            self._handle_client_message(message)

        @channel.on("close")
        def _on_close() -> None:
            if self._channel is channel:
                self._channel = None

        if getattr(channel, "readyState", None) == "open":
            self._send_bot_ready()

    def _send_bot_ready(self) -> None:
        if self._bot_ready_sent:
            return
        self._bot_ready_sent = True
        self.send_message(_message("bot-ready", version=_RTVI_VERSION))

    def _handle_client_message(self, message: Any) -> None:
        text = message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            _LOGGER.info(
                "rtvi.message_invalid_json",
                extra={"event": "rtvi.message_invalid_json", "session_id": self.session_id},
            )
            return
        if not isinstance(payload, dict):
            return
        message_type = payload.get("type")
        if message_type == "client-ready":
            _LOGGER.info(
                "rtvi.client_ready",
                extra={"event": "rtvi.client_ready", "session_id": self.session_id},
            )
        else:
            _LOGGER.info(
                "rtvi.message_ignored",
                extra={
                    "event": "rtvi.message_ignored",
                    "session_id": self.session_id,
                    "message_type": str(message_type),
                },
            )

    async def _consume_track(self, track: Any) -> None:
        aiortc = _import_aiortc()
        error_type = aiortc.mediastreams.MediaStreamError
        try:
            while not self._closed:
                frame = await track.recv()
                converter = self._inbound
                if converter is None:
                    continue
                chunk = converter.convert(frame)
                if chunk is not None:
                    self._enqueue_mic(chunk)
        except error_type:
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.warning(
                "rtvi.inbound_track_failed",
                extra={"event": "rtvi.inbound_track_failed", "session_id": self.session_id},
            )

    def _enqueue_mic(self, chunk: PcmChunk) -> None:
        try:
            self._mic_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            with suppress(asyncio.QueueEmpty):
                self._mic_queue.get_nowait()
            with suppress(asyncio.QueueFull):
                self._mic_queue.put_nowait(chunk)

    async def _mic_worker(self) -> None:
        while not self._closed:
            chunk = await self._mic_queue.get()
            loop = self._call_loop
            if loop is None:
                continue
            try:
                await loop.handle_mic(chunk)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.warning(
                    "rtvi.mic_failed",
                    extra={"event": "rtvi.mic_failed", "session_id": self.session_id},
                )

    async def _ensure_call_loop(self) -> None:
        if self._call_loop is not None:
            return
        manager = self.manager
        self._hermes = manager.create_hermes()
        loop = CallLoop(
            self.settings,
            emit_message=self.send_message,
            emit_audio=self._push_audio,
            tts_factory=manager.tts_factory,
            router=manager.router,
            hermes=self._hermes,
            stt_adapter=manager.stt_adapter,
            delegation_factory=manager.delegation_factory,
            narrator=manager.narrator,
            approval_gate_factory=manager.approval_gate_factory,
            outbound_clear=self._clear_outbound,
            scheduler=manager.scheduler,
            call_cap_seconds=manager.call_cap_seconds,
            call_cap_warning_seconds=manager.call_cap_warning_seconds,
            on_close=lambda reason: manager.close_session(self.session_id, reason=reason),
            session_id=self.session_id,
            session_key=manager.session_key,
        )
        self._call_loop = loop
        await loop.start()

    def _push_audio(self, chunk: AudioChunk) -> None:
        track = self._track
        if track is not None:
            track.push_chunk(chunk)

    def _clear_outbound(self) -> None:
        track = self._track
        if track is not None:
            track.clear()

    async def _send_message(self, message: Mapping[str, Any]) -> None:
        self.send_message(message)

    def _spawn(self, awaitable: Awaitable[Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


class RtviSessionManager:
    """Own RTVI sessions and their admission leases for one relay process."""

    def __init__(
        self,
        app: Any,
        settings: Any,
        limits: AdmissionController,
        *,
        stt_factory: Callable[[], Any] | None = None,
        tts_factory: Callable[[], Any] | None = None,
        router_factory: Callable[[], Any] | None = None,
        hermes_factory: Callable[[], Any] | None = None,
        delegation_factory: (Callable[[Any, Callable[[Any], Awaitable[None]]], Any] | None) = None,
        narrator: Any | None = None,
        approval_gate_factory: Callable[[], Any] | None = None,
        scheduler: Callable[[float, Callable[[], None]], Any] | None = None,
        call_cap_seconds: float | None = None,
        call_cap_warning_seconds: float | None = None,
    ) -> None:
        self.app = app
        self.settings = settings
        self.limits = limits
        self.stt_factory = stt_factory if stt_factory is not None else lambda: create_stt(settings)
        self.tts_factory = tts_factory if tts_factory is not None else create_tts_factory(settings)
        self.router_factory = router_factory
        self.hermes_factory = hermes_factory
        self.delegation_factory = delegation_factory
        self.narrator = narrator
        self.approval_gate_factory = approval_gate_factory
        self.scheduler = scheduler
        self.call_cap_seconds = call_cap_seconds
        self.call_cap_warning_seconds = call_cap_warning_seconds
        self.session_key = _secret_text(getattr(settings, "hermes_session_key", None))
        self._stt_adapter = _LazySttAdapter(self.stt_factory)
        self._router: Any | None = None
        self._sessions: dict[str, RtviSession] = {}
        self._closed = False

    @property
    def stt_adapter(self) -> _LazySttAdapter:
        return self._stt_adapter

    @property
    def router(self) -> Any:
        if self._router is None:
            if self.router_factory is not None:
                self._router = self.router_factory()
            else:
                self._router = VoiceBrainRouter.from_settings(self.settings)
        return self._router

    def availability(self) -> tuple[bool, str]:
        return rtvi_available()

    def available(self) -> bool:
        return self.availability()[0]

    def create_hermes(self) -> Any:
        if self.hermes_factory is not None:
            return self.hermes_factory()
        return HermesClient.from_settings(self.settings)

    async def start_session(self, device_id: str) -> RtviSession:
        """Admit one call session and register its lease stop hook."""
        if not self.available():
            _, detail = self.availability()
            raise RtviUnavailable(detail)
        lease = await self.limits.acquire_session(device_id)
        session_id = str(uuid.uuid4())
        session = RtviSession(self, session_id, lease, self.settings)
        self._sessions[session_id] = session

        async def _stop_hook() -> None:
            await self.close_session(session_id, reason="lease_stop")

        lease.register_stop(_stop_hook)
        return session

    def get_session(self, session_id: str) -> RtviSession | None:
        return self._sessions.get(session_id)

    async def offer(self, session_id: str, sdp: str, offer_type: str) -> dict[str, str]:
        session = self._sessions.get(session_id)
        if session is None:
            raise RtviUnknownSession(session_id)
        return await session.offer(sdp, offer_type)

    async def add_candidates(self, session_id: str, candidates: list[Any]) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            raise RtviUnknownSession(session_id)
        await session.add_candidates(candidates)

    async def close_session(self, session_id: str, *, reason: str) -> bool:
        """Close one session and release its lease exactly once."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        try:
            await session.close(reason)
        finally:
            await self.limits.close_session(session.lease.session_id, reason=reason)
        return True

    async def close_all(self, *, reason: str = "shutdown") -> None:
        """Close every RTVI session, then release the shared router client."""
        self._closed = True
        for session_id in tuple(self._sessions):
            await self.close_session(session_id, reason=reason)
        router = self._router
        self._router = None
        if router is not None:
            close = getattr(router, "aclose", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result


router = APIRouter()


def _manager(request: Request) -> RtviSessionManager:
    return request.app.state.rtvi


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


@router.post("/start")
async def start_call(
    request: Request,
    device: Annotated[DeviceToken, Depends(require_device_token)],
) -> JSONResponse:
    """Admit a call session and return its id and empty ICE configuration."""
    manager = _manager(request)
    if not manager.available():
        return _error("rtvi_unavailable", 503)
    try:
        session = await manager.start_session(device.token_id)
    except RtviUnavailable:
        return _error("rtvi_unavailable", 503)
    except LimitError as exc:
        correlation_id = getattr(request.state, "correlation_id", None)
        return JSONResponse(
            exc.to_body(correlation_id),
            status_code=exc.status_code,
            headers=exc.headers(),
        )
    return JSONResponse(
        {
            "sessionId": session.session_id,
            "iceConfig": {"iceServers": []},
        }
    )


async def _read_json_object(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    return body


@router.post("/sessions/{session_id}/api/offer")
async def post_offer(
    session_id: str,
    request: Request,
    device: Annotated[DeviceToken, Depends(require_device_token)],
) -> JSONResponse:
    """Answer an SDP offer for an admitted session."""
    del device
    manager = _manager(request)
    if not manager.available():
        return _error("rtvi_unavailable", 503)
    body = await _read_json_object(request)
    if body is None:
        return _error("invalid_json", 400)
    sdp = body.get("sdp")
    if not isinstance(sdp, str) or not sdp:
        return _error("missing_sdp", 400)
    offer_type = body.get("type", "offer")
    if not isinstance(offer_type, str) or not offer_type:
        return _error("invalid_type", 400)
    try:
        answer = await manager.offer(session_id, sdp, offer_type)
    except RtviUnknownSession:
        return _error("unknown session", 404)
    except RtviUnavailable:
        return _error("rtvi_unavailable", 503)
    except Exception as exc:
        _LOGGER.exception(
            "rtvi.offer_failed",
            extra={"event": "rtvi.offer_failed", "session_id": session_id},
        )
        return _error(str(exc), 500)
    return JSONResponse(answer)


@router.patch("/sessions/{session_id}/api/offer")
async def patch_offer(
    session_id: str,
    request: Request,
    device: Annotated[DeviceToken, Depends(require_device_token)],
) -> JSONResponse:
    """Accept trickle ICE candidates for an existing session."""
    del device
    manager = _manager(request)
    if not manager.available():
        return _error("rtvi_unavailable", 503)
    body = await _read_json_object(request)
    if body is None:
        return _error("invalid_json", 400)
    candidates = body.get("candidates", [])
    if not isinstance(candidates, list):
        return _error("candidates must be a list", 400)
    try:
        await manager.add_candidates(session_id, candidates)
    except RtviUnknownSession:
        return _error("unknown session", 404)
    except RtviUnavailable:
        return _error("rtvi_unavailable", 503)
    except Exception as exc:
        _LOGGER.exception(
            "rtvi.patch_failed",
            extra={"event": "rtvi.patch_failed", "session_id": session_id},
        )
        return _error(str(exc), 500)
    return JSONResponse({"status": "ok"})


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request) -> JSONResponse:
    """Close a session and release its lease; idempotent for the fixture contract."""
    manager = _manager(request)
    await manager.close_session(session_id, reason="client_delete")
    return JSONResponse({"status": "ok"})


__all__ = [
    "RtviError",
    "RtviSession",
    "RtviSessionManager",
    "RtviUnavailable",
    "RtviUnknownSession",
    "create_outbound_track",
    "router",
    "rtvi_available",
]

#!/usr/bin/env python3
"""Stub Hermes Talk relay for exercising the Android RTVI client.

This is a test fixture, not the real relay. It speaks the Pipecat
SmallWebRTC/RTVI signalling surface just far enough for the Android scaffold to
reach ``TransportState.Connected`` and ``TransportState.Ready``:

* ``POST /start`` creates a session and returns an empty ICE server list.
* ``POST /sessions/{id}/api/offer`` answers an SDP offer after ICE gathering.
* ``PATCH /sessions/{id}/api/offer`` accepts trickle ICE candidates.
* The ``chat`` data channel receives the RTVI ``client-ready`` message and the
  server sends ``bot-ready`` when the channel opens.
* An optional 440 Hz tone is sent to the client and incoming audio frames are
  counted.

There is no STT, TTS, LLM, TLS, or real authentication beyond an optional
bearer token. Run it on loopback and use ``adb reverse`` for a USB device.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import json
import logging
import math
import re
import threading
import uuid
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse

import av
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.rtcicetransport import candidate_from_aioice
from aioice import Candidate as AioiceCandidate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("stub-relay")
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("aioice").setLevel(logging.WARNING)

SESSION_PATH_RE = re.compile(r"^/sessions/([0-9a-fA-F-]+)(?:/api/offer)?$")
OFFER_PATH_RE = re.compile(r"^/sessions/([0-9a-fA-F-]+)/api/offer$")
SESSIONS: dict[str, "Session"] = {}
SESSIONS_LOCK = threading.Lock()
EVENT_LOOP: Optional[asyncio.AbstractEventLoop] = None
AUTH_TOKEN: Optional[str] = None
TONE_ENABLED = True


class ToneTrack(MediaStreamTrack):
    """A 48 kHz mono s16 sine tone track."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._closed = False
        self._pts = 0
        self._phase = 0.0
        self._sample_rate = 48000
        self._samples = 960
        self._frequency = 440.0

    async def recv(self) -> av.AudioFrame:
        if self._closed:
            raise MediaStreamError
        await asyncio.sleep(0.02)
        samples = array.array("h")
        for _ in range(self._samples):
            samples.append(int(0.1 * 32767 * math.sin(self._phase)))
            self._phase += 2.0 * math.pi * self._frequency / self._sample_rate
            if self._phase > 2.0 * math.pi:
                self._phase -= 2.0 * math.pi
        frame = av.AudioFrame(format="s16", layout="mono", samples=self._samples)
        frame.sample_rate = self._sample_rate
        frame.pts = self._pts
        frame.time_base = Fraction(1, self._sample_rate)
        frame.planes[0].update(samples.tobytes())
        self._pts += self._samples
        return frame

    def stop(self) -> None:
        self._closed = True
        super().stop()


class Session:
    """One RTVI session and its WebRTC peer connection."""

    def __init__(self, session_id: str) -> None:
        self.id = session_id
        self.pc: Optional[RTCPeerConnection] = None
        self.channel: Any = None
        self.tone: Optional[ToneTrack] = None
        self.ice_complete: Optional[asyncio.Event] = None
        self.audio_frames = 0
        self.bot_ready_sent = False
        self.closed = False


def _send_bot_ready(channel: Any, session: Session) -> None:
    if session.bot_ready_sent:
        return
    message = {
        "label": "rtvi",
        "type": "bot-ready",
        "data": {"version": "2.0.0"},
    }
    try:
        channel.send(json.dumps(message))
        session.bot_ready_sent = True
        LOG.info("session %s bot-ready sent", session.id)
    except Exception as error:  # pragma: no cover - defensive
        LOG.info("session %s bot-ready send failed: %s", session.id, error)


async def _create_peer(session: Session) -> RTCPeerConnection:
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    session.pc = pc
    session.ice_complete = asyncio.Event()

    @pc.on("datachannel")
    def on_datachannel(channel: Any) -> None:
        session.channel = channel
        LOG.info("session %s datachannel %s", session.id, channel.label)

        @channel.on("open")
        def on_open() -> None:
            LOG.info("session %s datachannel open", session.id)
            _send_bot_ready(channel, session)

        # A remotely-created channel can already be open when the datachannel
        # event fires, in which case aiortc does not emit a second "open"
        # event. Send bot-ready immediately in that case.
        if channel.readyState == "open":
            LOG.info("session %s datachannel already open", session.id)
            _send_bot_ready(channel, session)

        @channel.on("message")
        def on_message(message: Any) -> None:
            text = message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message)
            LOG.info("session %s datachannel message: %s", session.id, text[:200])
            if "client-ready" in text:
                LOG.info("session %s client-ready received", session.id)

        @channel.on("close")
        def on_close() -> None:
            LOG.info("session %s datachannel closed", session.id)
            session.channel = None

    @pc.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        LOG.info("session %s received %s track", session.id, track.kind)
        if track.kind == "audio":
            asyncio.ensure_future(_consume_audio(session, track))

    @pc.on("icegatheringstatechange")
    def on_icegatheringstatechange() -> None:
        LOG.info("session %s ice gathering %s", session.id, pc.iceGatheringState)
        if pc.iceGatheringState == "complete" and session.ice_complete is not None:
            session.ice_complete.set()

    if TONE_ENABLED:
        session.tone = ToneTrack()
        pc.addTrack(session.tone)

    return pc


async def _consume_audio(session: Session, track: MediaStreamTrack) -> None:
    try:
        while True:
            await track.recv()
            session.audio_frames += 1
            if session.audio_frames % 100 == 0:
                LOG.info("session %s audio frames received: %d", session.id, session.audio_frames)
    except MediaStreamError:
        LOG.info("session %s audio track ended", session.id)
    except Exception as error:  # pragma: no cover - defensive
        LOG.info("session %s audio track error: %s", session.id, error)


async def _handle_offer(session_id: str, sdp: str, offer_type: str) -> dict[str, Any]:
    with SESSIONS_LOCK:
        session = SESSIONS.get(session_id)
    if session is None:
        raise KeyError(session_id)
    if session.pc is not None:
        await session.pc.close()
    pc = await _create_peer(session)
    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=offer_type))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    if session.ice_complete is not None:
        try:
            await asyncio.wait_for(session.ice_complete.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            LOG.info("session %s ice gathering timed out", session.id)
    description = pc.localDescription
    if description is None:
        raise RuntimeError("peer connection has no local description")
    LOG.info("session %s offer answered", session.id)
    return {
        "sdp": description.sdp,
        "type": description.type,
        "pc_id": session.id,
    }


async def _add_candidates(session_id: str, candidates: list[dict[str, Any]]) -> None:
    with SESSIONS_LOCK:
        session = SESSIONS.get(session_id)
    if session is None or session.pc is None:
        return
    for item in candidates:
        raw = item.get("candidate")
        if not raw:
            continue
        try:
            candidate = candidate_from_aioice(AioiceCandidate.from_sdp(raw))
            candidate.sdpMid = item.get("sdpMid")
            candidate.sdpMLineIndex = item.get("sdpMLineIndex")
            await session.pc.addIceCandidate(candidate)
        except Exception as error:  # pragma: no cover - duplicate candidates are harmless
            LOG.info("session %s candidate ignored: %s", session_id, error)


async def _close_session(session_id: str) -> None:
    with SESSIONS_LOCK:
        session = SESSIONS.pop(session_id, None)
    if session is None:
        return
    session.closed = True
    if session.pc is not None:
        await session.pc.close()
    if session.tone is not None:
        session.tone.stop()
    LOG.info("session %s closed", session_id)


class RelayRequestHandler(BaseHTTPRequestHandler):
    """HTTP signalling surface for the stub relay."""

    server_version = "HermesTalkStubRelay/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if AUTH_TOKEN is None:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {AUTH_TOKEN}"

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._send_json(401, {"error": "unauthorized"})
        return False

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid JSON body: {error}") from error
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/ready":
            self._send_json(200, {"status": "ready"})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path == "/start":
            if not self._require_auth():
                return
            session_id = str(uuid.uuid4())
            with SESSIONS_LOCK:
                SESSIONS[session_id] = Session(session_id)
            LOG.info("session %s created", session_id)
            self._send_json(
                200,
                {"sessionId": session_id, "iceConfig": {"iceServers": []}},
            )
            return

        offer_match = OFFER_PATH_RE.match(path)
        if offer_match:
            if not self._require_auth():
                return
            session_id = offer_match.group(1)
            with SESSIONS_LOCK:
                exists = session_id in SESSIONS
            if not exists:
                self._send_json(404, {"error": "unknown session"})
                return
            try:
                body = self._read_json()
                sdp = body.get("sdp")
                offer_type = body.get("type", "offer")
                if not isinstance(sdp, str) or not sdp:
                    raise ValueError("missing sdp")
                if EVENT_LOOP is None:
                    raise RuntimeError("relay event loop is not running")
                future = asyncio.run_coroutine_threadsafe(
                    _handle_offer(session_id, sdp, str(offer_type)),
                    EVENT_LOOP,
                )
                answer = future.result(timeout=20.0)
                self._send_json(200, answer)
            except Exception as error:
                LOG.exception("session %s offer failed", session_id)
                self._send_json(500, {"error": str(error)})
            return

        self._send_json(404, {"error": "not found"})

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        offer_match = OFFER_PATH_RE.match(path)
        if not offer_match:
            self._send_json(404, {"error": "not found"})
            return
        if not self._require_auth():
            return
        session_id = offer_match.group(1)
        with SESSIONS_LOCK:
            exists = session_id in SESSIONS
        if not exists:
            self._send_json(404, {"error": "unknown session"})
            return
        try:
            body = self._read_json()
            candidates = body.get("candidates", [])
            if not isinstance(candidates, list):
                raise ValueError("candidates must be a list")
            if EVENT_LOOP is None:
                raise RuntimeError("relay event loop is not running")
            asyncio.run_coroutine_threadsafe(
                _add_candidates(session_id, candidates),
                EVENT_LOOP,
            ).result(timeout=10.0)
            LOG.info("session %s received %d candidates", session_id, len(candidates))
            self._send_json(200, {"status": "ok"})
        except Exception as error:
            LOG.info("session %s candidate update failed: %s", session_id, error)
            self._send_json(500, {"error": str(error)})

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        match = SESSION_PATH_RE.match(path)
        if not match:
            self._send_json(404, {"error": "not found"})
            return
        session_id = match.group(1)
        if EVENT_LOOP is not None:
            asyncio.run_coroutine_threadsafe(_close_session(session_id), EVENT_LOOP)
        self._send_json(200, {"status": "ok"})


class RelayHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server that can be shut down cleanly."""

    daemon_threads = True
    allow_reuse_address = True


def _run_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Talk stub relay (test fixture)")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8790, help="bind port (default: 8790)")
    parser.add_argument("--token", default=None, help="optional bearer token for /start and offers")
    parser.add_argument(
        "--tone",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="serve a 440 Hz tone (default: on)",
    )
    args = parser.parse_args()

    global AUTH_TOKEN, TONE_ENABLED, EVENT_LOOP
    AUTH_TOKEN = args.token
    TONE_ENABLED = args.tone

    loop = asyncio.new_event_loop()
    EVENT_LOOP = loop
    thread = threading.Thread(target=_run_event_loop, args=(loop,), daemon=True)
    thread.start()

    server = RelayHTTPServer((args.host, args.port), RelayRequestHandler)
    LOG.info("stub relay listening on %s:%s", args.host, args.port)
    LOG.info("auth: %s", "enabled" if AUTH_TOKEN is not None else "disabled")
    LOG.info("tone: %s", "on" if TONE_ENABLED else "off")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        server.server_close()
        with SESSIONS_LOCK:
            session_ids = list(SESSIONS.keys())
        for session_id in session_ids:
            asyncio.run_coroutine_threadsafe(_close_session(session_id), loop)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)


if __name__ == "__main__":
    main()

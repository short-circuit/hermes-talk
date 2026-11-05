"""Offline tests for the RTVI signalling contract.

These tests drive the ASGI app with ``httpx.ASGITransport``.  The real aiortc
peer connection is replaced with a small fake so the signalling surface can be
tested on any host, including one without the optional ``webrtc`` extra.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from relay.transport.rtvi import RtviSession

_BASE_URL = "http://relay.test"
_AUTH = {"Authorization": "Bearer token-a"}
_TOKEN_OVERRIDE = {"device_tokens_raw": "token-a"}


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as client:
        return await client.request(
            method,
            path,
            headers=dict(headers) if headers is not None else None,
            json=json_body,
            content=content,
        )


@asynccontextmanager
async def _started_session(
    app: FastAPI,
) -> AsyncIterator[tuple[str, str]]:
    response = await _request(app, "POST", "/start", headers=_AUTH)
    assert response.status_code == 200, response.text
    session_id = str(response.json()["sessionId"])
    try:
        yield session_id, response.text
    finally:
        await app.state.rtvi.close_all(reason="test_cleanup")


def _available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "relay.transport.rtvi.rtvi_available",
        lambda: (True, "fake webrtc available"),
    )


def test_start_requires_a_device_token(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available(monkeypatch)
    app = make_app(**_TOKEN_OVERRIDE)

    response = asyncio.run(_request(app, "POST", "/start"))

    assert response.status_code == 401
    assert response.json() == {
        "error": "unauthorized",
        "reason": "missing_credentials",
    }


def test_start_returns_a_dashed_uuid_and_an_empty_ice_list(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available(monkeypatch)
    app = make_app(**_TOKEN_OVERRIDE)

    async def scenario() -> None:
        async with _started_session(app) as (session_id, raw):
            parsed = uuid.UUID(session_id)
            assert parsed.version == 4
            assert session_id.count("-") == 4
            body = json.loads(raw)
            assert body == {
                "sessionId": session_id,
                "iceConfig": {"iceServers": []},
            }
            assert app.state.rtvi.get_session(session_id) is not None

    asyncio.run(scenario())


def test_start_reports_rtvi_unavailable_without_the_extra(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "relay.transport.rtvi.rtvi_available",
        lambda: (False, "missing webrtc dependencies: aiortc"),
    )
    app = make_app(**_TOKEN_OVERRIDE)

    response = asyncio.run(_request(app, "POST", "/start", headers=_AUTH))

    assert response.status_code == 503
    assert response.json() == {"error": "rtvi_unavailable"}


def test_start_refuses_a_second_call_at_the_session_cap(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available(monkeypatch)
    app = make_app(**_TOKEN_OVERRIDE, max_concurrent_sessions=1)

    async def scenario() -> None:
        first = await _request(app, "POST", "/start", headers=_AUTH)
        assert first.status_code == 200
        second = await _request(app, "POST", "/start", headers=_AUTH)
        try:
            assert second.status_code == 503
            body = second.json()
            assert body["error"] == "session_limit_reached"
            assert body["spoken"] == "The relay is already on a call. Try again shortly."
        finally:
            await app.state.rtvi.close_all(reason="test_cleanup")

    asyncio.run(scenario())


def test_offer_requires_a_device_token(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available(monkeypatch)
    app = make_app(**_TOKEN_OVERRIDE)

    async def scenario() -> None:
        async with _started_session(app) as (session_id, _):
            response = await _request(
                app,
                "POST",
                f"/sessions/{session_id}/api/offer",
                json_body={"sdp": "v=0", "type": "offer"},
            )
            assert response.status_code == 401

    asyncio.run(scenario())


def test_offer_answers_the_sdp_for_an_admitted_session(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available(monkeypatch)
    calls: list[tuple[str, str, str]] = []

    async def _fake_offer(
        self: RtviSession,
        sdp: str,
        offer_type: str,
    ) -> dict[str, str]:
        calls.append((self.session_id, sdp, offer_type))
        return {
            "sdp": "v=0\r\no=answer",
            "type": "answer",
            "pc_id": self.session_id,
        }

    monkeypatch.setattr(RtviSession, "offer", _fake_offer)
    app = make_app(**_TOKEN_OVERRIDE)

    async def scenario() -> None:
        async with _started_session(app) as (session_id, _):
            response = await _request(
                app,
                "POST",
                f"/sessions/{session_id}/api/offer",
                headers=_AUTH,
                json_body={"sdp": "v=0\r\no=offer", "type": "offer"},
            )
            assert response.status_code == 200
            assert response.json() == {
                "sdp": "v=0\r\no=answer",
                "type": "answer",
                "pc_id": session_id,
            }
            assert calls == [(session_id, "v=0\r\no=offer", "offer")]

    asyncio.run(scenario())


def test_offer_rejects_malformed_bodies(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available(monkeypatch)
    app = make_app(**_TOKEN_OVERRIDE)

    async def scenario() -> None:
        async with _started_session(app) as (session_id, _):
            path = f"/sessions/{session_id}/api/offer"
            cases: list[tuple[bytes, str]] = [
                (b"not json", "invalid_json"),
                (b"[]", "invalid_json"),
                (b'{"type": "offer"}', "missing_sdp"),
                (b'{"sdp": "", "type": "offer"}', "missing_sdp"),
                (b'{"sdp": "v=0", "type": 5}', "invalid_type"),
            ]
            for payload, expected in cases:
                response = await _request(
                    app,
                    "POST",
                    path,
                    headers={**_AUTH, "Content-Type": "application/json"},
                    content=payload,
                )
                assert response.status_code == 400, payload
                assert response.json() == {"error": expected}

    asyncio.run(scenario())


def test_offer_returns_404_for_an_unknown_session(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available(monkeypatch)
    app = make_app(**_TOKEN_OVERRIDE)

    response = asyncio.run(
        _request(
            app,
            "POST",
            f"/sessions/{uuid.uuid4()}/api/offer",
            headers=_AUTH,
            json_body={"sdp": "v=0", "type": "offer"},
        )
    )

    assert response.status_code == 404
    assert response.json() == {"error": "unknown session"}


def test_offer_reports_an_aiortc_failure_as_500(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available(monkeypatch)

    async def _failing_offer(
        self: RtviSession,
        sdp: str,
        offer_type: str,
    ) -> dict[str, str]:
        del self, sdp, offer_type
        raise RuntimeError("sdp negotiation failed")

    monkeypatch.setattr(RtviSession, "offer", _failing_offer)
    app = make_app(**_TOKEN_OVERRIDE)

    async def scenario() -> None:
        async with _started_session(app) as (session_id, _):
            response = await _request(
                app,
                "POST",
                f"/sessions/{session_id}/api/offer",
                headers=_AUTH,
                json_body={"sdp": "v=0", "type": "offer"},
            )
            assert response.status_code == 500
            assert response.json() == {"error": "sdp negotiation failed"}

    asyncio.run(scenario())


def test_patch_accepts_trickle_candidates(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available(monkeypatch)
    seen: list[tuple[str, list[Any]]] = []

    async def _fake_add(self: RtviSession, candidates: list[Any]) -> None:
        seen.append((self.session_id, candidates))

    monkeypatch.setattr(RtviSession, "add_candidates", _fake_add)
    app = make_app(**_TOKEN_OVERRIDE)
    candidates = [
        {
            "candidate": "candidate:1 1 udp 2122260223 127.0.0.1 50000 typ host",
            "sdpMid": "0",
            "sdpMLineIndex": 0,
        },
        {
            "candidate": "candidate:1 1 udp 2122260223 127.0.0.1 50000 typ host",
            "sdpMid": "0",
            "sdpMLineIndex": 0,
        },
    ]

    async def scenario() -> None:
        async with _started_session(app) as (session_id, _):
            response = await _request(
                app,
                "PATCH",
                f"/sessions/{session_id}/api/offer",
                headers=_AUTH,
                json_body={"candidates": candidates},
            )
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            assert seen == [(session_id, candidates)]

            bad = await _request(
                app,
                "PATCH",
                f"/sessions/{session_id}/api/offer",
                headers=_AUTH,
                json_body={"candidates": "not-a-list"},
            )
            assert bad.status_code == 400
            assert bad.json() == {"error": "candidates must be a list"}

    asyncio.run(scenario())


def test_patch_returns_404_for_an_unknown_session(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available(monkeypatch)
    app = make_app(**_TOKEN_OVERRIDE)

    response = asyncio.run(
        _request(
            app,
            "PATCH",
            f"/sessions/{uuid.uuid4()}/api/offer",
            headers=_AUTH,
            json_body={"candidates": []},
        )
    )

    assert response.status_code == 404
    assert response.json() == {"error": "unknown session"}


def test_delete_closes_the_session_and_later_patch_is_unknown(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available(monkeypatch)
    app = make_app(**_TOKEN_OVERRIDE)

    async def scenario() -> None:
        response = await _request(app, "POST", "/start", headers=_AUTH)
        session_id = str(response.json()["sessionId"])
        manager = app.state.rtvi
        session = manager.get_session(session_id)
        assert session is not None
        lease_id = session.lease.session_id

        closed = await _request(app, "DELETE", f"/sessions/{session_id}")
        assert closed.status_code == 200
        assert closed.json() == {"status": "ok"}
        assert manager.get_session(session_id) is None
        assert session.lease.closed is True
        assert await app.state.limits.close_session(lease_id, reason="duplicate") is False

        later = await _request(
            app,
            "PATCH",
            f"/sessions/{session_id}/api/offer",
            headers=_AUTH,
            json_body={"candidates": []},
        )
        assert later.status_code == 404
        assert later.json() == {"error": "unknown session"}

        repeat = await _request(app, "DELETE", f"/sessions/{session_id}")
        assert repeat.status_code == 200
        assert repeat.json() == {"status": "ok"}

    asyncio.run(scenario())


def test_lease_stop_hook_closes_the_matching_session(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available(monkeypatch)
    app = make_app(**_TOKEN_OVERRIDE)

    async def scenario() -> None:
        response = await _request(app, "POST", "/start", headers=_AUTH)
        session_id = str(response.json()["sessionId"])
        manager = app.state.rtvi
        session = manager.get_session(session_id)
        assert session is not None

        await app.state.limits.close_session(
            session.lease.session_id,
            reason="idle_reaped",
        )

        assert session.closed is True
        assert manager.get_session(session_id) is None
        assert session.lease.closed is True

    asyncio.run(scenario())

"""Async HTTP/SSE client for the Hermes run API.

Threat model: the relay talks only to a configured loopback gateway and holds
one bearer token.  Request headers are built from validated configuration, the
token and session key never appear in exception messages, and every response
body is treated as untrusted until it matches the documented envelope.  The
client owns a single :class:`httpx.AsyncClient`; it starts no background
tasks and has no global state.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

from relay.config import Settings, get_settings
from relay.hermes.events import (
    EventGate,
    EventKind,
    HermesEventError,
    RunEvent,
    SseDecoder,
    decode_event,
)

_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
_MAX_ERROR_EXCERPT = 200
_MAX_SESSION_KEY_CHARS = 256
_RECONNECT_EXCEPTIONS = (
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    httpx.ConnectError,
    httpx.ConnectTimeout,
)
_TERMINAL_STATUS_KIND: Mapping[str, tuple[EventKind, str]] = {
    "completed": (EventKind.RUN_COMPLETED, "run.completed"),
    "cancelled": (EventKind.RUN_CANCELLED, "run.cancelled"),
    "failed": (EventKind.RUN_FAILED, "run.failed"),
}


class HermesError(RuntimeError):
    """Base class for Hermes adapter failures."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class HermesConfigError(HermesError):
    """The client configuration is unusable."""


class HermesAuthError(HermesError):
    """The gateway rejected the bearer token."""


class HermesNotFoundError(HermesError):
    """The requested run or endpoint does not exist."""


class HermesConflictError(HermesError):
    """The gateway refused the request because of a state conflict."""


class HermesRunGoneError(HermesNotFoundError):
    """The run event buffer was swept; use the status endpoint instead."""


class HermesUnavailableError(HermesError):
    """The gateway could not be reached."""


class HermesTimeoutError(HermesError):
    """The gateway did not answer inside the configured budget."""


class HermesProtocolError(HermesError):
    """The gateway returned a body outside the documented contract."""


class HermesStreamError(HermesError):
    """The stream ended without a terminal event after all reconnects."""


@dataclass(frozen=True)
class RunAdmission:
    """The 202 response for a submitted run."""

    run_id: str
    status: str
    replayed: bool
    session_key: str | None


@dataclass(frozen=True)
class RunStatus:
    """The status body for one run."""

    run_id: str
    status: str
    session_id: str | None
    execution_generation: int | None
    output: str
    pending_controls: tuple[Mapping[str, Any], ...]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class StopResult:
    """The immediate response to a stop request."""

    run_id: str
    status: str
    admission_id: str | None
    elapsed_s: float


def _secret_text(value: str | SecretStr | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value


def _validated_session_key(value: str | SecretStr | None) -> str | None:
    text = _secret_text(value)
    if text is None:
        return None
    if not 1 <= len(text) <= _MAX_SESSION_KEY_CHARS:
        msg = f"session key must be 1..{_MAX_SESSION_KEY_CHARS} characters"
        raise ValueError(msg)
    if any(not 0x20 <= ord(character) <= 0x7E for character in text):
        msg = "session key must contain only printable ASCII characters"
        raise ValueError(msg)
    return text


def _require_string(body: Mapping[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str):
        msg = f"response field {field!r} must be a string"
        raise HermesProtocolError(msg)
    return value


def _optional_string(body: Mapping[str, Any], field: str) -> str | None:
    value = body.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"response field {field!r} must be a string"
        raise HermesProtocolError(msg)
    return value


def _optional_generation(body: Mapping[str, Any], field: str) -> int | None:
    value = body.get(field)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"response field {field!r} must be an integer"
        raise HermesProtocolError(msg)
    return value


def _validated_input(value: object) -> str | list[dict[str, object]]:
    """Validate a run input and normalise a content array to plain objects.

    A string is passed through unchanged.  A sequence is validated part by part
    before any request is made: only ``text`` and ``image_url`` parts with the
    documented value shapes are accepted, while unknown extra keys survive so
    the gateway can ignore them.  Errors name the offending part index rather
    than echoing payload bytes.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        msg = "input must be a string or a non-empty sequence of content parts"
        raise HermesConfigError(msg)
    if not isinstance(value, Sequence):
        msg = "input must be a string or a non-empty sequence of content parts"
        raise HermesConfigError(msg)
    if not value:
        msg = "input content array must not be empty"
        raise HermesConfigError(msg)

    parts: list[dict[str, object]] = []
    for index, part in enumerate(value):
        if not isinstance(part, Mapping):
            msg = f"input part {index} must be an object"
            raise HermesConfigError(msg)
        kind = part.get("type")
        if kind == "text":
            if not isinstance(part.get("text"), str):
                msg = f"input part {index} of type 'text' must have a string 'text'"
                raise HermesConfigError(msg)
            parts.append(dict(part))
        elif kind == "image_url":
            image = part.get("image_url")
            if not isinstance(image, Mapping):
                msg = f"input part {index} of type 'image_url' must have an 'image_url' object"
                raise HermesConfigError(msg)
            if not isinstance(image.get("url"), str):
                msg = f"input part {index} of type 'image_url' must have a string 'url'"
                raise HermesConfigError(msg)
            normalized = dict(part)
            normalized["image_url"] = dict(image)
            parts.append(normalized)
        else:
            msg = f"input part {index} has an unsupported 'type'"
            raise HermesConfigError(msg)
    return parts


class HermesClient:
    """Drive one Hermes turn at a time against a loopback gateway."""

    def __init__(
        self,
        base_url: str,
        api_key: str | SecretStr,
        *,
        session_key: str | SecretStr | None = None,
        timeout_s: float = 30.0,
        connect_timeout_s: float = 5.0,
        stop_timeout_s: float = 2.0,
        stream_timeout_s: float = 60.0,
        max_reconnects: int = 3,
        reconnect_backoff_s: float = 0.05,
        max_reconnect_backoff_s: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES or not parsed.hostname:
            msg = "hermes base URL must be an http(s) URL with a host"
            raise HermesConfigError(msg)

        key = _secret_text(api_key)
        if not key:
            msg = "hermes API key is not configured"
            raise HermesConfigError(msg)

        for name, value in (
            ("timeout_s", timeout_s),
            ("connect_timeout_s", connect_timeout_s),
            ("stop_timeout_s", stop_timeout_s),
            ("stream_timeout_s", stream_timeout_s),
        ):
            if not math.isfinite(value) or value <= 0:
                msg = f"{name} must be finite and greater than zero"
                raise HermesConfigError(msg)
        if max_reconnects < 0:
            msg = "max_reconnects must be zero or greater"
            raise HermesConfigError(msg)
        if not math.isfinite(reconnect_backoff_s) or reconnect_backoff_s < 0:
            msg = "reconnect_backoff_s must be finite and non-negative"
            raise HermesConfigError(msg)
        if not math.isfinite(max_reconnect_backoff_s) or max_reconnect_backoff_s < 0:
            msg = "max_reconnect_backoff_s must be finite and non-negative"
            raise HermesConfigError(msg)

        self._base_url = base_url.rstrip("/")
        self._api_key = key
        self._session_key = _validated_session_key(session_key)
        self._stop_timeout_s = stop_timeout_s
        self._stream_timeout_s = stream_timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._max_reconnects = max_reconnects
        self._reconnect_backoff_s = reconnect_backoff_s
        self._max_reconnect_backoff_s = max_reconnect_backoff_s
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **overrides: Any) -> HermesClient:
        """Build a client from relay settings without issuing a request."""
        if settings is None:
            settings = get_settings()
        base_url = getattr(settings, "hermes_base_url", None) or ""
        api_key = getattr(settings, "hermes_api_key", None)
        session_key = getattr(settings, "hermes_session_key", None)
        return cls(
            str(base_url),
            api_key,
            session_key=session_key,
            **overrides,
        )

    async def aclose(self) -> None:
        """Close the owned HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> HermesClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def submit_run(
        self,
        input: str | Sequence[Mapping[str, object]],
        *,
        session_id: str | None = None,
        session_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunAdmission:
        """Submit one turn and return the gateway admission."""
        validated_input = _validated_input(input)
        effective_session_key = _validated_session_key(
            session_key if session_key is not None else self._session_key
        )
        body: dict[str, Any] = {"input": validated_input}
        if session_id is not None:
            body["session_id"] = session_id

        response = await self._request(
            "POST",
            "/v1/runs",
            json=body,
            headers=self._headers(
                session_key=effective_session_key,
                idempotency_key=idempotency_key,
            ),
        )
        data = self._json_object(response)
        return RunAdmission(
            run_id=_require_string(data, "run_id"),
            status=_require_string(data, "status"),
            replayed=self._replayed(data),
            session_key=response.headers.get("X-Hermes-Session-Key"),
        )

    async def get_run(self, run_id: str) -> RunStatus:
        """Fetch the current status of one run."""
        response = await self._request(
            "GET",
            f"/v1/runs/{quote(run_id, safe='')}",
            headers=self._headers(),
        )
        data = self._json_object(response)
        controls = data.get("pending_controls", [])
        if not isinstance(controls, list) or any(
            not isinstance(control, Mapping) for control in controls
        ):
            msg = "response field 'pending_controls' must be a list of objects"
            raise HermesProtocolError(msg)
        output = data.get("output", "")
        if not isinstance(output, str):
            msg = "response field 'output' must be a string"
            raise HermesProtocolError(msg)
        return RunStatus(
            run_id=_require_string(data, "run_id"),
            status=_require_string(data, "status"),
            session_id=_optional_string(data, "session_id"),
            execution_generation=_optional_generation(data, "execution_generation"),
            output=output,
            pending_controls=tuple(controls),
            raw=data,
        )

    async def stop(self, run_id: str) -> StopResult:
        """Interrupt a run and return the gateway's immediate answer."""
        started = time.monotonic()
        response = await self._request(
            "POST",
            f"/v1/runs/{quote(run_id, safe='')}/stop",
            json={},
            headers=self._headers(),
            request_timeout=self._stop_timeout_s,
        )
        elapsed_s = time.monotonic() - started
        data = self._json_object(response)
        return StopResult(
            run_id=_require_string(data, "run_id"),
            status=_require_string(data, "status"),
            admission_id=_optional_string(data, "admission_id"),
            elapsed_s=elapsed_s,
        )

    async def events(
        self,
        run_id: str,
        *,
        last_seq: int | None = None,
        reconnect: bool = True,
    ) -> AsyncIterator[RunEvent]:
        """Stream normalized events for one run until its terminal event."""
        gate = EventGate(run_id, start_after_seq=last_seq)
        path = f"/v1/runs/{quote(run_id, safe='')}/events"
        attempts = 0
        last_error: Exception | None = None

        while True:
            headers = self._headers()
            headers["Accept"] = "text/event-stream"
            params: dict[str, str] = {}
            if gate.last_seq is not None:
                # The query parameter is the documented way to ask for an
                # initial resume point.  Reconnects use the standard SSE
                # Last-Event-ID header so the gateway treats them as a
                # resumed subscription rather than a fresh replay request.
                if attempts == 0 and last_seq is not None:
                    params["last_seq"] = str(gate.last_seq)
                else:
                    headers["Last-Event-ID"] = str(gate.last_seq)

            try:
                async with self._client.stream(
                    "GET",
                    path,
                    params=params or None,
                    headers=headers,
                    timeout=httpx.Timeout(self._stream_timeout_s, connect=self._connect_timeout_s),
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        self._raise_stream_status(response.status_code, body)

                    decoder = SseDecoder()
                    async for line in response.aiter_lines():
                        frame = decoder.feed(line)
                        if frame is None:
                            continue
                        event = self._decode_frame(frame)
                        if event is None:
                            continue
                        if self._observe(gate, event):
                            yield event
                            if event.terminal:
                                return

                    frame = decoder.flush()
                    if frame is not None:
                        event = self._decode_frame(frame)
                        if event is not None and self._observe(gate, event):
                            yield event
                            if event.terminal:
                                return
            except _RECONNECT_EXCEPTIONS as exc:
                last_error = exc

            if not reconnect or attempts >= self._max_reconnects:
                break
            attempts += 1
            delay = min(
                self._reconnect_backoff_s * (2 ** (attempts - 1)),
                self._max_reconnect_backoff_s,
            )
            await asyncio.sleep(delay)

        status = await self.get_run(run_id)
        terminal = _TERMINAL_STATUS_KIND.get(status.status)
        if terminal is None:
            detail = type(last_error).__name__ if last_error is not None else "stream ended"
            msg = f"run stream ended without a terminal event ({detail})"
            raise HermesStreamError(msg)

        event = self._synthetic_terminal(gate, status, terminal)
        if self._observe(gate, event):
            yield event

    @staticmethod
    def _observe(gate: EventGate, event: RunEvent) -> bool:
        try:
            return gate.observe(event)
        except HermesEventError as exc:
            msg = f"run stream contract violation: {exc}"
            raise HermesProtocolError(msg) from exc

    def _headers(
        self,
        *,
        session_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        effective_session_key = session_key if session_key is not None else self._session_key
        if effective_session_key is not None:
            headers["X-Hermes-Session-Key"] = effective_session_key
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        request_timeout: float | httpx.Timeout | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                path,
                json=json,
                headers=headers,
                timeout=httpx.USE_CLIENT_DEFAULT if request_timeout is None else request_timeout,
            )
        except httpx.TimeoutException as exc:
            msg = "hermes request timed out"
            raise HermesTimeoutError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"hermes transport failure: {type(exc).__name__}"
            raise HermesUnavailableError(msg) from exc

        if response.status_code >= 400:
            self._raise_status(response.status_code, response.content)
        return response

    def _raise_stream_status(self, status_code: int, body: bytes) -> None:
        message, code = self._parse_error_body(status_code, body)
        if status_code == 404 and code == "run_not_found":
            raise HermesRunGoneError(message, code=code)
        self._raise_typed(status_code, message, code)

    def _raise_status(self, status_code: int, body: bytes) -> None:
        message, code = self._parse_error_body(status_code, body)
        self._raise_typed(status_code, message, code)

    @staticmethod
    def _raise_typed(status_code: int, message: str, code: str | None) -> None:
        if status_code == 401:
            raise HermesAuthError(message, code=code)
        if status_code == 404:
            raise HermesNotFoundError(message, code=code)
        if status_code == 409:
            raise HermesConflictError(message, code=code)
        raise HermesError(message, code=code)

    @staticmethod
    def _parse_error_body(status_code: int, body: bytes) -> tuple[str, str | None]:
        text = body.decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            excerpt = text[:_MAX_ERROR_EXCERPT]
            msg = f"non-JSON error response for HTTP {status_code}: {excerpt!r}"
            raise HermesProtocolError(msg) from exc

        if not isinstance(parsed, dict):
            msg = f"error response for HTTP {status_code} must be a JSON object"
            raise HermesProtocolError(msg)
        envelope = parsed.get("error")
        if not isinstance(envelope, dict):
            msg = f"error response for HTTP {status_code} has no 'error' object"
            raise HermesProtocolError(msg)

        message = envelope.get("message")
        code = envelope.get("code")
        if not isinstance(message, str):
            msg = f"error response for HTTP {status_code} has no string 'message'"
            raise HermesProtocolError(msg)
        if code is not None and not isinstance(code, str):
            msg = f"error response for HTTP {status_code} has a non-string 'code'"
            raise HermesProtocolError(msg)
        return message, code

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except ValueError as exc:
            excerpt = response.text[:_MAX_ERROR_EXCERPT]
            msg = f"response body is not JSON: {excerpt!r}"
            raise HermesProtocolError(msg) from exc
        if not isinstance(parsed, dict):
            msg = "response body must be a JSON object"
            raise HermesProtocolError(msg)
        return parsed

    @staticmethod
    def _replayed(body: Mapping[str, Any]) -> bool:
        value = body.get("replayed", False)
        if not isinstance(value, bool):
            msg = "response field 'replayed' must be a boolean"
            raise HermesProtocolError(msg)
        return value

    def _decode_frame(self, frame: Any) -> RunEvent | None:
        if frame.kind != "data":
            return None
        try:
            return decode_event(frame)
        except HermesEventError as exc:
            msg = f"malformed Hermes event frame: {exc}"
            raise HermesProtocolError(msg) from exc

    @staticmethod
    def _synthetic_terminal(
        gate: EventGate,
        status: RunStatus,
        terminal: tuple[EventKind, str],
    ) -> RunEvent:
        kind, raw_name = terminal
        payload: dict[str, Any] = dict(status.raw)
        payload["output"] = status.output
        if kind is EventKind.RUN_COMPLETED:
            payload.setdefault("completed", True)
            payload.setdefault("partial", False)
            payload.setdefault("interrupted", False)
        elif kind is EventKind.RUN_CANCELLED:
            payload.setdefault("completed", False)
            payload.setdefault("partial", False)
            payload.setdefault("interrupted", True)
            payload.setdefault("turn_exit_reason", "interrupted_by_user")
        else:
            payload.setdefault("completed", False)
            payload.setdefault("partial", False)
            payload.setdefault("interrupted", False)
            payload.setdefault("error", status.output or "run failed")

        seq = 0 if gate.last_seq is None else gate.last_seq + 1
        return RunEvent(
            kind=kind,
            raw_name=raw_name,
            seq=seq,
            run_id=status.run_id,
            timestamp=time.time(),
            payload=payload,
        )

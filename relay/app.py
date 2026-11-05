"""FastAPI application shell for the Hermes Talk voice relay.

This module deliberately contains no business logic: it boots, exposes
liveness/readiness probes, emits structured logs with correlation ids, and
shuts down gracefully.  Nothing here makes network calls.

Admission control is wired here so every request passes through the limits
middleware: ``/v1/runs``, ``/v1/attachments`` and ``/v1/artifacts`` inherit
per-device rate limits, and session, run, and upload capacity leases come from
``app.state.limits``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from relay import __version__
from relay.api.attach import router as attach_router
from relay.api.files import router as files_router
from relay.auth.deps import _DeviceAuthError, require_device_token
from relay.auth.tokens import (
    DeviceToken,
    TokenStateError,
    TokenStore,
    extract_bearer_token,
)
from relay.config import Settings, get_settings
from relay.limits import AdmissionController, LimitError, build_limits
from relay.logging import (
    audit,
    configure_audit,
    configure_logging,
    log_request,
    reset_audit,
    reset_correlation_id,
    set_correlation_id,
)
from relay.storage.inbox import InboxConfigError, InboxError, InboxStore
from relay.storage.outbox import OutboxConfigError, OutboxError, OutboxStore
from relay.transport.rtvi import RtviSessionManager
from relay.transport.rtvi import router as rtvi_router

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SERVICE_NAME = "voice-relay"
_logger = logging.getLogger(__name__)


def _bootstrap_tokens(settings: Settings) -> tuple[str, ...]:
    try:
        return tuple(token.get_secret_value() for token in settings.device_tokens)
    except ValueError:
        return ()


def _display_path(path: Path, data_dir: Path | None) -> str:
    """Render a storage path relative to DATA_DIR when possible."""
    if data_dir is not None:
        try:
            return str(path.relative_to(data_dir))
        except ValueError:
            pass
    return str(path)


def _settings_check(settings: Settings, store: TokenStore) -> dict[str, Any]:
    missing = settings.missing_required_settings()
    if store.has_configured_tokens():
        missing = [name for name in missing if name != "device_tokens"]
    if missing:
        return {
            "name": "settings",
            "ok": False,
            "detail": f"missing required settings: {', '.join(missing)}",
        }
    return {
        "name": "settings",
        "ok": True,
        "detail": "all required settings present",
    }


def _storage_check(settings: Settings) -> dict[str, Any]:
    paths = settings.storage_paths()
    if not paths:
        return {
            "name": "storage",
            "ok": True,
            "detail": "no storage directories configured",
        }

    failures: list[str] = []
    checked: list[str] = []
    for path in paths:
        display = _display_path(path, settings.data_dir)
        checked.append(display)
        if not path.exists():
            failures.append(f"{display}: missing")
        elif not path.is_dir():
            failures.append(f"{display}: not a directory")
        elif not os.access(path, os.W_OK):
            failures.append(f"{display}: not writable")

    if failures:
        return {"name": "storage", "ok": False, "detail": "; ".join(failures)}
    return {
        "name": "storage",
        "ok": True,
        "detail": f"writable: {', '.join(checked)}",
    }


def _auth_check(store: TokenStore) -> dict[str, Any]:
    active_count = len(store.active_tokens())
    bootstrap_count = store.bootstrap_token_count
    if active_count == 0 and bootstrap_count == 0:
        return {
            "name": "auth",
            "ok": False,
            "detail": "no device tokens configured",
        }
    if active_count == 0:
        return {
            "name": "auth",
            "ok": True,
            "detail": "0 active device token(s); bootstrap fallback active",
        }
    return {
        "name": "auth",
        "ok": True,
        "detail": f"{active_count} active device token(s)",
    }


def _models_check(settings: Settings) -> dict[str, Any]:
    model_names = {
        "voice_brain": settings.voice_brain_model,
        "stt": settings.stt_model,
        "tts": settings.tts_model,
        "vad": settings.vad_model,
    }
    missing = sorted(name for name, value in model_names.items() if not value.strip())
    if missing:
        return {
            "name": "models",
            "ok": False,
            "detail": f"missing model names: {', '.join(missing)}",
        }
    return {
        "name": "models",
        "ok": True,
        "detail": "all model names configured",
    }


def _limits_check(limits: AdmissionController) -> dict[str, Any]:
    snapshot = limits.snapshot()
    sessions = snapshot["sessions"]
    runs = snapshot["runs"]
    uploads = snapshot["uploads"]
    detail = (
        f"sessions {sessions['active']}/{sessions['max']}; "
        f"runs {runs['active']}/{runs['max']}; "
        f"uploads {uploads['in_flight']} in flight, "
        f"{uploads['queued']}/{uploads['max_queued']} queued"
    )
    return {"name": "limits", "ok": True, "detail": detail}


def _rtvi_check(rtvi: RtviSessionManager) -> dict[str, Any]:
    available, detail = rtvi.availability()
    return {"name": "rtvi", "ok": available, "detail": detail}


def _readiness_checks(
    settings: Settings,
    store: TokenStore,
    shutting_down: bool,
    limits: AdmissionController | None = None,
    rtvi: RtviSessionManager | None = None,
) -> list[dict[str, Any]]:
    shutdown_check: dict[str, Any] = {
        "name": "shutdown",
        "ok": not shutting_down,
        "detail": "shutdown in progress" if shutting_down else "shutdown not in progress",
    }
    checks = [
        _settings_check(settings, store),
        _storage_check(settings),
        _auth_check(store),
        _models_check(settings),
        shutdown_check,
    ]
    if limits is not None:
        checks.append(_limits_check(limits))
    if rtvi is not None:
        checks.append(_rtvi_check(rtvi))
    return checks


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start up and shut down the relay without touching the network."""
    # The relay's own relay.access middleware records method, path and status
    # only, so query strings never reach logs; disable uvicorn's duplicate
    # access log, which would otherwise log the raw request line.
    logging.getLogger("uvicorn.access").disabled = True
    settings: Settings = app.state.settings
    configure_logging(settings)
    configure_audit(settings)
    app.state.shutting_down = False
    _logger.info(
        "relay.startup",
        extra={
            "service_version": __version__,
            "host": settings.relay_host,
            "port": settings.relay_port,
            "log_format": settings.log_format,
        },
    )
    store: TokenStore = app.state.token_store
    if store.state_path_inside_git_work_tree():
        _logger.warning(
            "auth.state_path_inside_repo",
            extra={
                "event": "auth.state_path_inside_repo",
                "state_path": store.state_path_display(),
            },
        )
    active_tokens = store.active_tokens()
    _logger.info(
        "auth.store_loaded",
        extra={
            "event": "auth.store_loaded",
            "active_tokens": len(active_tokens),
            "bootstrap_fallback_active": len(active_tokens) == 0
            and store.bootstrap_token_count > 0,
            "state_path": store.state_path_display(),
        },
    )
    audit("service.start", version=__version__)
    limits: AdmissionController = app.state.limits
    reaper_interval = settings.session_reaper_interval_seconds

    async def _reaper_loop() -> None:
        while True:
            try:
                closed = await limits.reap_idle_sessions()
                for session_id in closed:
                    _logger.info(
                        "limits.session_reaped",
                        extra={
                            "event": "limits.session_reaped",
                            "session_id": session_id,
                        },
                    )
                await asyncio.sleep(reaper_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "limits.reaper_error",
                    extra={"event": "limits.reaper_error"},
                )
                await asyncio.sleep(reaper_interval)

    reaper_task: asyncio.Task[None] | None = asyncio.create_task(_reaper_loop())
    try:
        yield
    finally:
        app.state.shutting_down = True
        if reaper_task is not None:
            reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await reaper_task
            reaper_task = None
        rtvi: RtviSessionManager = app.state.rtvi
        await rtvi.close_all(reason="shutdown")
        closed_sessions = await limits.close_all(reason="shutdown")
        for session_id in closed_sessions:
            _logger.info(
                "limits.session_closed",
                extra={
                    "event": "limits.session_closed",
                    "session_id": session_id,
                    "reason": "shutdown",
                },
            )
        _logger.info(
            "relay.shutdown.begin",
            extra={"timeout_seconds": settings.shutdown_timeout_seconds},
        )
        timeout = max(settings.shutdown_timeout_seconds, 0.0)
        if timeout > 0:
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.sleep(min(0.05, timeout)),
                    timeout=timeout,
                )
        _logger.info("relay.shutdown.complete")
        audit("service.stop", version=__version__)
        reset_audit()
        app.state.shutting_down = False


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the relay application shell."""
    resolved_settings = settings if settings is not None else get_settings()
    app = FastAPI(
        title="Hermes Talk voice relay",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.shutting_down = False
    state_path = resolved_settings.device_token_state_path()
    bootstrap_tokens = _bootstrap_tokens(resolved_settings)
    try:
        token_store = TokenStore(state_path, bootstrap_tokens=bootstrap_tokens)
    except TokenStateError:
        token_store = TokenStore(
            state_path,
            bootstrap_tokens=bootstrap_tokens,
            allow_git_work_tree=True,
        )
    app.state.token_store = token_store
    app.state.limits = build_limits(resolved_settings)
    app.state.rtvi = RtviSessionManager(app, resolved_settings, app.state.limits)
    try:
        app.state.inbox = InboxStore(resolved_settings)
    except InboxConfigError:
        # Keep the shell bootable and /health at 200; uploads return a spoken
        # 503 until INBOX_DIR points at a directory outside the repository.
        app.state.inbox = None
    try:
        app.state.outbox = OutboxStore(resolved_settings)
    except OutboxConfigError:
        # Keep the shell bootable and /health at 200; artifact requests return a
        # spoken 503 until OUTBOX_DIR points at a directory outside the repository.
        app.state.outbox = None

    @app.middleware("http")
    async def admission_control_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        limits: AdmissionController = request.app.state.limits
        correlation_id = getattr(request.state, "correlation_id", None)
        category = limits.category_for(request.method, request.url.path)
        if category is not None:
            device = getattr(request.state, "device_token", None)
            if device is None:
                token = extract_bearer_token(request.headers.get("Authorization"))
                if token is not None:
                    client_host = request.client.host if request.client is not None else None
                    device = request.app.state.token_store.verify(
                        token,
                        client_host=client_host,
                        path=request.url.path,
                    )
                    if device is not None:
                        request.state.device_token = device
            if device is not None:
                try:
                    limits.check_rate_limit(device.token_id, category)
                except LimitError as exc:
                    return JSONResponse(
                        exc.to_body(correlation_id),
                        status_code=exc.status_code,
                        headers=exc.headers(),
                    )
        try:
            return await call_next(request)
        except LimitError as exc:
            return JSONResponse(
                exc.to_body(correlation_id),
                status_code=exc.status_code,
                headers=exc.headers(),
            )

    @app.middleware("http")
    async def correlation_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        supplied = request.headers.get("X-Correlation-ID", "")
        correlation_id = supplied if _CORRELATION_ID_RE.fullmatch(supplied) else uuid.uuid4().hex

        request.state.correlation_id = correlation_id
        token = set_correlation_id(correlation_id)
        started = time.perf_counter()
        client_host = request.client.host if request.client is not None else None
        try:
            response = await call_next(request)
            duration_ms = (time.perf_counter() - started) * 1000
            response.headers["X-Correlation-ID"] = correlation_id
            log_request(
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                client_host,
            )
            return response
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            log_request(request.method, request.url.path, 500, duration_ms, client_host)
            raise
        finally:
            reset_correlation_id(token)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", None)
        if not isinstance(correlation_id, str) or not correlation_id:
            correlation_id = uuid.uuid4().hex
        token = set_correlation_id(correlation_id)
        try:
            _logger.error(
                "relay.unhandled_exception",
                exc_info=True,
                extra={
                    "correlation_id": correlation_id,
                    "exception_type": type(exc).__name__,
                },
            )
        finally:
            reset_correlation_id(token)
        return JSONResponse(
            {"error": "internal_error", "correlation_id": correlation_id},
            status_code=500,
            headers={"X-Correlation-ID": correlation_id},
        )

    @app.exception_handler(_DeviceAuthError)
    async def device_auth_error_handler(
        request: Request,
        exc: _DeviceAuthError,
    ) -> JSONResponse:
        return JSONResponse(
            exc.detail,
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(LimitError)
    async def limit_error_handler(
        request: Request,
        exc: LimitError,
    ) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", None)
        if not isinstance(correlation_id, str) or not correlation_id:
            correlation_id = uuid.uuid4().hex
        return JSONResponse(
            exc.to_body(correlation_id),
            status_code=exc.status_code,
            headers=exc.headers(),
        )

    @app.exception_handler(InboxError)
    async def inbox_error_handler(
        request: Request,
        exc: InboxError,
    ) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", None)
        if not isinstance(correlation_id, str) or not correlation_id:
            correlation_id = uuid.uuid4().hex
        return JSONResponse(
            exc.to_body(correlation_id),
            status_code=exc.status_code,
            headers=exc.headers(),
        )

    @app.exception_handler(OutboxError)
    async def outbox_error_handler(
        request: Request,
        exc: OutboxError,
    ) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", None)
        if not isinstance(correlation_id, str) or not correlation_id:
            correlation_id = uuid.uuid4().hex
        return JSONResponse(
            exc.to_body(correlation_id),
            status_code=exc.status_code,
            headers=exc.headers(),
        )

    app.include_router(attach_router)
    app.include_router(files_router)
    app.include_router(rtvi_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "service": _SERVICE_NAME,
            "version": __version__,
            "docs": "/docs",
        }

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": _SERVICE_NAME,
            "version": __version__,
        }

    @app.get("/v1/whoami")
    async def whoami(
        device: Annotated[DeviceToken, Depends(require_device_token)],
    ) -> dict[str, str]:
        return {
            "label": device.label,
            "token_id": device.token_id,
            "issued_at": device.issued_at,
        }

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        checks = _readiness_checks(
            resolved_settings,
            request.app.state.token_store,
            bool(request.app.state.shutting_down),
            request.app.state.limits,
            request.app.state.rtvi,
        )
        all_ok = all(check["ok"] for check in checks)
        payload = {
            "status": "ready" if all_ok else "not_ready",
            "checks": checks,
        }
        return JSONResponse(payload, status_code=200 if all_ok else 503)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    runtime_settings = get_settings()
    uvicorn.run(
        app,
        host=runtime_settings.relay_host,
        port=runtime_settings.relay_port,
    )

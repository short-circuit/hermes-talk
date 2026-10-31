"""FastAPI application shell for the Hermes Talk voice relay.

This module deliberately contains no business logic: it boots, exposes
liveness/readiness probes, emits structured logs with correlation ids, and
shuts down gracefully.  Nothing here makes network calls.
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
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from relay import __version__
from relay.config import Settings, get_settings
from relay.logging import (
    configure_logging,
    log_request,
    reset_correlation_id,
    set_correlation_id,
)

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SERVICE_NAME = "voice-relay"
_logger = logging.getLogger(__name__)


def _display_path(path: Path, data_dir: Path | None) -> str:
    """Render a storage path relative to DATA_DIR when possible."""
    if data_dir is not None:
        try:
            return str(path.relative_to(data_dir))
        except ValueError:
            pass
    return str(path)


def _settings_check(settings: Settings) -> dict[str, Any]:
    missing = settings.missing_required_settings()
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


def _auth_check(settings: Settings) -> dict[str, Any]:
    try:
        count = settings.configured_device_token_count
    except ValueError:
        count = 0
    if count == 0:
        return {
            "name": "auth",
            "ok": False,
            "detail": "no device tokens configured",
        }
    return {
        "name": "auth",
        "ok": True,
        "detail": f"{count} device token(s) configured",
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


def _readiness_checks(settings: Settings, shutting_down: bool) -> list[dict[str, Any]]:
    shutdown_check: dict[str, Any] = {
        "name": "shutdown",
        "ok": not shutting_down,
        "detail": "shutdown in progress" if shutting_down else "shutdown not in progress",
    }
    return [
        _settings_check(settings),
        _storage_check(settings),
        _auth_check(settings),
        _models_check(settings),
        shutdown_check,
    ]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start up and shut down the relay without touching the network."""
    settings: Settings = app.state.settings
    configure_logging(settings)
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
    try:
        yield
    finally:
        app.state.shutting_down = True
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

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        checks = _readiness_checks(
            resolved_settings,
            bool(request.app.state.shutting_down),
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

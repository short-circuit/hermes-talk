"""FastAPI dependencies for device-token authentication.

The dependency lives here so feature routers (for example
``relay.api.attach``) can require a device token without importing
``relay.app`` and creating an import cycle.  ``relay.app`` re-exports the
same objects, so ``from relay.app import require_device_token`` keeps
working for existing callers and tests.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from relay.auth.tokens import (
    DeviceToken,
    TokenStore,
    extract_bearer_token,
    log_auth_failure,
)


class _DeviceAuthError(HTTPException):
    """A 401 that renders the coarse auth failure body directly."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            status_code=401,
            detail={"error": "unauthorized", "reason": reason},
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_device_token(request: Request) -> DeviceToken:
    """Authenticate a request from the Authorization header only."""
    existing = getattr(request.state, "device_token", None)
    if existing is not None:
        return existing
    store: TokenStore = request.app.state.token_store
    authorization = request.headers.get("Authorization")
    client_host = request.client.host if request.client is not None else None
    path = request.url.path
    if authorization is None:
        log_auth_failure("missing_header", client_host=client_host, path=path)
        raise _DeviceAuthError("missing_credentials")
    token = extract_bearer_token(authorization)
    if token is None:
        log_auth_failure("invalid_scheme", client_host=client_host, path=path)
        raise _DeviceAuthError("invalid_scheme")
    record = store.verify(token, client_host=client_host, path=path)
    if record is None:
        reason = "no_tokens_configured" if not store.has_configured_tokens() else "invalid_token"
        raise _DeviceAuthError(reason)
    return record


__all__ = ["_DeviceAuthError", "require_device_token"]

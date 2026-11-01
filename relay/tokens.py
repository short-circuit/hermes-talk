"""Shim so ``python -m relay.tokens`` reaches the token administration CLI."""

from __future__ import annotations

from relay.auth.tokens import (
    DEFAULT_STATE_FILENAME,
    STATE_VERSION,
    TOKEN_BYTES,
    TOKEN_ID_LENGTH,
    TOKEN_PREFIX,
    DeviceToken,
    TokenError,
    TokenStateError,
    TokenStore,
    UnknownDeviceError,
    extract_bearer_token,
    generate_token,
    hash_token,
    is_well_formed,
    log_auth_failure,
    main,
    state_path_is_inside_git_work_tree,
    token_id_for,
)

__all__ = [
    "DEFAULT_STATE_FILENAME",
    "STATE_VERSION",
    "TOKEN_BYTES",
    "TOKEN_ID_LENGTH",
    "TOKEN_PREFIX",
    "DeviceToken",
    "TokenError",
    "TokenStateError",
    "TokenStore",
    "UnknownDeviceError",
    "extract_bearer_token",
    "generate_token",
    "hash_token",
    "is_well_formed",
    "log_auth_failure",
    "main",
    "state_path_is_inside_git_work_tree",
    "token_id_for",
]

if __name__ == "__main__":
    raise SystemExit(main())

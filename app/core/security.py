"""API-key authentication.

Deliberately simple: a shared key supplied via the ``X-API-Key`` header or as a
``Bearer`` token. Compared with :func:`hmac.compare_digest` so the check is
constant time. A real multi-tenant deployment would swap this dependency for
OIDC/JWT — the rest of the app would not change, because routers only depend on
``require_api_key``, never on the mechanism behind it.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer = HTTPBearer(auto_error=False)


def _matches(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(candidate.encode(), expected.encode())


def require_api_key(
    x_api_key: str | None = Security(_api_key_header),
    bearer: HTTPAuthorizationCredentials | None = Security(_bearer),
    settings: Settings = Depends(get_settings),
) -> str:
    """FastAPI dependency enforcing authentication. Returns the principal name."""
    if not settings.auth_enabled:
        return "anonymous"

    candidate = x_api_key or (bearer.credentials if bearer else None)
    if not candidate:
        raise AuthenticationError(
            "Missing API credentials. Send an X-API-Key header or a Bearer token."
        )
    if not _matches(candidate, settings.api_key):
        raise AuthenticationError("Invalid API credentials.")
    return "api-client"

"""Service to service authentication with API keys in the ``X-API-Key`` header."""

from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, Request, status

from v2e_rag.config import Settings
from v2e_rag.observability.logging import get_logger

log = get_logger(__name__)


def get_app_settings(request: Request) -> Settings:
    return request.app.state.container.settings


def _matches(candidate: str | None, keys: frozenset[str]) -> bool:
    if not candidate:
        return False
    return any(hmac.compare_digest(candidate.encode(), key.encode()) for key in keys)


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_app_settings),
) -> None:
    keys = settings.api_key_set | settings.admin_api_key_set
    if not keys:
        if settings.environment in ("development", "test"):
            return
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API keys are not configured")
    if not _matches(x_api_key, keys):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or missing X-API-Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


async def require_admin_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_app_settings),
) -> None:
    keys = settings.admin_api_key_set
    if not keys:
        if settings.environment in ("development", "test"):
            return
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin API keys are not configured")
    if not _matches(x_api_key, keys):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin privileges required")

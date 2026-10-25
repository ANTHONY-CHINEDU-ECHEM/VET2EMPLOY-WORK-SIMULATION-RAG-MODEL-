"""Service to service authentication with API keys in the ``X-API-Key`` header.

Keys are compared in constant time against every configured key (no early exit), so
response timing reveals neither whether a key matched nor which one. When no keys are
configured, development and test environments allow access; every other environment
fails closed with ``503`` so a misconfigured deployment is never silently open.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, Request, status

from v2e_rag.config import Settings
from v2e_rag.observability.logging import get_logger

log = get_logger(__name__)


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.container.settings
    return settings


def key_matches(candidate: str | None, keys: frozenset[str]) -> bool:
    if not candidate:
        return False
    encoded = candidate.encode("utf-8")
    matched = False
    for key in keys:
        matched |= hmac.compare_digest(encoded, key.encode("utf-8"))
    return matched


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_app_settings),
) -> None:
    keys = settings.api_key_set | settings.admin_api_key_set
    if not keys:
        if settings.is_local:
            return
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API keys are not configured")
    if not key_matches(x_api_key, keys):
        log.info("authentication_failed", key_present=bool(x_api_key))
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
        if settings.is_local:
            return
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Admin API keys are not configured"
        )
    if not x_api_key:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing X-API-Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if not key_matches(x_api_key, keys):
        log.info("admin_authentication_failed")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin privileges required")


async def require_metrics_token(request: Request) -> None:
    """Optional bearer token for ``/metrics`` (``V2E_METRICS_TOKEN``)."""
    settings: Settings = request.app.state.container.settings
    expected = settings.metrics_token.get_secret_value()
    if not expected:
        return
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Metrics require a bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

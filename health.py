"""Liveness, readiness and metrics.

Readiness runs aggregate queries, so its result is cached for
``V2E_READINESS_CACHE_SECONDS``: probes every few seconds from several replicas would
otherwise add constant load to the database for no benefit.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from v2e_rag import __version__
from v2e_rag.api.schemas import ReadinessResponse
from v2e_rag.api.security import require_metrics_token
from v2e_rag.llm.prompts import PROMPT_VERSION

router = APIRouter(tags=["Operations"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "prompt_version": PROMPT_VERSION}


async def _readiness(request: Request) -> tuple[bool, dict[str, Any]]:
    container = request.app.state.container
    store = await container.store.health()
    stats = await container.store.stats() if store.get("ok") else None
    checks: dict[str, Any] = {
        "store": store,
        "knowledge_base_loaded": bool(stats and stats.chunks > 0),
        "openrouter_configured": container.chat is not None,
        "embedding_model_matches": bool(
            stats is None or stats.embedding_model in (None, container.embedder.model)
        ),
        "embedding_dimensions_match": bool(
            stats is None or stats.embedding_dimensions in (None, container.embedder.dimensions)
        ),
    }
    ok = bool(
        store.get("ok")
        and checks["knowledge_base_loaded"]
        and checks["openrouter_configured"]
        and checks["embedding_model_matches"]
        and checks["embedding_dimensions_match"]
    )
    return ok, checks


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "Not ready"}},
)
async def ready(request: Request, response: Response) -> dict[str, Any]:
    ttl: float = request.app.state.container.settings.readiness_cache_seconds
    cached = getattr(request.app.state, "readiness_cache", None)
    now = time.monotonic()
    if cached is not None and now - cached[0] < ttl:
        ok, checks = cached[1], cached[2]
    else:
        ok, checks = await _readiness(request)
        request.app.state.readiness_cache = (now, ok, checks)
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ok else "not_ready", "checks": checks}


@router.get("/metrics", include_in_schema=False, dependencies=[Depends(require_metrics_token)])
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

"""Liveness, readiness and metrics."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from v2e_rag import __version__

router = APIRouter(tags=["Operations"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict[str, Any]:
    container = request.app.state.container
    store = await container.store.health()
    stats = await container.store.stats() if store.get("ok") else None
    checks = {
        "store": store,
        "knowledge_base_loaded": bool(stats and stats.chunks > 0),
        "openrouter_configured": container.chat is not None,
        "embedding_model_matches": bool(
            stats is None or stats.embedding_model in (None, container.embedder.model)
        ),
    }
    ok = (
        store.get("ok")
        and checks["knowledge_base_loaded"]
        and checks["openrouter_configured"]
        and checks["embedding_model_matches"]
    )
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ok else "not_ready", "checks": checks}


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

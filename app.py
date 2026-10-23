"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from v2e_rag import __version__
from v2e_rag.api.routes import admin, generation, health, knowledge
from v2e_rag.config import Settings, get_settings
from v2e_rag.container import Container, build_container
from v2e_rag.generation.service import GenerationRejected, VerificationUnavailable
from v2e_rag.llm.openrouter import LLMError, LLMOutputError
from v2e_rag.observability.logging import configure_logging, get_logger
from v2e_rag.observability.metrics import REQUEST_LATENCY, REQUESTS
from v2e_rag.retrieval.evidence import InsufficientEvidenceError

log = get_logger(__name__)

DESCRIPTION = """
Grounded retrieval augmented generation for the Vet2Employ AI Manager.

Every output is generated from retrieved evidence only, every claim carries citations, and
every response is verified before it is returned. When the knowledge base cannot support a
request the service abstains instead of guessing.
"""


def _error(
    status_code: int, error: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "message": message, "details": details or {}},
    )


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container or build_container(settings)
        app.state.ingestion_runs = {}
        app.state.background_tasks = set()
        await app.state.container.start()
        log.info(
            "service_started",
            version=__version__,
            backend=settings.vector_backend,
            environment=settings.environment,
        )
        try:
            yield
        finally:
            for task in list(app.state.background_tasks):
                task.cancel()
            await app.state.container.stop()
            log.info("service_stopped")

    app = FastAPI(
        title="Vet2Employ RAG Service",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"],
        )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, path=request.url.path)
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        elapsed = time.perf_counter() - started
        REQUESTS.labels(
            route=route_path, method=request.method, status=str(response.status_code)
        ).inc()
        REQUEST_LATENCY.labels(route=route_path).observe(elapsed)
        response.headers["X-Request-ID"] = request_id
        if not request.url.path.startswith(("/health", "/metrics")):
            log.info(
                "request_completed",
                status=response.status_code,
                ms=int(elapsed * 1000),
                method=request.method,
            )
        return response

    @app.exception_handler(InsufficientEvidenceError)
    async def insufficient(_: Request, exc: InsufficientEvidenceError) -> JSONResponse:
        return _error(
            422,
            "insufficient_evidence",
            str(exc),
            {"missing": exc.missing_steps, "diagnostics": exc.diagnostics},
        )

    @app.exception_handler(VerificationUnavailable)
    async def verification_unavailable(_: Request, exc: VerificationUnavailable) -> JSONResponse:
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "verification_unavailable",
            str(exc),
            {
                "audit_id": exc.audit_id,
                "verification": exc.report.to_dict() if exc.report else None,
            },
        )

    @app.exception_handler(GenerationRejected)
    async def rejected(_: Request, exc: GenerationRejected) -> JSONResponse:
        return _error(
            422,
            "grounding_failed",
            str(exc),
            {
                "audit_id": exc.audit_id,
                "verification": exc.report.to_dict() if exc.report else None,
            },
        )

    @app.exception_handler(LLMOutputError)
    async def bad_output(_: Request, exc: LLMOutputError) -> JSONResponse:
        return _error(status.HTTP_502_BAD_GATEWAY, "invalid_model_output", str(exc))

    @app.exception_handler(LLMError)
    async def upstream(_: Request, exc: LLMError) -> JSONResponse:
        return _error(status.HTTP_502_BAD_GATEWAY, "upstream_model_error", str(exc))

    @app.exception_handler(RuntimeError)
    async def runtime(_: Request, exc: RuntimeError) -> JSONResponse:
        log.error("runtime_error", error=str(exc))
        return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "service_unavailable", str(exc))

    app.include_router(health.router)
    app.include_router(generation.router)
    app.include_router(knowledge.router)
    app.include_router(admin.router)
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory v2e_rag.api.app:app_factory``."""
    return create_app()

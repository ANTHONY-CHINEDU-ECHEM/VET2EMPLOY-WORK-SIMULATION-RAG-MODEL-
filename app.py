"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from v2e_rag import __version__
from v2e_rag.api.errors import install_error_handlers
from v2e_rag.api.middleware import RequestContextMiddleware
from v2e_rag.api.routes import admin, generation, health, knowledge
from v2e_rag.api.schemas import ErrorResponse
from v2e_rag.config import Settings, get_settings
from v2e_rag.container import Container, build_container
from v2e_rag.llm.prompts import PROMPT_VERSION
from v2e_rag.observability.logging import configure_logging, get_logger
from v2e_rag.observability.metrics import BUILD_INFO

log = get_logger(__name__)

DESCRIPTION = """
Grounded retrieval augmented generation for the Vet2Employ AI Manager.

Every output is generated from retrieved evidence only, every claim carries citations, and
every response is verified before it is returned. When the knowledge base cannot support a
request the service abstains instead of guessing.

Every error response uses the same envelope: `error` (a stable code), `message`, `details`
and `request_id`.
"""

OPENAPI_TAGS = [
    {"name": "AI Manager", "description": "Grounded tasks and in character replies."},
    {"name": "Knowledge", "description": "Retrieval, verification, catalogue and insights."},
    {"name": "Administration", "description": "Knowledge base operations. Admin key required."},
    {"name": "Operations", "description": "Health and readiness probes."},
]


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())
    configure_logging(settings.log_level, settings.log_json)
    BUILD_INFO.info({"version": __version__, "prompt_version": PROMPT_VERSION})

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container or build_container(settings)
        app.state.ingestion_runs = {}
        app.state.background_tasks = set()
        app.state.readiness_cache = None
        await app.state.container.start()
        log.info(
            "service_started",
            version=__version__,
            prompt_version=PROMPT_VERSION,
            backend=settings.vector_backend,
            environment=settings.environment,
            entailment=settings.verification_enable_entailment,
            safeguarding=settings.safeguarding_enabled,
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
        docs_url="/docs" if settings.expose_docs else None,
        redoc_url="/redoc" if settings.expose_docs else None,
        openapi_url="/openapi.json" if settings.expose_docs else None,
        openapi_tags=OPENAPI_TAGS,
        responses={500: {"model": ErrorResponse, "description": "Unexpected error"}},
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
        )
    # Added last so it is the outermost layer: it sees every request and every failure.
    app.add_middleware(
        RequestContextMiddleware,
        max_body_bytes=settings.max_request_body_bytes,
        upload_paths=("/v1/admin/documents",),
        max_upload_bytes=settings.max_upload_bytes,
    )

    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(generation.router)
    app.include_router(knowledge.router)
    app.include_router(admin.router)
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory v2e_rag.api.app:app_factory``."""
    return create_app()

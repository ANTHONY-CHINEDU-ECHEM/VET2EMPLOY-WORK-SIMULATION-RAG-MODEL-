"""The public error contract.

Every non 2xx response from this service has the same body, whatever produced it (a
pipeline decision, request validation, authentication, an upstream failure or an
unexpected crash):

    {"error": "<stable code>", "message": "<safe text>", "details": {...}, "request_id": "..."}

Messages never contain stack traces, SQL, connection strings or raw provider responses.
Those go to the structured log, correlated by ``request_id``.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import psycopg
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException

from v2e_rag.embeddings.base import EmbeddingError
from v2e_rag.errors import ServiceError
from v2e_rag.generation.service import GenerationRejected, VerificationUnavailable
from v2e_rag.llm.openrouter import LLMError, LLMOutputError
from v2e_rag.observability.logging import get_logger
from v2e_rag.retrieval.evidence import InsufficientEvidenceError

log = get_logger(__name__)

_HTTP_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "timeout",
}

_SERVICE_STATUS: dict[str, int] = {
    "service_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "store_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "unknown_chunks": 422,
    "invalid_request": 422,
    "ingestion_in_progress": status.HTTP_409_CONFLICT,
}


def request_id_of(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def error_response(
    request: Request | None,
    status_code: int,
    error: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": error,
            "message": message,
            "details": details or {},
            "request_id": request_id_of(request) if request is not None else None,
        },
        headers=headers,
    )


def _validation_details(exc: RequestValidationError) -> dict[str, Any]:
    """Field level problems without echoing the submitted values, which may hold
    personal data from a candidate message."""
    problems = []
    for item in exc.errors()[:50]:
        problems.append(
            {
                "loc": [str(p) for p in item.get("loc", ())],
                "msg": str(item.get("msg", "")),
                "type": str(item.get("type", "")),
            }
        )
    return {"errors": problems}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(InsufficientEvidenceError)
    async def insufficient(request: Request, exc: InsufficientEvidenceError) -> JSONResponse:
        return error_response(
            request,
            422,
            "insufficient_evidence",
            str(exc),
            {"missing": exc.missing_steps, "diagnostics": exc.diagnostics},
        )

    @app.exception_handler(VerificationUnavailable)
    async def verification_unavailable(
        request: Request, exc: VerificationUnavailable
    ) -> JSONResponse:
        return error_response(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "verification_unavailable",
            str(exc),
            {
                "audit_id": exc.audit_id,
                "verification": exc.report.to_dict() if exc.report else None,
            },
            headers={"Retry-After": "30"},
        )

    @app.exception_handler(GenerationRejected)
    async def rejected(request: Request, exc: GenerationRejected) -> JSONResponse:
        return error_response(
            request,
            422,
            "grounding_failed",
            str(exc),
            {
                "audit_id": exc.audit_id,
                "verification": exc.report.to_dict() if exc.report else None,
            },
        )

    @app.exception_handler(LLMOutputError)
    async def bad_output(request: Request, exc: LLMOutputError) -> JSONResponse:
        log.warning("invalid_model_output", error=str(exc))
        return error_response(
            request,
            status.HTTP_502_BAD_GATEWAY,
            "invalid_model_output",
            "The model returned output that did not match the required schema.",
            {"audit_id": getattr(exc, "audit_id", None)},
        )

    @app.exception_handler(LLMError)
    async def upstream(request: Request, exc: LLMError) -> JSONResponse:
        log.warning("upstream_model_error", error=str(exc))
        return error_response(
            request,
            status.HTTP_502_BAD_GATEWAY,
            "upstream_model_error",
            "The model provider could not complete the request. Retry with backoff.",
            {
                "provider_status": getattr(exc, "status_code", None),
                "audit_id": getattr(exc, "audit_id", None),
            },
        )

    @app.exception_handler(EmbeddingError)
    async def embedding_failed(request: Request, exc: EmbeddingError) -> JSONResponse:
        log.warning("embedding_error", error=str(exc))
        return error_response(
            request,
            status.HTTP_502_BAD_GATEWAY,
            "upstream_model_error",
            "The embedding provider could not complete the request. Retry with backoff.",
        )

    @app.exception_handler(TimeoutError)
    async def timed_out(request: Request, exc: TimeoutError) -> JSONResponse:
        log.warning("request_deadline_exceeded")
        return error_response(
            request,
            status.HTTP_504_GATEWAY_TIMEOUT,
            "generation_timeout",
            "The request did not complete within the service deadline. Retry with backoff.",
        )

    @app.exception_handler(psycopg.OperationalError)
    @app.exception_handler(PoolTimeout)
    async def store_down(request: Request, exc: Exception) -> JSONResponse:
        log.error("store_unavailable", error_type=type(exc).__name__, error=str(exc))
        return error_response(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "store_unavailable",
            "The knowledge base store is temporarily unavailable. Retry with backoff.",
            headers={"Retry-After": "10"},
        )

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> JSONResponse:
        code = _SERVICE_STATUS.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        if code >= 500:
            log.error("service_error", code=exc.code, error=str(exc))
        return error_response(request, code, exc.code, str(exc), exc.details)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            request,
            422,
            "invalid_request",
            "The request body or parameters are not valid.",
            _validation_details(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "error")
        try:
            default = HTTPStatus(exc.status_code).phrase
        except ValueError:
            default = "Error"
        message = exc.detail if isinstance(exc.detail, str) else default
        return error_response(
            request, exc.status_code, code, message, headers=getattr(exc, "headers", None)
        )

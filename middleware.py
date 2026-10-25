"""Request context middleware (pure ASGI).

For every HTTP request it:

* assigns a request id (accepting a well formed ``X-Request-ID`` from the caller, otherwise
  generating one) and binds it to every log line and every error body;
* rejects bodies larger than the configured limit before they are read into memory;
* records Prometheus metrics, including requests in flight, even when the handler crashes;
* converts any unhandled exception into the standard error envelope (``internal_error``)
  instead of a bare text 500, logging the traceback server side only;
* adds defensive response headers.

It is written as plain ASGI rather than ``BaseHTTPMiddleware`` so that context variables
and exceptions behave predictably and streaming responses are not buffered.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import structlog
from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from v2e_rag.observability.logging import get_logger
from v2e_rag.observability.metrics import IN_FLIGHT, REQUEST_LATENCY, REQUESTS

log = get_logger(__name__)

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
QUIET_PREFIXES = ("/health", "/metrics")

_API_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
]
_DOC_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
]


class _BodyTooLarge(HTTPException):
    """Raised while streaming a body past the limit. It subclasses ``HTTPException`` so
    FastAPI's body parser re-raises it unchanged and the error handler answers ``413``."""

    def __init__(self, limit: int) -> None:
        super().__init__(413, "The request body is too large.")
        self.limit = limit


def resolve_request_id(raw: str | None) -> str:
    """Use the caller's id when it is safe to log, otherwise generate one."""
    if raw and REQUEST_ID_RE.fullmatch(raw):
        return raw
    return str(uuid.uuid4())


class RequestContextMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        upload_paths: tuple[str, ...] = (),
        max_upload_bytes: int = 0,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.upload_paths = upload_paths
        # Multipart overhead (boundaries and part headers) on top of the file itself.
        self.max_upload_bytes = max_upload_bytes + 1024 * 1024

    def _limit_for(self, path: str) -> int:
        if self.upload_paths and path.startswith(self.upload_paths):
            return max(self.max_body_bytes, self.max_upload_bytes)
        return self.max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        request_id = resolve_request_id(headers.get("x-request-id"))
        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET")
        scope.setdefault("state", {})["request_id"] = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, path=path, method=method)

        limit = self._limit_for(path)
        declared = headers.get("content-length")
        started = time.perf_counter()
        status_holder: dict[str, int] = {"status": 500}
        response_started = False
        extra_headers = _DOC_HEADERS if path.startswith(("/docs", "/redoc")) else _API_HEADERS

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_holder["status"] = int(message["status"])
                raw_headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in raw_headers}
                raw_headers.append((b"x-request-id", request_id.encode("latin-1")))
                raw_headers.extend((k, v) for k, v in extra_headers if k not in present)
                message = {**message, "headers": raw_headers}
            await send(message)

        received = 0

        async def receive_wrapper() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge(limit)
            return message

        IN_FLIGHT.inc()
        try:
            if declared is not None and declared.isdigit() and int(declared) > limit:
                await self._send_error(
                    send_wrapper,
                    413,
                    "payload_too_large",
                    "The request body is too large.",
                    request_id,
                    {"limit_bytes": limit},
                )
            else:
                await self.app(scope, receive_wrapper, send_wrapper)
        except _BodyTooLarge:
            if not response_started:
                await self._send_error(
                    send_wrapper,
                    413,
                    "payload_too_large",
                    "The request body is too large.",
                    request_id,
                    {"limit_bytes": limit},
                )
        except Exception:
            log.exception("unhandled_exception")
            if not response_started:
                await self._send_error(
                    send_wrapper,
                    500,
                    "internal_error",
                    "An unexpected error occurred. Quote the request id when reporting it.",
                    request_id,
                )
        finally:
            IN_FLIGHT.dec()
            elapsed = time.perf_counter() - started
            route = scope.get("route")
            route_path = getattr(route, "path", None) or "unmatched"
            status_code = status_holder["status"]
            REQUESTS.labels(route=route_path, method=method, status=str(status_code)).inc()
            REQUEST_LATENCY.labels(route=route_path).observe(elapsed)
            if not path.startswith(QUIET_PREFIXES):
                log.info("request_completed", status=status_code, ms=int(elapsed * 1000))

    @staticmethod
    async def _send_error(
        send: Send,
        status_code: int,
        error: str,
        message: str,
        request_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        body = json.dumps(
            {"error": error, "message": message, "details": details or {}, "request_id": request_id}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

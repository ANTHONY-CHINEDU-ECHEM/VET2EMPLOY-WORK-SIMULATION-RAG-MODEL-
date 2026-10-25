"""HTTP contract guarantees added in 1.1: one error envelope for every failure, request
correlation, defensive headers, body limits, deadlines, crash containment, safeguarding
and the corrected admin behaviour."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from tests.conftest import (
    ScriptedChat,
    build_test_container,
    evidence_ids_by_facet,
    heuristic_judge,
    task_handler,
)
from v2e_rag.api.app import create_app
from v2e_rag.config import Settings
from v2e_rag.llm.openrouter import LLMError

AUTH = {"X-API-Key": "test-key"}
ADMIN = {"X-API-Key": "admin-key"}


def _reply(review: bool = False) -> Any:
    def handler(messages: list[dict[str, str]]) -> dict[str, Any]:
        deliv = evidence_ids_by_facet(messages)["track_deliverables"][0]
        return {
            "reply": "The Data Quality Log is due Tue 17:00.\n\nYour Track Supervisor",
            "intent": "answer",
            "claims": [{"text": "The Data Quality Log is due Tue 17:00.", "evidence_ids": [deliv]}],
            "needs_human_review": review,
            "review_reason": None,
        }

    return handler


async def _client(
    settings: Settings, handlers: dict[str, Any] | None = None
) -> tuple[httpx.AsyncClient, Any, Any]:
    chat = ScriptedChat(
        handlers
        or {"task": task_handler(), "task_repair": task_handler(), "manager_reply": _reply()}
    )
    container = await build_test_container(
        settings, chat, ScriptedChat({"verification": heuristic_judge})
    )
    app = create_app(settings=settings, container=container)
    return app, container, chat


@pytest.fixture
async def api(settings) -> AsyncIterator[tuple[httpx.AsyncClient, Any, Any]]:
    app, container, chat = await _client(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, container, chat


def _assert_envelope(response: httpx.Response, code: str) -> dict[str, Any]:
    body = response.json()
    assert set(body) == {"error", "message", "details", "request_id"}, body
    assert body["error"] == code, body
    assert body["request_id"] == response.headers["X-Request-ID"]
    return body


# Error envelope ----------------------------------------------------------------------------


async def test_validation_errors_use_the_envelope_without_echoing_input(api):
    client, _, _ = api
    secret_like = "ada.secret@example.com"
    response = await client.post(
        "/v1/tasks/generate",
        headers=AUTH,
        json={"track": "DA", "unexpected": secret_like},
    )
    assert response.status_code == 422
    body = _assert_envelope(response, "invalid_request")
    assert body["details"]["errors"][0]["loc"][-1] == "unexpected"
    assert secret_like not in response.text


async def test_auth_and_routing_errors_use_the_envelope(api):
    client, _, _ = api
    _assert_envelope(await client.post("/v1/tasks/generate", json={"track": "DA"}), "unauthorized")
    _assert_envelope(await client.get("/v1/admin/documents", headers=AUTH), "forbidden")
    _assert_envelope(await client.get("/v1/admin/documents"), "unauthorized")
    _assert_envelope(await client.get("/v1/does-not-exist", headers=AUTH), "not_found")


async def test_unexpected_exceptions_return_internal_error_without_details(api, monkeypatch):
    client, container, _ = api

    async def explode(_request: Any) -> Any:
        raise ZeroDivisionError("database password is hunter2")

    monkeypatch.setattr(container.service, "generate_task", explode)
    response = await client.post("/v1/tasks/generate", headers=AUTH, json={"track": "DA"})
    assert response.status_code == 500
    _assert_envelope(response, "internal_error")
    assert "hunter2" not in response.text


async def test_upstream_errors_hide_provider_text_and_are_audited(api, monkeypatch):
    client, container, chat = api

    def fail(_messages: Any) -> Any:
        raise LLMError("OpenRouter HTTP 500: provider internals sk-or-abcdefghijklmnop", 500)

    chat.handlers["task"] = fail
    response = await client.post(
        "/v1/tasks/generate", headers=AUTH, json={"track": "DA", "activity_number": 3}
    )
    assert response.status_code == 502
    body = _assert_envelope(response, "upstream_model_error")
    assert "provider internals" not in response.text and "sk-or" not in response.text
    assert body["details"]["provider_status"] == 500
    audit = await container.store.get_audit(body["details"]["audit_id"])
    assert audit is not None and audit["status"] == "model_error"


# Correlation and headers -------------------------------------------------------------------


async def test_request_ids_are_echoed_or_replaced_when_unsafe(api):
    client, container, _ = api
    good = await client.get("/health/live", headers={"X-Request-ID": "platform-42"})
    assert good.headers["X-Request-ID"] == "platform-42"
    bad = await client.get("/health/live", headers={"X-Request-ID": "x" * 300})
    assert bad.headers["X-Request-ID"] != "x" * 300 and len(bad.headers["X-Request-ID"]) == 36

    response = await client.post(
        "/v1/tasks/generate",
        headers={**AUTH, "X-Request-ID": "trace-7"},
        json={"track": "DA", "activity_number": 3},
    )
    audit = await container.store.get_audit(response.json()["meta"]["audit_id"])
    assert audit["request_id"] == "trace-7"


async def test_defensive_headers_are_set(api):
    client, _, _ = api
    response = await client.get("/v1/catalog", headers=AUTH)
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


# Limits and deadlines ---------------------------------------------------------------------


async def test_oversized_bodies_are_rejected(settings):
    small = settings.model_copy(update={"max_request_body_bytes": 2000})
    app, _, _ = await _client(small)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/retrieve", headers=AUTH, json={"query": "x" * 5000, "limit": 3}
            )
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"


async def test_slow_generation_hits_the_deadline(settings):
    tight = settings.model_copy(update={"request_timeout_seconds": 0.2})
    app, container, _ = await _client(tight)

    async def stalled(_request: Any) -> Any:
        await asyncio.sleep(5)

    container.service.generate_task = stalled
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/tasks/generate", headers=AUTH, json={"track": "DA"})
    assert response.status_code == 504
    assert response.json()["error"] == "generation_timeout"


# Catalogue validation and verification -----------------------------------------------------


async def test_activity_beyond_track_count_is_rejected_before_retrieval(api):
    client, _, chat = api
    response = await client.post(
        "/v1/tasks/generate", headers=AUTH, json={"track": "DA", "activity_number": 13}
    )
    assert response.status_code == 422
    body = _assert_envelope(response, "invalid_request")
    assert body["details"]["activity_count"] == 12
    assert chat.calls == []


async def test_verify_rejects_unknown_chunk_ids(api):
    client, _, _ = api
    response = await client.post(
        "/v1/verify",
        headers=AUTH,
        json={"text": "The log is due Tue 17:00.", "chunk_ids": ["da_deliverables", "nope"]},
    )
    assert response.status_code == 422
    assert _assert_envelope(response, "unknown_chunks")["details"]["missing"] == ["nope"]


async def test_retrieve_rejects_unknown_filter_values(api):
    client, _, _ = api
    response = await client.post(
        "/v1/retrieve", headers=AUTH, json={"query": "deadline", "facets": ["not_a_facet"]}
    )
    assert response.status_code == 422


# Safeguarding over HTTP --------------------------------------------------------------------


async def test_crisis_message_is_intercepted_without_any_model_call(api):
    client, _, chat = api
    response = await client.post(
        "/v1/manager/reply",
        headers=AUTH,
        json={
            "track": "DA",
            "candidate_first_name": "Sam",
            "candidate_message": "I have been thinking about ending my life.",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verification"]["verdict"] == "intercepted"
    assert body["safeguarding"] == {
        "flagged": True,
        "categories": ["crisis"],
        "action": "intercept",
    }
    assert body["reply"]["needs_human_review"] is True and body["reply"]["intent"] == "escalate"
    assert "116 123" in body["reply"]["reply"] and "Sam" in body["reply"]["reply"]
    assert chat.calls == []


async def test_flagged_message_forces_human_review_even_if_model_disagrees(api):
    client, _, _ = api
    response = await client.post(
        "/v1/manager/reply",
        headers=AUTH,
        json={
            "track": "DA",
            "candidate_message": "I feel completely overwhelmed. When is the Data Quality Log due?",
        },
    )
    body = response.json()
    assert response.status_code == 200
    assert body["safeguarding"]["action"] == "flag"
    assert body["reply"]["needs_human_review"] is True
    assert "stress" in body["reply"]["review_reason"]


async def test_ordinary_message_is_not_flagged(api):
    client, _, _ = api
    body = (
        await client.post(
            "/v1/manager/reply",
            headers=AUTH,
            json={"track": "DA", "candidate_message": "When is the Data Quality Log due?"},
        )
    ).json()
    assert body["safeguarding"] == {"flagged": False, "categories": [], "action": "none"}
    assert body["reply"]["needs_human_review"] is False


# Administration -----------------------------------------------------------------------------


async def test_admin_delete_unknown_document_is_404(api):
    client, _, _ = api
    response = await client.delete("/v1/admin/documents/doc_missing", headers=ADMIN)
    assert response.status_code == 404
    _assert_envelope(response, "not_found")


async def test_admin_upload_checks_file_signature(api):
    client, _, _ = api
    response = await client.post(
        "/v1/admin/documents",
        headers=ADMIN,
        files={"file": ("brief.pdf", b"this is not a pdf", "application/pdf")},
    )
    assert response.status_code == 400
    assert "not a valid .pdf" in response.json()["message"]


async def test_admin_upload_of_corrupt_workbook_is_422_and_written_atomically(api, settings):
    client, _, _ = api
    response = await client.post(
        "/v1/admin/documents",
        headers=ADMIN,
        files={"file": ("Data_Analyst_Test.xlsx", b"PK\x03\x04 broken zip", "application/zip")},
    )
    assert response.status_code == 422
    leftovers = [p.name for p in settings.knowledge_base_dir.iterdir() if p.name.startswith(".")]
    assert leftovers == []


# Operations ---------------------------------------------------------------------------------


async def test_metrics_token_is_enforced_when_configured(settings):
    from pydantic import SecretStr

    guarded = settings.model_copy(update={"metrics_token": SecretStr("scrape-token")})
    app, _, _ = await _client(guarded)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/metrics")).status_code == 401
            ok = await client.get("/metrics", headers={"Authorization": "Bearer scrape-token"})
            assert ok.status_code == 200 and "v2e_http_requests_in_flight" in ok.text


async def test_docs_can_be_hidden(settings):
    hidden = settings.model_copy(update={"expose_docs": False})
    app, _, _ = await _client(hidden)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/docs")).status_code == 404
            assert (await client.get("/openapi.json")).status_code == 404


async def test_readiness_is_cached(api, monkeypatch):
    client, container, _ = api
    calls = 0
    original = container.store.stats

    async def counting() -> Any:
        nonlocal calls
        calls += 1
        return await original()

    monkeypatch.setattr(container.store, "stats", counting)
    for _ in range(3):
        assert (await client.get("/health/ready")).status_code == 200
    assert calls == 1


async def test_database_outage_is_a_retryable_503(api, monkeypatch):
    import psycopg

    client, container, _ = api

    async def down(*_args: Any, **_kwargs: Any) -> Any:
        raise psycopg.OperationalError("connection to server at db:5432 failed: password=secret")

    monkeypatch.setattr(container.store, "stats", down)
    response = await client.get("/v1/catalog", headers=AUTH)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "10"
    _assert_envelope(response, "store_unavailable")
    assert "secret" not in response.text and "db:5432" not in response.text

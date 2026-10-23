"""HTTP contract tests through the ASGI app with scripted models."""

from __future__ import annotations

from collections.abc import AsyncIterator

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

AUTH = {"X-API-Key": "test-key"}
ADMIN = {"X-API-Key": "admin-key"}


def _reply(messages):
    ids = evidence_ids_by_facet(messages)
    deliv = ids["track_deliverables"][0]
    return {
        "reply": "The Data Quality Log is due Tue 17:00.\n\nYour Track Supervisor",
        "intent": "answer",
        "claims": [{"text": "The Data Quality Log is due Tue 17:00.", "evidence_ids": [deliv]}],
        "needs_human_review": False,
        "review_reason": None,
    }


@pytest.fixture
async def client(settings) -> AsyncIterator[httpx.AsyncClient]:
    chat = ScriptedChat(
        {"task": task_handler(), "task_repair": task_handler(), "manager_reply": _reply}
    )
    container = await build_test_container(
        settings, chat, ScriptedChat({"verification": heuristic_judge})
    )
    app = create_app(settings=settings, container=container)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def test_health_and_metrics(client):
    assert (await client.get("/health/live")).json()["status"] == "ok"
    ready = await client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["checks"]["knowledge_base_loaded"] is True
    metrics = await client.get("/metrics")
    assert "v2e_http_requests_total" in metrics.text


async def test_authentication_is_enforced(client):
    response = await client.post("/v1/tasks/generate", json={"track": "DA"})
    assert response.status_code == 401
    assert (await client.get("/v1/admin/documents", headers=AUTH)).status_code == 403


async def test_generate_task_contract(client):
    response = await client.post(
        "/v1/tasks/generate",
        headers={**AUTH, "X-Request-ID": "req-123"},
        json={
            "track": "DA",
            "activity_number": 3,
            "job_level": "mid_level",
            "sector": "consulting",
            "candidate_first_name": "Ada",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == "req-123"
    body = response.json()
    assert body["verification"]["verdict"] == "approved"
    assert body["task"]["deliverables"][0]["reference"] == "D2"
    assert {e["evidence_id"] for e in body["evidence"]} >= set(
        body["task"]["summary"]["evidence_ids"]
    )
    assert body["meta"]["audit_id"] and body["meta"]["prompt_version"]


async def test_validation_and_abstention_errors(client):
    mismatch = await client.post(
        "/v1/tasks/generate", headers=AUTH, json={"track": "DA", "role_family": "cybersecurity"}
    )
    assert mismatch.status_code == 422
    missing = await client.post(
        "/v1/tasks/generate", headers=AUTH, json={"track": "SE", "activity_number": 4}
    )
    assert missing.status_code == 422
    assert missing.json()["error"] == "insufficient_evidence"
    assert "activity_definition" in missing.json()["details"]["missing"]


async def test_manager_reply_contract(client):
    response = await client.post(
        "/v1/manager/reply",
        headers=AUTH,
        json={"track": "DA", "candidate_message": "When is the Data Quality Log due?"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"]["intent"] == "answer" and body["verification"]["verdict"] == "approved"


async def test_retrieve_and_verify(client):
    retrieved = await client.post(
        "/v1/retrieve",
        headers=AUTH,
        json={"query": "Data Quality Log due", "track": "DA", "limit": 3},
    )
    assert retrieved.status_code == 200
    body = retrieved.json()
    assert body["evidence"] and "NON NEGOTIABLE GROUNDING RULES" in body["grounding_prompt"]
    chunk_ids = [e["chunk_id"] for e in body["evidence"]]
    verified = await client.post(
        "/v1/verify",
        headers=AUTH,
        json={"text": "The Data Quality Log is due Tue 17:00.", "chunk_ids": chunk_ids},
    )
    assert verified.status_code == 200
    assert verified.json()["verification"]["verdict"] == "approved"


async def test_catalog_and_insights(client):
    catalog = (await client.get("/v1/catalog", headers=AUTH)).json()
    assert {t["code"] for t in catalog["tracks"]} == {"DA", "SE", "CY", "DE"}
    assert catalog["knowledge_base"]["chunks"] > 0
    profile = await client.get(
        "/v1/insights/role_profile", headers=AUTH, params={"role_family": "data_analyst"}
    )
    assert profile.status_code == 200 and profile.json()["filters"]["role_family"] == "data_analyst"


async def test_admin_upload_rejects_unsupported_types(client):
    response = await client.post(
        "/v1/admin/documents", headers=ADMIN, files={"file": ("notes.txt", b"hello", "text/plain")}
    )
    assert response.status_code == 400
    documents = await client.get("/v1/admin/documents", headers=ADMIN)
    assert documents.status_code == 200 and documents.json()

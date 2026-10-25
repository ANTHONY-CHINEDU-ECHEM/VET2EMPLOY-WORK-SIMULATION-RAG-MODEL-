"""Model client resilience, store locks and retention, ingestion pruning and the CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from tests.conftest import ROOT
from tests.unit.test_ingestion_units import _workbook
from v2e_rag.domain.catalog import load_catalog
from v2e_rag.embeddings.base import (
    CachedEmbedder,
    EmbeddingError,
    HashingEmbedder,
    OpenRouterEmbedder,
)
from v2e_rag.errors import IngestionLockedError
from v2e_rag.ingestion.pipeline import IngestionPipeline
from v2e_rag.llm.openrouter import LLMError, LLMTruncatedError, OpenRouterClient
from v2e_rag.store.memory_store import MemoryStore

BASE = "https://openrouter.test/api/v1"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
MESSAGES = [{"role": "user", "content": "hi"}]


def _client() -> OpenRouterClient:
    return OpenRouterClient(api_key="sk-test", base_url=BASE, max_retries=0)


def _completion(content: str, finish: str = "stop") -> dict:
    return {
        "model": "m",
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


# OpenRouter client ----------------------------------------------------------------------------


@respx.mock
async def test_truncated_output_is_reported_as_truncation():
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion('{"ok": tr', finish="length"))
    )
    with pytest.raises(LLMTruncatedError, match="token limit"):
        await _client().complete_json(
            MESSAGES, purpose="task", schema_name="s", schema=SCHEMA, max_tokens=50
        )


@respx.mock
async def test_rejected_structured_mode_is_remembered_per_model():
    calls: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        mode = body.get("response_format", {}).get("type", "prompt")
        calls.append(mode)
        if mode == "json_schema":
            return httpx.Response(400, text="response_format json_schema is not supported")
        return httpx.Response(200, json=_completion('{"ok": true}'))

    respx.post(f"{BASE}/chat/completions").mock(side_effect=responder)
    client = _client()
    for _ in range(3):
        result = await client.complete_json(
            MESSAGES, purpose="task", schema_name="s", schema=SCHEMA, model="m1"
        )
        assert result.data == {"ok": True}
    assert calls == ["json_schema", "json_object", "json_object", "json_object"]


@respx.mock
async def test_http_errors_carry_the_provider_status():
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(401, text="bad key"))
    with pytest.raises(LLMError) as caught:
        await _client().complete_json(MESSAGES, purpose="task", schema_name="s", schema=SCHEMA)
    assert caught.value.status_code == 401


@respx.mock
async def test_choice_level_provider_errors_are_raised():
    body = {"model": "m", "choices": [{"error": {"message": "upstream overloaded"}}]}
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(LLMError, match="overloaded"):
        await _client().complete_json(MESSAGES, purpose="task", schema_name="s", schema=SCHEMA)


@respx.mock
async def test_embedding_http_errors_become_embedding_errors():
    respx.post(f"{BASE}/embeddings").mock(return_value=httpx.Response(402, text="no credit"))
    embedder = OpenRouterEmbedder(
        api_key="sk", model="e", dimensions=4, base_url=BASE, max_retries=0
    )
    with pytest.raises(EmbeddingError, match="402"):
        await embedder.embed_query("hello")


# Memory store ---------------------------------------------------------------------------------


async def test_ingestion_lock_is_exclusive():
    store = MemoryStore()
    async with store.ingestion_lock():
        with pytest.raises(IngestionLockedError):
            async with store.ingestion_lock():
                pass
    async with store.ingestion_lock():
        pass  # released after use


async def test_audit_lookup_and_purge():
    store = MemoryStore()
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    new = datetime.now(UTC).isoformat()
    await store.record_audit({"audit_id": "a1", "created_at": old})
    await store.record_audit({"audit_id": "a2", "created_at": new})
    assert (await store.get_audit("a2"))["created_at"] == new
    removed = await store.purge_audit(datetime.now(UTC) - timedelta(days=30))
    assert removed == 1 and await store.get_audit("a1") is None


async def test_delete_document_reports_whether_it_existed():
    store = MemoryStore()
    assert await store.delete_document("doc_missing") is False


async def test_snapshots_are_written_without_temporary_leftovers(tmp_path):
    store = MemoryStore(snapshot_path=tmp_path / "snap")
    store.save()
    names = sorted(p.name for p in (tmp_path / "snap").iterdir())
    assert names == ["chunks.jsonl", "documents.jsonl", "meta.json", "records.jsonl", "vectors.npy"]


# Ingestion pruning ----------------------------------------------------------------------------


def _pipeline(store: MemoryStore) -> IngestionPipeline:
    catalog = load_catalog(str(ROOT / "config"))
    return IngestionPipeline(store, CachedEmbedder(HashingEmbedder(64), store), catalog)


async def test_prune_removes_documents_whose_source_file_was_withdrawn(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    keep = _workbook(kb / "Data_Analyst_Construction_Oct_22nd_2026.xlsx")
    gone = _workbook(kb / "Data_Engineer_Consulting_Oct_19th_2026.xlsx")
    store = MemoryStore()
    pipeline = _pipeline(store)
    first = await pipeline.ingest_directory(kb)
    assert first.totals["ingested"] == 2
    gone.unlink()

    without_prune = await pipeline.ingest_directory(kb)
    assert without_prune.pruned == []
    report = await pipeline.ingest_directory(kb, prune=True)
    assert report.pruned == ["doc_data_engineer_consulting_oct_19th_2026"]
    remaining = {d.source_file for d in await store.list_documents()}
    assert keep.name in remaining and gone.name not in remaining
    assert "## Removed documents" in report.to_markdown()


async def test_prune_is_skipped_when_the_directory_is_empty(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    _workbook(kb / "Data_Analyst_Construction_Oct_22nd_2026.xlsx")
    store = MemoryStore()
    pipeline = _pipeline(store)
    await pipeline.ingest_directory(kb)
    for path in kb.iterdir():
        path.unlink()
    report = await pipeline.ingest_directory(kb, prune=True)
    assert report.pruned == [] and "no supported files" in report.notes[0]
    assert await store.list_documents()


async def test_missing_directory_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError):
        await _pipeline(MemoryStore()).ingest_directory(tmp_path / "absent")


# Command line ---------------------------------------------------------------------------------


@pytest.fixture
def offline_env(monkeypatch, tmp_path) -> Path:
    from v2e_rag.config import get_settings

    for key, value in {
        "V2E_ENVIRONMENT": "test",
        "V2E_VECTOR_BACKEND": "memory",
        "V2E_EMBEDDING_PROVIDER": "hashing",
        "V2E_EMBEDDING_DIMENSIONS": "64",
        "V2E_CONFIG_DIR": str(ROOT / "config"),
        "V2E_KNOWLEDGE_BASE_DIR": str(tmp_path / "kb"),
        "V2E_MEMORY_SNAPSHOT_PATH": str(tmp_path / "snap"),
        "V2E_AUDIT_LOG_PATH": str(tmp_path / "audit.jsonl"),
        "V2E_REPORTS_DIR": str(tmp_path / "reports"),
        "V2E_LOG_JSON": "false",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_cli_openapi_export_and_check(offline_env):
    from v2e_rag.cli import app

    target = offline_env / "openapi.json"
    runner = CliRunner()
    assert runner.invoke(app, ["openapi", "--output", str(target)]).exit_code == 0
    schema = json.loads(target.read_text())
    assert "/v1/tasks/generate" in schema["paths"]
    assert runner.invoke(app, ["openapi", "--output", str(target), "--check"]).exit_code == 0
    target.write_text("{}")
    assert runner.invoke(app, ["openapi", "--output", str(target), "--check"]).exit_code == 1


def test_committed_openapi_schema_is_current(offline_env):
    """Paths and schema names must match the code. CI also runs the exact text check
    (`v2e openapi --check`) with pinned dependencies."""
    from v2e_rag.api.app import create_app
    from v2e_rag.config import Settings

    live = create_app(Settings(_env_file=None, environment="test")).openapi()
    committed = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    assert set(committed["paths"]) == set(live["paths"]), "run `v2e openapi`"
    assert set(committed["components"]["schemas"]) == set(live["components"]["schemas"])
    assert committed["info"]["version"] == live["info"]["version"]


def test_cli_ingest_prune_and_doctor(offline_env):
    from v2e_rag.cli import app

    kb = offline_env / "kb"
    kb.mkdir()
    _workbook(kb / "Data_Analyst_Construction_Oct_22nd_2026.xlsx")
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", "--prune"])
    assert ingested.exit_code == 0, ingested.output
    doctor = runner.invoke(app, ["doctor"])
    assert "PASS  config: safeguarding" in doctor.output
    assert "PASS  knowledge base loaded" in doctor.output
    assert "WARN  settings safe for production" in doctor.output
    # No OpenRouter key in this environment, so doctor must report failure.
    assert "FAIL  OpenRouter key present" in doctor.output and doctor.exit_code == 1


def test_cli_audit_purge_requires_a_period(offline_env):
    from v2e_rag.cli import app

    result = CliRunner().invoke(app, ["audit", "purge", "--yes"])
    assert result.exit_code == 2


def test_cli_version(offline_env):
    from v2e_rag import __version__
    from v2e_rag.cli import app

    result = CliRunner().invoke(app, ["version"])
    assert json.loads(result.output)["version"] == __version__


async def test_memory_audit_lookup_reads_records_written_by_another_process(tmp_path):
    path = tmp_path / "audit.jsonl"
    writer = MemoryStore(audit_path=path)
    await writer.record_audit({"audit_id": "from-api", "status": "approved"})
    reader = MemoryStore(audit_path=path)
    assert (await reader.get_audit("from-api"))["status"] == "approved"

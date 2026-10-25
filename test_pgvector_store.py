"""PostgreSQL + pgvector backend. Runs when V2E_TEST_DATABASE_URL points at a disposable database.

Example:
    V2E_TEST_DATABASE_URL=postgresql://postgres@localhost:5433/v2e_test pytest -m integration
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tests.conftest import CORPUS, ROOT, make_chunk
from v2e_rag.domain.models import RoleRecord, SourceDocument
from v2e_rag.domain.taxonomy import JobLevel, SourceType
from v2e_rag.embeddings.base import HashingEmbedder
from v2e_rag.errors import IngestionLockedError
from v2e_rag.store.base import SearchFilters
from v2e_rag.store.migrations import EmbeddingDimensionMismatch, apply_migrations
from v2e_rag.store.pgvector_store import PgVectorStore, build_tsquery

DATABASE_URL = os.getenv("V2E_TEST_DATABASE_URL")
DIM = 64

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="V2E_TEST_DATABASE_URL is not set"),
]


@pytest.fixture
async def store():
    assert DATABASE_URL
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        for table in (
            "kb_chunks",
            "kb_role_records",
            "kb_documents",
            "embedding_cache",
            "generation_audit",
            "ingestion_runs",
            "schema_migrations",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    apply_migrations(DATABASE_URL, DIM, ROOT / "migrations")
    s = PgVectorStore(DATABASE_URL, min_size=1, max_size=4)
    await s.open()
    embedder = HashingEmbedder(DIM)
    chunks = [make_chunk(spec) for spec in CORPUS]
    by_doc: dict[str, list] = {}
    for chunk in chunks:
        by_doc.setdefault(chunk.document_id, []).append(chunk)
    for doc_id, doc_chunks in by_doc.items():
        record = RoleRecord(
            record_id=f"r_{doc_id}",
            document_id=doc_id,
            source_file="f.xlsx",
            sheet="Master",
            row_number=2,
            company="Example Consulting",
            job_title="Data Analyst",
            job_level=JobLevel.MID_LEVEL,
            role_families=["data_analyst"],
            sectors=["consulting"],
            fields={"tech_stack": "SQL; Power BI"},
        )
        await s.replace_document(
            SourceDocument(
                document_id=doc_id,
                source_file="synthetic",
                title=doc_id,
                source_type=doc_chunks[0].source_type,
                content_hash="h1",
            ),
            doc_chunks,
            await embedder.embed_documents([c.text for c in doc_chunks]),
            [record] if doc_chunks[0].source_type == SourceType.JOB_MARKET else [],
            embedder.model,
        )
    yield s
    await s.close()


async def test_migrations_are_idempotent_and_dimension_checked(store):
    assert apply_migrations(DATABASE_URL, DIM, ROOT / "migrations") == []
    with pytest.raises(EmbeddingDimensionMismatch):
        apply_migrations(DATABASE_URL, DIM + 1, ROOT / "migrations")


async def test_semantic_and_lexical_search_with_filters(store):
    query = await HashingEmbedder(DIM).embed_query("record each issue in the data quality log")
    semantic = await store.semantic_search(
        query, SearchFilters(tracks=("DA",), activity_numbers=(3,)), 5
    )
    assert [h.chunk.chunk_id for h in semantic] == ["da_activity_3"]
    lexical = await store.lexical_search(
        "Data Quality Log due", SearchFilters(source_types=("programme_brief",)), 5
    )
    assert lexical and lexical[0].chunk.source_type.value == "programme_brief"
    untracked = await store.lexical_search(
        "escalate blocked", SearchFilters(only_untracked=True), 5
    )
    assert untracked and all(h.chunk.track is None for h in untracked)
    market = await store.semantic_search(
        query,
        SearchFilters(
            role_families=("data_analyst",), sectors=("consulting",), job_levels=("mid_level",)
        ),
        10,
    )
    assert market and all(
        h.chunk.job_level and h.chunk.job_level.value == "mid_level" for h in market
    )
    assert build_tsquery("What's the Data-Quality log?!") == "data | quality | log"


async def test_replace_is_atomic_and_cascades(store):
    docs = await store.list_documents()
    assert {d.document_id for d in docs} >= {"doc_programme_brief", "doc_job_market"}
    stats = await store.stats()
    assert (
        stats.chunks == len(CORPUS)
        and stats.role_records == 1
        and stats.embedding_dimensions == DIM
    )
    await store.delete_document("doc_job_market")
    assert (await store.stats()).role_records == 0
    assert await store.get_chunks(["market_tech"]) == []
    fetched = await store.get_chunks(["da_activity_3", "norms"])
    assert [c.chunk_id for c in fetched] == ["da_activity_3", "norms"] and fetched[0].metadata[
        "activity_number"
    ] == 3


async def test_embedding_cache_and_audit(store):
    await store.cache_embeddings("m", {"h1": [0.1] * DIM})
    assert list((await store.cached_embeddings("m", ["h1", "h2"])).keys()) == ["h1"]
    await store.record_audit(
        {
            "audit_id": str(uuid.uuid4()),
            "operation": "task",
            "status": "approved",
            "request": {"track": "DA"},
            "evidence": [],
            "latency_ms": 5,
        }
    )
    records = await store.role_records(role_families=("data_analyst",))
    assert records and records[0].fields["tech_stack"] == "SQL; Power BI"


# Added in 1.1 ---------------------------------------------------------------------------------


async def test_migration_0002_adds_audit_columns_and_activity_index(store):
    assert DATABASE_URL
    with psycopg.connect(DATABASE_URL) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'generation_audit'"
            )
        }
        indexes = {row[0] for row in conn.execute("SELECT indexname FROM pg_indexes")}
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]
    assert {"request_id", "prompt_version", "details"} <= columns
    assert "kb_chunks_activity_idx" in indexes
    assert versions == ["0001_initial_schema", "0002_audit_correlation_and_retention"]


async def test_concurrent_migrations_are_serialised(store):
    import concurrent.futures

    assert DATABASE_URL
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version LIKE '0002%'")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: apply_migrations(DATABASE_URL, DIM, ROOT / "migrations"), range(4))
        )
    # Exactly one replica applied the migration; the others found it done.
    assert sorted(len(r) for r in results) == [0, 0, 0, 1]


async def test_missing_migrations_directory_fails_loudly(tmp_path, monkeypatch):
    from v2e_rag.store import migrations

    assert DATABASE_URL
    monkeypatch.setattr(migrations, "DEFAULT_MIGRATIONS_DIR", tmp_path / "also_missing")
    with pytest.raises(migrations.MigrationsNotFound):
        apply_migrations(DATABASE_URL, DIM, tmp_path / "missing")


async def test_ingestion_lock_spans_processes(store):
    assert DATABASE_URL
    other = PgVectorStore(DATABASE_URL, min_size=1, max_size=2)
    await other.open()
    try:
        async with store.ingestion_lock():
            with pytest.raises(IngestionLockedError):
                async with other.ingestion_lock():
                    pass
        async with other.ingestion_lock():
            pass  # released by the first holder
    finally:
        await other.close()


async def test_audit_round_trip_and_retention_purge(store):
    assert DATABASE_URL
    audit_id = str(uuid.uuid4())
    await store.record_audit(
        {
            "audit_id": audit_id,
            "request_id": "req-9",
            "operation": "manager_reply",
            "status": "intercepted",
            "request": {"track": "DA"},
            "evidence": [],
            "latency_ms": 3,
            "prompt_version": "2026.10.2",
            "safeguarding": {"flagged": True, "categories": ["crisis"], "action": "intercept"},
        }
    )
    record = await store.get_audit(audit_id)
    assert record is not None
    assert record["request_id"] == "req-9" and record["prompt_version"] == "2026.10.2"
    assert record["details"]["safeguarding"]["action"] == "intercept"
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "UPDATE generation_audit SET created_at = now() - interval '400 days' "
            "WHERE audit_id = %s",
            (audit_id,),
        )
    deleted = await store.purge_audit(datetime.now(UTC) - timedelta(days=365))
    assert deleted == 1 and await store.get_audit(audit_id) is None


async def test_delete_reports_unknown_documents(store):
    assert await store.delete_document("doc_does_not_exist") is False
    assert await store.delete_document("doc_job_market") is True


async def test_statement_timeout_is_applied_to_pool_connections():
    assert DATABASE_URL
    s = PgVectorStore(DATABASE_URL, min_size=1, max_size=1, statement_timeout_ms=1234)
    await s.open()
    try:
        async with s.pool.connection() as conn:
            row = await (await conn.execute("SHOW statement_timeout")).fetchone()
        assert row is not None and row[0] == "1234ms"
    finally:
        await s.close()

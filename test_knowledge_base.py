"""Ingest the real Vet2Employ knowledge base and check structure and retrieval quality.

Uses the offline hashing embedder so it runs without network access. Retrieval quality with
production embeddings is measured with ``v2e eval-retrieval``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import ROOT
from v2e_rag.config import Settings
from v2e_rag.container import build_container
from v2e_rag.domain.taxonomy import Facet
from v2e_rag.evaluation.retrieval_eval import run_retrieval_eval
from v2e_rag.store.memory_store import MemoryStore

KB_DIR = ROOT / "knowledge_base" / "raw"

pytestmark = [
    pytest.mark.knowledge_base,
    pytest.mark.skipif(
        not any(KB_DIR.glob("*.pdf")), reason="knowledge base files are not present"
    ),
]


@pytest.fixture(scope="module")
def kb_settings(tmp_path_factory) -> Settings:
    tmp = tmp_path_factory.mktemp("kb")
    return Settings(
        _env_file=None,
        environment="test",
        vector_backend="memory",
        embedding_provider="hashing",
        embedding_dimensions=384,
        config_dir=ROOT / "config",
        knowledge_base_dir=KB_DIR,
        audit_log_path=tmp / "audit.jsonl",
        memory_snapshot_path=tmp / "snapshot",
        min_semantic_similarity=0.05,
        log_json=False,
        log_level="WARNING",
    )


@pytest.fixture(scope="module")
async def kb_container(kb_settings):
    container = build_container(kb_settings)
    await container.start()
    report = await container.ingestion_pipeline().ingest_directory(KB_DIR)
    container.ingestion_report = report  # type: ignore[attr-defined]
    yield container
    await container.stop()


@pytest.mark.asyncio(loop_scope="module")
async def test_every_file_ingests(kb_container):
    totals = kb_container.ingestion_report.totals
    assert totals["failed"] == 0 and totals["files"] >= 27
    assert totals["records"] >= 500


@pytest.mark.asyncio(loop_scope="module")
async def test_each_track_has_its_twelve_activities(kb_container):
    store: MemoryStore = kb_container.store
    for track in ("DA", "SE", "CY", "DE"):
        activities = {
            c.metadata.get("activity_number")
            for c in store.chunks.values()
            if c.track == track
            and c.facet == Facet.TRACK_ACTIVITY
            and c.metadata.get("activity_number")
        }
        assert activities == set(range(1, 13)), (track, sorted(activities))
        facets = {c.facet for c in store.chunks.values() if c.track == track}
        assert {
            Facet.TRACK_CLIENT,
            Facet.TRACK_DELIVERABLES,
            Facet.TRACK_RUBRIC,
            Facet.SUPERVISOR_VOICE,
        } <= facets


@pytest.mark.asyncio(loop_scope="module")
async def test_deliverable_file_names_survive_parsing(kb_container):
    texts = [
        c.text
        for c in kb_container.store.chunks.values()
        if c.facet == Facet.TRACK_DELIVERABLES and c.track == "DA"
    ]
    assert any("V2E_DA_[Surname]_D2_DataQualityLog_v1.xlsx" in t for t in texts)


@pytest.mark.asyncio(loop_scope="module")
async def test_brand_voice_and_market_coverage(kb_container):
    stats = await kb_container.store.stats()
    assert stats.by_facet.get("brand_voice", 0) >= 10
    for family in ("cybersecurity", "data_analyst", "data_engineer", "software_developer"):
        assert stats.by_role_family.get(family, 0) > 300


@pytest.mark.asyncio(loop_scope="module")
async def test_golden_retrieval_quality_offline(kb_container):
    result = await run_retrieval_eval(kb_container, ROOT / "eval" / "retrieval_golden.yaml", k=5)
    assert result["summary"]["hit_rate@5"] >= 0.85, result["summary"]
    assert result["summary"]["mrr"] >= 0.7


@pytest.mark.asyncio(loop_scope="module")
async def test_reingestion_is_idempotent(kb_container):
    report = await kb_container.ingestion_pipeline().ingest_directory(Path(KB_DIR))
    assert report.totals["unchanged"] == report.totals["files"]

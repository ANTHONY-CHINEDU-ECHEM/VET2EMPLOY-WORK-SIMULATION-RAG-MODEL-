"""Filters, the in memory store, hybrid fusion, reranking, planning and evidence assembly."""

from __future__ import annotations

import pytest

from tests.conftest import CORPUS, ROOT, load_corpus, make_chunk
from v2e_rag.domain.catalog import load_catalog
from v2e_rag.domain.models import SearchHit
from v2e_rag.domain.taxonomy import Facet, TaskType
from v2e_rag.embeddings.base import HashingEmbedder
from v2e_rag.generation.requests import ManagerReplyRequest, RetrieveRequest, TaskRequest
from v2e_rag.retrieval.evidence import EvidenceBuilder, InsufficientEvidenceError
from v2e_rag.retrieval.hybrid import HybridRetriever, reciprocal_rank_fusion, rerank
from v2e_rag.retrieval.planner import RetrievalPlanner
from v2e_rag.store.base import ScoredChunk, SearchFilters
from v2e_rag.store.lexical import BM25Index, tokenize
from v2e_rag.store.memory_store import MemoryStore

CATALOG = load_catalog(str(ROOT / "config"))


@pytest.fixture
async def store() -> MemoryStore:
    s = MemoryStore()
    await load_corpus(s, HashingEmbedder(256))
    return s


def test_tokenize_keeps_digits_and_stems():
    assert tokenize("Activity 3: profiling datasets") == ["activity", "3", "profil", "dataset"]


def test_bm25_prefers_matching_documents():
    index = BM25Index()
    index.add("a", "power bi dashboards for finance")
    index.add("b", "incident response and phishing")
    scores = index.score("power bi")
    assert scores["a"] > 0 and "b" not in scores
    index.remove("a")
    assert index.score("power bi") == {}


def test_filters_semantics():
    activity = make_chunk(CORPUS[0])
    norms = make_chunk(CORPUS[6])
    assert SearchFilters(tracks=("DA",)).matches(activity)
    assert not SearchFilters(tracks=("DA",)).matches(norms)
    assert SearchFilters(tracks=("DA",), include_untracked=True).matches(norms)
    assert SearchFilters(only_untracked=True).matches(norms) and not SearchFilters(
        only_untracked=True
    ).matches(activity)
    assert SearchFilters(activity_numbers=(3,)).matches(activity)
    assert not SearchFilters(activity_numbers=(4,)).matches(activity)
    assert SearchFilters(
        role_families=("data_analyst",), include_untagged_role_family=True
    ).matches(norms)
    assert not SearchFilters(role_families=("cybersecurity",)).matches(activity)


async def test_memory_store_roundtrip(store, tmp_path):
    stats = await store.stats()
    assert stats.chunks == len(CORPUS) and stats.by_track["DA"] == 6
    store.save(tmp_path / "snap")
    restored = MemoryStore()
    restored.load(tmp_path / "snap")
    assert (await restored.stats()).chunks == stats.chunks
    hits = await restored.lexical_search("Data Quality Log", SearchFilters(tracks=("DA",)), 3)
    assert hits and hits[0].chunk.track == "DA"
    await restored.delete_document("doc_programme_brief")
    assert all(c.source_type.value != "programme_brief" for c in restored.chunks.values())


def test_rrf_and_rerank_diversity():
    a, b, c = (make_chunk(CORPUS[i]) for i in (7, 8, 10))  # same employer for a, b, c
    fused = reciprocal_rank_fusion(
        [ScoredChunk(a, 0.9), ScoredChunk(b, 0.8)], [ScoredChunk(b, 5.0), ScoredChunk(c, 1.0)]
    )
    assert fused[0].chunk.chunk_id == "market_tech"  # appears in both channels
    assert fused[0].semantic_rank == 2 and fused[0].lexical_rank == 1
    selected = rerank(fused, "power bi", limit=5)
    companies = [h.chunk.company for h in selected]
    assert companies.count("Example Consulting") <= 2  # employer cap keeps evidence diverse


def test_rerank_drops_exact_duplicates():
    a = make_chunk(CORPUS[0])
    b = a.model_copy(update={"chunk_id": "copy"})
    hits = [SearchHit(chunk=a, fused_score=0.5), SearchHit(chunk=b, fused_score=0.4)]
    assert len(rerank(hits, "profile", 5)) == 1


def test_task_plan_structure():
    plan = RetrievalPlanner(CATALOG).plan_task(
        TaskRequest(track="DA", activity_number=3, sector="consulting", job_level="mid_level")
    )
    names = [s.name for s in plan.steps]
    assert names[:6] == [
        "activity_definition",
        "deliverables",
        "client_context",
        "schedule",
        "supervisor_voice",
        "rubric",
    ]
    activity = plan.steps[0]
    assert (
        activity.required
        and activity.filters.activity_numbers == (3,)
        and activity.filters.tracks == ("DA",)
    )
    market = next(s for s in plan.steps if s.name == "market_responsibilities")
    assert market.filters.sectors == ("consulting",) and len(market.relaxations) == 2
    complication = RetrievalPlanner(CATALOG).plan_task(
        TaskRequest(track="CY", task_type=TaskType.COMPLICATION)
    )
    assert any(s.name == "planted_issues" and s.required for s in complication.steps)


def test_market_only_plan_requires_market_evidence():
    plan = RetrievalPlanner(CATALOG).plan_task(TaskRequest(role_family="data_engineer"))
    required = {s.name for s in plan.steps if s.required}
    assert required == {"programme_norms", "market_responsibilities"}
    assert plan.notes


async def test_evidence_builder_covers_every_purpose(store):
    builder = EvidenceBuilder(
        HybridRetriever(store, HashingEmbedder(256), min_semantic_similarity=0.0), 4000, 20
    )
    plan = RetrievalPlanner(CATALOG).plan_task(
        TaskRequest(track="DA", activity_number=3, sector="consulting", job_level="mid_level")
    )
    pack, _ = await builder.execute(plan)
    purposes = {i.retrieval_purpose for i in pack.items}
    assert {
        "activity_definition",
        "deliverables",
        "client_context",
        "brand_voice",
        "market_tools",
    } <= purposes
    assert [i.evidence_id for i in pack.items] == [f"E{n}" for n in range(1, len(pack.items) + 1)]
    assert len({i.chunk_id for i in pack.items}) == len(pack.items)
    assert pack.items[0].facet == Facet.TRACK_ACTIVITY  # required evidence is admitted first
    rendered = pack.render()
    assert rendered.startswith("[E1] (programme_brief | track_activity | ")


async def test_evidence_budget_is_enforced(store):
    builder = EvidenceBuilder(
        HybridRetriever(store, HashingEmbedder(256), min_semantic_similarity=0.0), 120, 20
    )
    pack, _ = await builder.execute(
        RetrievalPlanner(CATALOG).plan_task(TaskRequest(track="DA", activity_number=3))
    )
    assert 1 <= len(pack.items) < 5


async def test_relaxation_ladder_used_when_sector_has_no_evidence(store):
    builder = EvidenceBuilder(
        HybridRetriever(store, HashingEmbedder(256), min_semantic_similarity=0.0)
    )
    _, results = await builder.execute(
        RetrievalPlanner(CATALOG).plan_task(
            TaskRequest(track="DA", activity_number=3, sector="healthcare")
        )
    )
    tools = next(r for r in results if r.step.name == "market_tools")
    assert tools.relaxation_level == 1 and tools.hits


async def test_missing_required_evidence_raises(store):
    builder = EvidenceBuilder(HybridRetriever(store, HashingEmbedder(256)))
    with pytest.raises(InsufficientEvidenceError) as info:
        await builder.execute(
            RetrievalPlanner(CATALOG).plan_task(TaskRequest(track="DE", activity_number=1))
        )
    assert set(info.value.missing_steps) >= {"activity_definition", "client_context"}


def test_retrieve_and_reply_plans_exclude_engineering_reference():
    planner = RetrievalPlanner(CATALOG)
    plan = planner.plan_retrieve(RetrieveRequest(query="trust score", track="DA", limit=5))
    programme, market = plan.steps
    assert "engineering_reference" not in programme.filters.source_types
    assert programme.limit == 3 and market.limit == 2
    assert programme.filters.tracks == ("DA",) and programme.filters.include_untracked
    assert market.filters.role_families == ("data_analyst",)  # the track scopes market evidence
    explicit = planner.plan_retrieve(RetrieveRequest(query="tools", source_types=["job_market"]))
    assert len(explicit.steps) == 1
    assert explicit.steps[0].filters.exclude_source_types == ("engineering_reference",)
    reply = planner.plan_manager_reply(
        ManagerReplyRequest(track="DA", candidate_message="When is D2 due?")
    )
    assert reply.steps[0].apply_similarity_threshold and reply.steps[0].filters.include_untracked

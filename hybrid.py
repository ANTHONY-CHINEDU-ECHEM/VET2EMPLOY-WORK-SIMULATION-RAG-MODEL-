"""Hybrid retrieval: semantic and lexical channels fused with Reciprocal Rank Fusion,
then reranked for authority, query coverage and diversity."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from v2e_rag.domain.models import Chunk, SearchHit
from v2e_rag.embeddings.base import Embedder
from v2e_rag.observability.metrics import RETRIEVAL_LATENCY
from v2e_rag.store.base import ScoredChunk, SearchFilters, VectorStore
from v2e_rag.store.lexical import tokenize


def reciprocal_rank_fusion(
    semantic: list[ScoredChunk],
    lexical: list[ScoredChunk],
    k: int = 60,
    semantic_weight: float = 1.0,
    lexical_weight: float = 1.0,
) -> list[SearchHit]:
    """Fuse two ranked lists. RRF is robust to the different score scales of each channel."""
    hits: dict[str, SearchHit] = {}
    for rank, item in enumerate(semantic, start=1):
        hit = hits.setdefault(item.chunk.chunk_id, SearchHit(chunk=item.chunk))
        hit.semantic_similarity = item.score
        hit.semantic_rank = rank
        hit.fused_score += semantic_weight / (k + rank)
    for rank, item in enumerate(lexical, start=1):
        hit = hits.setdefault(item.chunk.chunk_id, SearchHit(chunk=item.chunk))
        hit.lexical_score = item.score
        hit.lexical_rank = rank
        hit.fused_score += lexical_weight / (k + rank)
    return sorted(hits.values(), key=lambda h: h.fused_score, reverse=True)


def _token_set(chunk: Chunk) -> set[str]:
    return set(tokenize(chunk.body))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class RerankConfig:
    fused_weight: float = 0.65
    authority_weight: float = 0.15
    coverage_weight: float = 0.20
    near_duplicate_threshold: float = 0.85
    max_per_company: int = 2


def rerank(
    hits: list[SearchHit], query: str, limit: int, config: RerankConfig | None = None
) -> list[SearchHit]:
    """Score hits on fused relevance, source authority and query term coverage, then select
    a diverse top set (no near duplicates, a cap per employer)."""
    if not hits:
        return []
    cfg = config or RerankConfig()
    max_fused = max(h.fused_score for h in hits) or 1.0
    query_terms = set(tokenize(query))
    for hit in hits:
        authority_score = (6 - hit.chunk.authority) / 5
        coverage = (
            len(query_terms & set(tokenize(hit.chunk.text))) / len(query_terms)
            if query_terms
            else 0.0
        )
        hit.final_score = (
            cfg.fused_weight * (hit.fused_score / max_fused)
            + cfg.authority_weight * authority_score
            + cfg.coverage_weight * coverage
        )
    ordered = sorted(hits, key=lambda h: h.final_score, reverse=True)
    selected: list[SearchHit] = []
    token_sets: list[set[str]] = []
    per_company: dict[str, int] = {}
    seen_hashes: set[str] = set()
    for hit in ordered:
        if len(selected) >= limit:
            break
        content_hash = hit.chunk.content_hash
        if content_hash in seen_hashes:
            continue
        tokens = _token_set(hit.chunk)
        if any(jaccard(tokens, other) >= cfg.near_duplicate_threshold for other in token_sets):
            continue
        company = hit.chunk.company_key
        if company and per_company.get(company, 0) >= cfg.max_per_company:
            continue
        selected.append(hit)
        token_sets.append(tokens)
        seen_hashes.add(content_hash)
        if company:
            per_company[company] = per_company.get(company, 0) + 1
    return selected


class HybridRetriever:
    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        candidates_per_channel: int = 40,
        rrf_k: int = 60,
        min_semantic_similarity: float = 0.2,
        rerank_config: RerankConfig | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.candidates = candidates_per_channel
        self.rrf_k = rrf_k
        self.min_similarity = min_semantic_similarity
        self.rerank_config = rerank_config or RerankConfig()

    async def search(
        self,
        query: str,
        filters: SearchFilters,
        limit: int,
        apply_similarity_threshold: bool = False,
        query_vector: list[float] | None = None,
    ) -> list[SearchHit]:
        started = time.perf_counter()
        vector = (
            query_vector if query_vector is not None else await self.embedder.embed_query(query)
        )
        semantic, lexical = await asyncio.gather(
            self.store.semantic_search(vector, filters, self.candidates),
            self.store.lexical_search(query, filters, self.candidates),
        )
        if apply_similarity_threshold:
            semantic = [s for s in semantic if s.score >= self.min_similarity]
        fused = reciprocal_rank_fusion(semantic, lexical, k=self.rrf_k)
        if apply_similarity_threshold:
            # A lexical only hit must still share real vocabulary with the query.
            fused = [
                h for h in fused if h.semantic_rank is not None or (h.lexical_score or 0) > 0.05
            ]
        result = rerank(fused, query, limit, self.rerank_config)
        RETRIEVAL_LATENCY.observe(time.perf_counter() - started)
        return result

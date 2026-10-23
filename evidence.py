"""Execute a retrieval plan and assemble a citable, budgeted evidence pack."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from v2e_rag.domain.models import EvidenceItem, EvidencePack, SearchHit, estimate_tokens
from v2e_rag.retrieval.hybrid import HybridRetriever
from v2e_rag.retrieval.planner import RetrievalPlan, RetrievalStep


class InsufficientEvidenceError(Exception):
    """Raised when required evidence is missing. The service abstains instead of guessing."""

    def __init__(self, message: str, missing_steps: list[str], diagnostics: dict[str, Any]) -> None:
        super().__init__(message)
        self.missing_steps = missing_steps
        self.diagnostics = diagnostics


@dataclass
class StepResult:
    step: RetrievalStep
    hits: list[SearchHit]
    relaxation_level: int = 0
    notes: list[str] = field(default_factory=list)


class EvidenceBuilder:
    def __init__(
        self, retriever: HybridRetriever, token_budget: int = 7000, max_items: int = 24
    ) -> None:
        self.retriever = retriever
        self.token_budget = token_budget
        self.max_items = max_items

    async def _run_step(self, step: RetrievalStep, query_vector: list[float] | None) -> StepResult:
        ladder = [step.filters, *step.relaxations]
        hits: list[SearchHit] = []
        used_level = 0
        for level, filters in enumerate(ladder):
            used_level = level
            hits = await self.retriever.search(
                step.query,
                filters,
                step.limit,
                apply_similarity_threshold=step.apply_similarity_threshold,
                query_vector=query_vector,
            )
            if len(hits) >= step.min_hits:
                break
        result = StepResult(step=step, hits=hits, relaxation_level=used_level)
        if used_level > 0:
            result.notes.append(
                f"{step.name}: filters relaxed {used_level} time(s) to find enough evidence"
            )
        return result

    async def execute(self, plan: RetrievalPlan) -> tuple[EvidencePack, list[StepResult]]:
        embedder = self.retriever.embedder
        unique_queries = list(dict.fromkeys(step.query for step in plan.steps))
        vectors = await asyncio.gather(*(embedder.embed_query(q) for q in unique_queries))
        by_query = dict(zip(unique_queries, vectors, strict=True))
        results = await asyncio.gather(*(self._run_step(s, by_query[s.query]) for s in plan.steps))

        missing = [r.step.name for r in results if r.step.required and not r.hits]
        diagnostics: dict[str, Any] = {
            "operation": plan.operation,
            "notes": [*plan.notes, *(n for r in results for n in r.notes)],
            "steps": [
                {
                    "name": r.step.name,
                    "purpose": r.step.purpose,
                    "hits": len(r.hits),
                    "required": r.step.required,
                    "relaxation_level": r.relaxation_level,
                }
                for r in results
            ],
        }
        if missing:
            raise InsufficientEvidenceError(
                "The knowledge base does not contain enough evidence for: " + ", ".join(missing),
                missing,
                diagnostics,
            )
        pack = self.assemble(results)
        pack.diagnostics = diagnostics | {
            "evidence_tokens": pack.total_tokens(),
            "evidence_items": len(pack.items),
        }
        return pack, list(results)

    def assemble(self, results: list[StepResult]) -> EvidencePack:
        """Admit hits round robin across steps (so every purpose is represented), dedupe by
        chunk and content, and stop at the token budget. Required steps are admitted first."""
        ordered = sorted(results, key=lambda r: (not r.step.required,))
        queues = [list(r.hits) for r in ordered]
        purposes = [r.step.name for r in ordered]
        items: list[EvidenceItem] = []
        seen_chunks: set[str] = set()
        seen_hashes: set[str] = set()
        used_tokens = 0
        progressed = True
        while progressed and len(items) < self.max_items:
            progressed = False
            for queue, purpose in zip(queues, purposes, strict=True):
                while queue:
                    hit = queue.pop(0)
                    chunk = hit.chunk
                    if chunk.chunk_id in seen_chunks or chunk.content_hash in seen_hashes:
                        continue
                    cost = estimate_tokens(chunk.text)
                    if used_tokens + cost > self.token_budget and items:
                        queue.clear()
                        break
                    seen_chunks.add(chunk.chunk_id)
                    seen_hashes.add(chunk.content_hash)
                    used_tokens += cost
                    items.append(
                        EvidenceItem(
                            evidence_id=f"E{len(items) + 1}",
                            chunk_id=chunk.chunk_id,
                            facet=chunk.facet,
                            source_type=chunk.source_type,
                            authority=chunk.authority,
                            citation=chunk.citation_label(),
                            text=chunk.text,
                            source_url=chunk.source_url,
                            score=round(hit.final_score, 4),
                            retrieval_purpose=purpose,
                        )
                    )
                    progressed = True
                    break
                if len(items) >= self.max_items:
                    break
        return EvidencePack(items=items)

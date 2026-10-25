"""Retrieval evaluation against a golden set of questions with expected evidence.

Relevance is defined by metadata criteria (track, facet, section, company, activity, text)
rather than chunk ids, so the golden set survives re-chunking and re-ingestion.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

import yaml

from v2e_rag.domain.models import EvidenceItem
from v2e_rag.generation.requests import RetrieveRequest


def _matches(item: EvidenceItem, chunk_meta: dict[str, Any], criterion: dict[str, Any]) -> bool:
    text = item.text.lower()
    for key, expected in criterion.items():
        if key == "source_type" and item.source_type.value != expected:
            return False
        if key == "facet" and item.facet.value not in (
            [expected] if isinstance(expected, str) else expected
        ):
            return False
        if key == "track" and chunk_meta.get("track") != expected:
            return False
        if (
            key == "section_contains"
            and str(expected).lower() not in (chunk_meta.get("section_path") or "").lower()
        ):
            return False
        if (
            key == "company"
            and str(expected).lower() not in (chunk_meta.get("company") or "").lower()
        ):
            return False
        if key == "activity_number" and chunk_meta.get("activity_number") != expected:
            return False
        if key == "text_contains":
            needles = [expected] if isinstance(expected, str) else expected
            if not all(n.lower() in text for n in needles):
                return False
    return True


async def run_retrieval_eval(container: Any, dataset: Path, k: int = 5) -> dict[str, Any]:
    text = await asyncio.to_thread(dataset.read_text, encoding="utf-8")
    cases = yaml.safe_load(text)["cases"]
    results = []
    for case in cases:
        request = RetrieveRequest(query=case["query"], limit=k, **case.get("filters", {}))
        pack, _ = await container.service.retrieve(request)
        chunks = {
            c.chunk_id: c
            for c in await container.store.get_chunks([i.chunk_id for i in pack.items])
        }
        relevance = []
        for item in pack.items[:k]:
            chunk = chunks.get(item.chunk_id)
            meta = {
                "track": chunk.track if chunk else None,
                "section_path": chunk.section_path if chunk else None,
                "company": chunk.company if chunk else None,
                "activity_number": chunk.metadata.get("activity_number") if chunk else None,
            }
            relevance.append(int(any(_matches(item, meta, c) for c in case["expected"])))
        first = next((i for i, r in enumerate(relevance) if r), None)
        dcg = sum(r / math.log2(i + 2) for i, r in enumerate(relevance))
        ideal = sum(1 / math.log2(i + 2) for i in range(min(sum(relevance) or 1, k)))
        results.append(
            {
                "id": case["id"],
                "query": case["query"],
                "hit": first is not None,
                "reciprocal_rank": 1 / (first + 1) if first is not None else 0.0,
                "precision": sum(relevance) / k,
                "ndcg": dcg / ideal if ideal else 0.0,
                "top_citations": [i.citation for i in pack.items[:3]],
            }
        )
    n = len(results) or 1
    summary = {
        "cases": len(results),
        "k": k,
        f"hit_rate@{k}": round(sum(r["hit"] for r in results) / n, 3),
        "mrr": round(sum(r["reciprocal_rank"] for r in results) / n, 3),
        f"precision@{k}": round(sum(r["precision"] for r in results) / n, 3),
        f"ndcg@{k}": round(sum(r["ndcg"] for r in results) / n, 3),
        "misses": [r["id"] for r in results if not r["hit"]],
        "embedding_model": container.embedder.model,
    }
    return {"summary": summary, "results": results}

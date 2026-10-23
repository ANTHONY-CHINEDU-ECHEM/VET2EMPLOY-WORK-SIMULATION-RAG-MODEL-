"""In memory vector store for tests, demos and offline development.

Implements the same contract as the pgvector backend. State can be saved to and loaded
from a snapshot directory so that ``v2e ingest`` and ``v2e serve`` can share data without
a database. Not intended for production traffic.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from v2e_rag.domain.models import Chunk, RoleRecord, SourceDocument
from v2e_rag.store.base import ScoredChunk, SearchFilters, StoreStats
from v2e_rag.store.lexical import BM25Index


class MemoryStore:
    def __init__(self, snapshot_path: Path | None = None, audit_path: Path | None = None) -> None:
        self.snapshot_path = snapshot_path
        self.audit_path = audit_path
        self.documents: dict[str, SourceDocument] = {}
        self.chunks: dict[str, Chunk] = {}
        self.vectors: dict[str, np.ndarray] = {}
        self.records: dict[str, RoleRecord] = {}
        self.embedding_cache: dict[tuple[str, str], list[float]] = {}
        self.embedding_model: str | None = None
        self.bm25 = BM25Index()
        self.audit: list[dict[str, Any]] = []
        self.ingestion_runs: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    # Lifecycle ---------------------------------------------------------------------------

    async def open(self) -> None:
        if self.snapshot_path and (self.snapshot_path / "chunks.jsonl").exists():
            self.load(self.snapshot_path)

    async def close(self) -> None:
        return None

    async def health(self) -> dict[str, Any]:
        return {"backend": "memory", "ok": True, "chunks": len(self.chunks)}

    # Documents ---------------------------------------------------------------------------

    async def get_document(self, document_id: str) -> SourceDocument | None:
        return self.documents.get(document_id)

    async def list_documents(self) -> list[SourceDocument]:
        return sorted(self.documents.values(), key=lambda d: d.source_file)

    async def replace_document(
        self,
        document: SourceDocument,
        chunks: list[Chunk],
        embeddings: list[list[float]],
        records: list[RoleRecord],
        embedding_model: str,
    ) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings must have the same length")
        async with self._lock:
            self._delete(document.document_id)
            self.documents[document.document_id] = document
            for chunk, vector in zip(chunks, embeddings, strict=True):
                self.chunks[chunk.chunk_id] = chunk
                self.vectors[chunk.chunk_id] = self._normalise(vector)
                self.bm25.add(chunk.chunk_id, chunk.text)
            for record in records:
                self.records[record.record_id] = record
            self.embedding_model = embedding_model

    async def delete_document(self, document_id: str) -> None:
        async with self._lock:
            self._delete(document_id)

    def _delete(self, document_id: str) -> None:
        self.documents.pop(document_id, None)
        for chunk_id in [cid for cid, c in self.chunks.items() if c.document_id == document_id]:
            self.chunks.pop(chunk_id, None)
            self.vectors.pop(chunk_id, None)
            self.bm25.remove(chunk_id)
        for record_id in [rid for rid, r in self.records.items() if r.document_id == document_id]:
            self.records.pop(record_id, None)

    # Search ------------------------------------------------------------------------------

    @staticmethod
    def _normalise(vector: list[float] | np.ndarray) -> np.ndarray:
        arr = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(arr))
        return arr / norm if norm > 0 else arr

    def _candidates(self, filters: SearchFilters) -> list[str]:
        return [cid for cid, chunk in self.chunks.items() if filters.matches(chunk)]

    async def semantic_search(
        self, embedding: list[float], filters: SearchFilters, limit: int
    ) -> list[ScoredChunk]:
        ids = self._candidates(filters)
        if not ids:
            return []
        query = self._normalise(embedding)
        matrix = np.stack([self.vectors[cid] for cid in ids])
        sims = matrix @ query
        order = np.argsort(-sims)[:limit]
        return [ScoredChunk(self.chunks[ids[i]], float(sims[i])) for i in order]

    async def lexical_search(
        self, query: str, filters: SearchFilters, limit: int
    ) -> list[ScoredChunk]:
        ids = set(self._candidates(filters))
        scores = self.bm25.score(query, ids)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [ScoredChunk(self.chunks[cid], score) for cid, score in ranked]

    async def get_chunks(self, chunk_ids: list[str]) -> list[Chunk]:
        return [self.chunks[cid] for cid in chunk_ids if cid in self.chunks]

    async def role_records(
        self,
        role_families: tuple[str, ...] = (),
        sectors: tuple[str, ...] = (),
        job_levels: tuple[str, ...] = (),
    ) -> list[RoleRecord]:
        out = []
        for record in self.records.values():
            if role_families and not set(record.role_families) & set(role_families):
                continue
            if sectors and not set(record.sectors) & set(sectors):
                continue
            if job_levels and (
                record.job_level is None or record.job_level.value not in job_levels
            ):
                continue
            out.append(record)
        return out

    async def stats(self) -> StoreStats:
        chunks = list(self.chunks.values())
        families: Counter[str] = Counter()
        sectors: Counter[str] = Counter()
        for chunk in chunks:
            families.update(chunk.role_families)
            sectors.update(chunk.sectors)
        dims = len(next(iter(self.vectors.values()))) if self.vectors else None
        return StoreStats(
            documents=len(self.documents),
            chunks=len(chunks),
            role_records=len(self.records),
            by_source_type=dict(Counter(c.source_type.value for c in chunks)),
            by_facet=dict(Counter(c.facet.value for c in chunks)),
            by_role_family=dict(families),
            by_sector=dict(sectors),
            by_track=dict(Counter(c.track for c in chunks if c.track)),
            embedding_model=self.embedding_model,
            embedding_dimensions=dims,
        )

    # Embedding cache, audit --------------------------------------------------------------

    async def cached_embeddings(
        self, model: str, content_hashes: list[str]
    ) -> dict[str, list[float]]:
        return {
            h: self.embedding_cache[(model, h)]
            for h in content_hashes
            if (model, h) in self.embedding_cache
        }

    async def cache_embeddings(self, model: str, items: dict[str, list[float]]) -> None:
        for content_hash, vector in items.items():
            self.embedding_cache[(model, content_hash)] = list(vector)

    async def record_audit(self, entry: dict[str, Any]) -> None:
        self.audit.append(entry)
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")

    async def record_ingestion_run(self, entry: dict[str, Any]) -> None:
        self.ingestion_runs.append(entry)

    # Snapshot persistence ----------------------------------------------------------------

    def save(self, path: Path | None = None) -> Path:
        target = path or self.snapshot_path
        if target is None:
            raise ValueError("No snapshot path configured")
        target.mkdir(parents=True, exist_ok=True)
        ids = list(self.chunks)
        with (target / "documents.jsonl").open("w", encoding="utf-8") as handle:
            for doc in self.documents.values():
                handle.write(doc.model_dump_json() + "\n")
        with (target / "chunks.jsonl").open("w", encoding="utf-8") as handle:
            for cid in ids:
                handle.write(self.chunks[cid].model_dump_json() + "\n")
        with (target / "records.jsonl").open("w", encoding="utf-8") as handle:
            for record in self.records.values():
                handle.write(record.model_dump_json() + "\n")
        vectors = (
            np.stack([self.vectors[cid] for cid in ids])
            if ids
            else np.zeros((0, 0), dtype=np.float32)
        )
        np.save(target / "vectors.npy", vectors)
        (target / "meta.json").write_text(
            json.dumps({"embedding_model": self.embedding_model, "chunk_ids": ids}),
            encoding="utf-8",
        )
        return target

    def load(self, path: Path) -> None:
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        self.embedding_model = meta.get("embedding_model")
        self.documents = {}
        for line in (path / "documents.jsonl").read_text(encoding="utf-8").splitlines():
            doc = SourceDocument.model_validate_json(line)
            self.documents[doc.document_id] = doc
        self.chunks = {}
        self.bm25 = BM25Index()
        for line in (path / "chunks.jsonl").read_text(encoding="utf-8").splitlines():
            chunk = Chunk.model_validate_json(line)
            self.chunks[chunk.chunk_id] = chunk
            self.bm25.add(chunk.chunk_id, chunk.text)
        self.records = {}
        records_file = path / "records.jsonl"
        if records_file.exists():
            for line in records_file.read_text(encoding="utf-8").splitlines():
                record = RoleRecord.model_validate_json(line)
                self.records[record.record_id] = record
        vectors = np.load(path / "vectors.npy")
        self.vectors = {cid: vectors[i] for i, cid in enumerate(meta["chunk_ids"])}

"""Storage contract shared by the PostgreSQL (pgvector) and in memory backends."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from v2e_rag.domain.models import Chunk, RoleRecord, SourceDocument


@dataclass(frozen=True)
class SearchFilters:
    """Metadata filters applied inside both retrieval channels.

    Every list is an OR within the field and an AND across fields. Empty lists mean no
    constraint. ``role_families`` and ``sectors`` match when the chunk shares any value.
    ``include_untagged_role_family`` admits chunks that carry no role family at all, which
    is how programme wide rules sit alongside track specific evidence.
    """

    source_types: tuple[str, ...] = ()
    facets: tuple[str, ...] = ()
    role_families: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    job_levels: tuple[str, ...] = ()
    tracks: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()
    exclude_source_types: tuple[str, ...] = ()
    include_untagged_role_family: bool = False
    include_untracked: bool = False
    only_untracked: bool = False
    activity_numbers: tuple[int, ...] = ()

    def matches(self, chunk: Chunk) -> bool:
        if self.source_types and chunk.source_type.value not in self.source_types:
            return False
        if self.exclude_source_types and chunk.source_type.value in self.exclude_source_types:
            return False
        if self.facets and chunk.facet.value not in self.facets:
            return False
        if self.role_families:
            if chunk.role_families:
                if not set(chunk.role_families) & set(self.role_families):
                    return False
            elif not self.include_untagged_role_family:
                return False
        if self.sectors and not set(chunk.sectors) & set(self.sectors):
            return False
        if self.job_levels and (
            chunk.job_level is None or chunk.job_level.value not in self.job_levels
        ):
            return False
        if self.only_untracked and chunk.track is not None:
            return False
        if self.tracks:
            if chunk.track is None:
                if not self.include_untracked:
                    return False
            elif chunk.track not in self.tracks:
                return False
        if (
            self.activity_numbers
            and chunk.metadata.get("activity_number") not in self.activity_numbers
        ):
            return False
        return not (self.document_ids and chunk.document_id not in self.document_ids)


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float


@dataclass
class StoreStats:
    documents: int = 0
    chunks: int = 0
    role_records: int = 0
    by_source_type: dict[str, int] = field(default_factory=dict)
    by_facet: dict[str, int] = field(default_factory=dict)
    by_role_family: dict[str, int] = field(default_factory=dict)
    by_sector: dict[str, int] = field(default_factory=dict)
    by_track: dict[str, int] = field(default_factory=dict)
    embedding_model: str | None = None
    embedding_dimensions: int | None = None


class VectorStore(Protocol):
    """Persistence and search operations required by ingestion and retrieval."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def health(self) -> dict[str, Any]: ...

    async def get_document(self, document_id: str) -> SourceDocument | None: ...

    async def list_documents(self) -> list[SourceDocument]: ...

    async def replace_document(
        self,
        document: SourceDocument,
        chunks: list[Chunk],
        embeddings: list[list[float]],
        records: list[RoleRecord],
        embedding_model: str,
    ) -> None: ...

    async def delete_document(self, document_id: str) -> bool:
        """Delete a document with its chunks and records. Returns whether it existed."""
        ...

    async def semantic_search(
        self, embedding: list[float], filters: SearchFilters, limit: int
    ) -> list[ScoredChunk]: ...

    async def lexical_search(
        self, query: str, filters: SearchFilters, limit: int
    ) -> list[ScoredChunk]: ...

    async def get_chunks(self, chunk_ids: list[str]) -> list[Chunk]: ...

    async def role_records(
        self,
        role_families: tuple[str, ...] = (),
        sectors: tuple[str, ...] = (),
        job_levels: tuple[str, ...] = (),
    ) -> list[RoleRecord]: ...

    async def stats(self) -> StoreStats: ...

    async def cached_embeddings(
        self, model: str, content_hashes: list[str]
    ) -> dict[str, list[float]]: ...

    async def cache_embeddings(self, model: str, items: dict[str, list[float]]) -> None: ...

    async def record_audit(self, entry: dict[str, Any]) -> None: ...

    async def get_audit(self, audit_id: str) -> dict[str, Any] | None: ...

    async def purge_audit(self, before: datetime) -> int:
        """Delete audit records created before ``before``. Returns the number deleted."""
        ...

    async def record_ingestion_run(self, entry: dict[str, Any]) -> None: ...

    def ingestion_lock(self) -> AbstractAsyncContextManager[None]:
        """Exclusive lock across every process sharing the store, held for a whole
        ingestion run. Raises ``IngestionLockedError`` when another run holds it."""
        ...

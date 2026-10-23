"""PostgreSQL + pgvector backend (production default, AlloyDB compatible).

Semantic search uses an HNSW cosine index. When a filtered search returns fewer rows than
requested (a known property of approximate indexes under selective filters), the query is
re-run as an exact scan inside the same transaction so recall never silently drops.
Lexical search uses PostgreSQL full text search with header terms weighted above body terms.
"""

from __future__ import annotations

import json
import re
from typing import Any

import numpy as np
from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from v2e_rag.domain.models import Chunk, RoleRecord, SourceDocument
from v2e_rag.domain.taxonomy import Facet, JobLevel, SourceType
from v2e_rag.observability.logging import get_logger
from v2e_rag.store.base import ScoredChunk, SearchFilters, StoreStats
from v2e_rag.store.lexical import tokenize

log = get_logger(__name__)

_CHUNK_COLUMNS = (
    "chunk_id, document_id, source_type, facet, authority, header, body, role_families, sectors, "
    "job_level, track, company, company_key, job_title, location, section_path, page_start, page_end, "
    "source_url, source_file, sheet, row_number, collected_on, metadata"
)


def _filter_sql(filters: SearchFilters, params: dict[str, Any]) -> str:
    clauses: list[str] = []
    if filters.source_types:
        clauses.append("source_type = ANY(%(f_source_types)s)")
        params["f_source_types"] = list(filters.source_types)
    if filters.exclude_source_types:
        clauses.append("NOT (source_type = ANY(%(f_exclude_source_types)s))")
        params["f_exclude_source_types"] = list(filters.exclude_source_types)
    if filters.facets:
        clauses.append("facet = ANY(%(f_facets)s)")
        params["f_facets"] = list(filters.facets)
    if filters.role_families:
        params["f_role_families"] = list(filters.role_families)
        if filters.include_untagged_role_family:
            clauses.append(
                "(role_families && %(f_role_families)s OR cardinality(role_families) = 0)"
            )
        else:
            clauses.append("role_families && %(f_role_families)s")
    if filters.sectors:
        clauses.append("sectors && %(f_sectors)s")
        params["f_sectors"] = list(filters.sectors)
    if filters.job_levels:
        clauses.append("job_level = ANY(%(f_job_levels)s)")
        params["f_job_levels"] = list(filters.job_levels)
    if filters.only_untracked:
        clauses.append("track IS NULL")
    if filters.tracks:
        params["f_tracks"] = list(filters.tracks)
        if filters.include_untracked:
            clauses.append("(track = ANY(%(f_tracks)s) OR track IS NULL)")
        else:
            clauses.append("track = ANY(%(f_tracks)s)")
    if filters.activity_numbers:
        clauses.append("(metadata->>'activity_number') = ANY(%(f_activities)s)")
        params["f_activities"] = [str(n) for n in filters.activity_numbers]
    if filters.document_ids:
        clauses.append("document_id = ANY(%(f_document_ids)s)")
        params["f_document_ids"] = list(filters.document_ids)
    return " AND ".join(clauses) if clauses else "TRUE"


def _row_to_chunk(row: dict[str, Any]) -> Chunk:
    return Chunk(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        source_type=SourceType(row["source_type"]),
        facet=Facet(row["facet"]),
        authority=row["authority"],
        header=row["header"],
        body=row["body"],
        role_families=list(row["role_families"] or []),
        sectors=list(row["sectors"] or []),
        job_level=JobLevel(row["job_level"]) if row["job_level"] else None,
        track=row["track"],
        company=row["company"],
        company_key=row["company_key"],
        job_title=row["job_title"],
        location=row["location"],
        section_path=row["section_path"],
        page_start=row["page_start"],
        page_end=row["page_end"],
        source_url=row["source_url"],
        source_file=row["source_file"],
        sheet=row["sheet"],
        row_number=row["row_number"],
        collected_on=row["collected_on"],
        metadata=row["metadata"] or {},
    )


def _vector_to_list(value: Any) -> list[float]:
    """pgvector returns numpy arrays for typed columns and Vector objects for untyped ones."""
    if hasattr(value, "to_list"):
        return [float(x) for x in value.to_list()]
    return np.asarray(value, dtype=np.float32).tolist()


def build_tsquery(query: str, max_terms: int = 32) -> str | None:
    """OR query over sanitised terms. Ranking (ts_rank_cd) rewards chunks matching more terms."""
    terms = list(dict.fromkeys(re.sub(r"[^a-z0-9]", "", t) for t in tokenize(query)))
    terms = [t for t in terms if len(t) > 1][:max_terms]
    return " | ".join(terms) if terms else None


class PgVectorStore:
    def __init__(
        self,
        database_url: str,
        min_size: int = 1,
        max_size: int = 10,
        ef_search: int = 80,
    ) -> None:
        self.database_url = database_url
        self.ef_search = ef_search
        self._iterative_scan_supported = False
        self.pool = AsyncConnectionPool(
            database_url,
            min_size=min_size,
            max_size=max_size,
            open=False,
            configure=self._configure,
            kwargs={"autocommit": False},
        )

    @staticmethod
    async def _configure(conn: AsyncConnection) -> None:
        await register_vector_async(conn)
        await conn.commit()

    async def open(self) -> None:
        await self.pool.open(wait=True, timeout=30)
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).fetchone()
            if row is None:
                raise RuntimeError("pgvector extension is missing. Run `v2e migrate` first.")
            major, minor = (int(x) for x in str(row[0]).split(".")[:2])
            self._iterative_scan_supported = (major, minor) >= (0, 8)
            log.info(
                "pgvector_ready", version=row[0], iterative_scan=self._iterative_scan_supported
            )

    async def close(self) -> None:
        await self.pool.close()

    async def health(self) -> dict[str, Any]:
        try:
            async with self.pool.connection() as conn:
                await conn.execute("SELECT 1")
            return {"backend": "pgvector", "ok": True, "pool": self.pool.get_stats()}
        except Exception as exc:  # pragma: no cover, depends on infrastructure
            return {"backend": "pgvector", "ok": False, "error": str(exc)}

    # Documents ---------------------------------------------------------------------------

    @staticmethod
    def _row_to_document(row: dict[str, Any]) -> SourceDocument:
        return SourceDocument(
            document_id=row["document_id"],
            source_file=row["source_file"],
            title=row["title"],
            source_type=SourceType(row["source_type"]),
            content_hash=row["content_hash"],
            collected_on=row["collected_on"],
            metadata=row["metadata"] or {},
        )

    async def get_document(self, document_id: str) -> SourceDocument | None:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute("SELECT * FROM kb_documents WHERE document_id = %s", (document_id,))
            row = await cur.fetchone()
        return self._row_to_document(row) if row else None

    async def list_documents(self) -> list[SourceDocument]:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute("SELECT * FROM kb_documents ORDER BY source_file")
            rows = await cur.fetchall()
        return [self._row_to_document(r) for r in rows]

    async def replace_document(
        self,
        document: SourceDocument,
        chunks: list[Chunk],
        embeddings: list[list[float]],
        records: list[RoleRecord],
        embedding_model: str,
    ) -> None:
        """Atomically replace a document, its chunks and its role records."""
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings must have the same length")
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM kb_documents WHERE document_id = %s", (document.document_id,)
            )
            await conn.execute(
                "INSERT INTO kb_documents (document_id, source_file, title, source_type, content_hash, "
                "collected_on, metadata, chunk_count, embedding_model) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    document.document_id,
                    document.source_file,
                    document.title,
                    document.source_type.value,
                    document.content_hash,
                    document.collected_on,
                    Jsonb(document.metadata),
                    len(chunks),
                    embedding_model,
                ),
            )
            if chunks:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        f"INSERT INTO kb_chunks ({_CHUNK_COLUMNS}, content_hash, token_count, embedding) "
                        "VALUES (" + ", ".join(["%s"] * 27) + ")",
                        [
                            (
                                c.chunk_id,
                                c.document_id,
                                c.source_type.value,
                                c.facet.value,
                                c.authority,
                                c.header,
                                c.body,
                                c.role_families,
                                c.sectors,
                                c.job_level.value if c.job_level else None,
                                c.track,
                                c.company,
                                c.company_key,
                                c.job_title,
                                c.location,
                                c.section_path,
                                c.page_start,
                                c.page_end,
                                c.source_url,
                                c.source_file,
                                c.sheet,
                                c.row_number,
                                c.collected_on,
                                Jsonb(c.metadata),
                                c.content_hash,
                                c.token_count,
                                np.asarray(vector, dtype=np.float32),
                            )
                            for c, vector in zip(chunks, embeddings, strict=True)
                        ],
                    )
            if records:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO kb_role_records (record_id, document_id, role_families, sectors, "
                        "job_level, company, company_key, job_title, record) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (record_id) DO UPDATE SET record = EXCLUDED.record",
                        [
                            (
                                r.record_id,
                                r.document_id,
                                r.role_families,
                                r.sectors,
                                r.job_level.value if r.job_level else None,
                                r.company,
                                r.company_key,
                                r.job_title,
                                Jsonb(json.loads(r.model_dump_json())),
                            )
                            for r in records
                        ],
                    )

    async def delete_document(self, document_id: str) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM kb_documents WHERE document_id = %s", (document_id,))

    # Search ------------------------------------------------------------------------------

    async def semantic_search(
        self, embedding: list[float], filters: SearchFilters, limit: int
    ) -> list[ScoredChunk]:
        params: dict[str, Any] = {"q": np.asarray(embedding, dtype=np.float32), "limit": limit}
        where = _filter_sql(filters, params)
        sql = (
            f"SELECT {_CHUNK_COLUMNS}, 1 - (embedding <=> %(q)s) AS similarity "
            f"FROM kb_chunks WHERE {where} ORDER BY embedding <=> %(q)s LIMIT %(limit)s"
        )
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL hnsw.ef_search = {int(max(self.ef_search, limit))}")
            if self._iterative_scan_supported:
                await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            if len(rows) < limit and where != "TRUE":
                # Approximate index under a selective filter may under-return. Fall back to
                # an exact scan for this query only.
                await conn.execute("SET LOCAL enable_indexscan = off")
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        return [ScoredChunk(_row_to_chunk(r), float(r["similarity"])) for r in rows]

    async def lexical_search(
        self, query: str, filters: SearchFilters, limit: int
    ) -> list[ScoredChunk]:
        tsquery = build_tsquery(query)
        if not tsquery:
            return []
        params: dict[str, Any] = {"tsq": tsquery, "limit": limit}
        where = _filter_sql(filters, params)
        sql = (
            f"SELECT {_CHUNK_COLUMNS}, ts_rank_cd(tsv, q, 32) AS rank "
            f"FROM kb_chunks, to_tsquery('english', %(tsq)s) AS q "
            f"WHERE tsv @@ q AND {where} ORDER BY rank DESC LIMIT %(limit)s"
        )
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(sql, params)
            rows = await cur.fetchall()
        return [ScoredChunk(_row_to_chunk(r), float(r["rank"])) for r in rows]

    async def get_chunks(self, chunk_ids: list[str]) -> list[Chunk]:
        if not chunk_ids:
            return []
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {_CHUNK_COLUMNS} FROM kb_chunks WHERE chunk_id = ANY(%s)", (chunk_ids,)
            )
            rows = await cur.fetchall()
        by_id = {r["chunk_id"]: _row_to_chunk(r) for r in rows}
        return [by_id[cid] for cid in chunk_ids if cid in by_id]

    async def role_records(
        self,
        role_families: tuple[str, ...] = (),
        sectors: tuple[str, ...] = (),
        job_levels: tuple[str, ...] = (),
    ) -> list[RoleRecord]:
        clauses, params = [], []
        if role_families:
            clauses.append("role_families && %s")
            params.append(list(role_families))
        if sectors:
            clauses.append("sectors && %s")
            params.append(list(sectors))
        if job_levels:
            clauses.append("job_level = ANY(%s)")
            params.append(list(job_levels))
        where = " AND ".join(clauses) if clauses else "TRUE"
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT record FROM kb_role_records WHERE {where}", params)
            rows = await cur.fetchall()
        return [RoleRecord.model_validate(r[0]) for r in rows]

    async def stats(self) -> StoreStats:
        async with self.pool.connection() as conn:

            async def scalar(sql: str) -> Any:
                return (await (await conn.execute(sql)).fetchone())[0]

            async def grouped(sql: str) -> dict[str, int]:
                return {
                    str(k): int(v)
                    for k, v in await (await conn.execute(sql)).fetchall()
                    if k is not None
                }

            stats = StoreStats(
                documents=await scalar("SELECT count(*) FROM kb_documents"),
                chunks=await scalar("SELECT count(*) FROM kb_chunks"),
                role_records=await scalar("SELECT count(*) FROM kb_role_records"),
                by_source_type=await grouped(
                    "SELECT source_type, count(*) FROM kb_chunks GROUP BY 1"
                ),
                by_facet=await grouped("SELECT facet, count(*) FROM kb_chunks GROUP BY 1"),
                by_role_family=await grouped(
                    "SELECT unnest(role_families), count(*) FROM kb_chunks GROUP BY 1"
                ),
                by_sector=await grouped(
                    "SELECT unnest(sectors), count(*) FROM kb_chunks GROUP BY 1"
                ),
                by_track=await grouped(
                    "SELECT track, count(*) FROM kb_chunks WHERE track IS NOT NULL GROUP BY 1"
                ),
            )
            row = await (
                await conn.execute(
                    "SELECT embedding_model FROM kb_documents ORDER BY ingested_at DESC LIMIT 1"
                )
            ).fetchone()
            stats.embedding_model = row[0] if row else None
            dims = await (
                await conn.execute("SELECT vector_dims(embedding) FROM kb_chunks LIMIT 1")
            ).fetchone()
            stats.embedding_dimensions = int(dims[0]) if dims else None
        return stats

    # Embedding cache, audit --------------------------------------------------------------

    async def cached_embeddings(
        self, model: str, content_hashes: list[str]
    ) -> dict[str, list[float]]:
        if not content_hashes:
            return {}
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT content_hash, embedding FROM embedding_cache WHERE model = %s AND content_hash = ANY(%s)",
                (model, content_hashes),
            )
            rows = await cur.fetchall()
        return {h: _vector_to_list(v) for h, v in rows}

    async def cache_embeddings(self, model: str, items: dict[str, list[float]]) -> None:
        if not items:
            return
        async with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO embedding_cache (model, content_hash, embedding) VALUES (%s, %s, %s) "
                "ON CONFLICT (model, content_hash) DO NOTHING",
                [(model, h, np.asarray(v, dtype=np.float32)) for h, v in items.items()],
            )

    async def record_audit(self, entry: dict[str, Any]) -> None:
        payload = json.loads(json.dumps(entry, default=str))
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO generation_audit (audit_id, operation, status, request, evidence, response, "
                "verification, model, latency_ms, usage) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    payload["audit_id"],
                    payload["operation"],
                    payload["status"],
                    Jsonb(payload.get("request", {})),
                    Jsonb(payload.get("evidence", [])),
                    Jsonb(payload.get("response")),
                    Jsonb(payload.get("verification")),
                    payload.get("model"),
                    payload.get("latency_ms"),
                    Jsonb(payload.get("usage")),
                ),
            )

    async def record_ingestion_run(self, entry: dict[str, Any]) -> None:
        payload = json.loads(json.dumps(entry, default=str))
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO ingestion_runs (run_id, started_at, finished_at, summary) VALUES (%s, %s, %s, %s)",
                (payload["run_id"], payload["started_at"], payload["finished_at"], Jsonb(payload)),
            )

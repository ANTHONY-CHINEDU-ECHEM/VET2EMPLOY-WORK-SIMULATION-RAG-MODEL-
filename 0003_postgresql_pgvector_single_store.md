# 0003: PostgreSQL with pgvector as the only store

Status: Accepted, October 2026

## Context

The platform already runs PostgreSQL (Supabase or AlloyDB) and the knowledge graph design uses `vector(1536)` embeddings. The service needs vector search, full text search, structured role records, an embedding cache and an audit trail.

## Decision

Keep everything in one PostgreSQL database with the pgvector extension: HNSW for vectors, a generated `tsvector` for full text, JSONB for records and audit payloads. Use advisory locks so concurrent migrations (several replicas starting together) and concurrent ingestion runs (CLI and API) are serialised across processes.

## Consequences

* One transactional store: a document, its chunks and its records are replaced atomically.
* No extra infrastructure to operate or secure.
* The embedding dimension is fixed in the schema; changing models means a new database or schema and a reingestion (see operations.md).
* Approximate search under selective filters can under return; the store falls back to an exact scan for that query, and pgvector 0.8 iterative scans are used when available.

-- Vet2Employ RAG service: initial schema.
--
-- {{EMBEDDING_DIM}} is replaced by the migration runner with V2E_EMBEDDING_DIMENSIONS
-- (1536 for openai/text-embedding-3-small, matching the platform artefacts table).
-- The schema is compatible with PostgreSQL 15+ and AlloyDB for PostgreSQL with pgvector.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS kb_documents (
    document_id      text PRIMARY KEY,
    source_file      text        NOT NULL,
    title            text        NOT NULL,
    source_type      text        NOT NULL,
    content_hash     text        NOT NULL,
    collected_on     date,
    metadata         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    chunk_count      integer     NOT NULL DEFAULT 0,
    embedding_model  text        NOT NULL,
    version          integer     NOT NULL DEFAULT 1,
    ingested_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    chunk_id       text PRIMARY KEY,
    document_id    text        NOT NULL REFERENCES kb_documents (document_id) ON DELETE CASCADE,
    source_type    text        NOT NULL,
    facet          text        NOT NULL,
    authority      smallint    NOT NULL,
    header         text        NOT NULL,
    body           text        NOT NULL,
    role_families  text[]      NOT NULL DEFAULT '{}',
    sectors        text[]      NOT NULL DEFAULT '{}',
    job_level      text,
    track          text,
    company        text,
    company_key    text,
    job_title      text,
    location       text,
    section_path   text,
    page_start     integer,
    page_end       integer,
    source_url     text,
    source_file    text        NOT NULL,
    sheet          text,
    row_number     integer,
    collected_on   date,
    metadata       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    content_hash   text        NOT NULL,
    token_count    integer     NOT NULL,
    embedding      vector({{EMBEDDING_DIM}}) NOT NULL,
    tsv            tsvector GENERATED ALWAYS AS (
                       setweight(to_tsvector('english', coalesce(header, '')), 'A') ||
                       setweight(to_tsvector('english', coalesce(body, '')), 'B')
                   ) STORED,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kb_chunks_embedding_hnsw
    ON kb_chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS kb_chunks_tsv_gin ON kb_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS kb_chunks_role_families_gin ON kb_chunks USING gin (role_families);
CREATE INDEX IF NOT EXISTS kb_chunks_sectors_gin ON kb_chunks USING gin (sectors);
CREATE INDEX IF NOT EXISTS kb_chunks_source_facet_idx ON kb_chunks (source_type, facet);
CREATE INDEX IF NOT EXISTS kb_chunks_track_idx ON kb_chunks (track);
CREATE INDEX IF NOT EXISTS kb_chunks_job_level_idx ON kb_chunks (job_level);
CREATE INDEX IF NOT EXISTS kb_chunks_document_idx ON kb_chunks (document_id);
CREATE INDEX IF NOT EXISTS kb_chunks_content_hash_idx ON kb_chunks (content_hash);

CREATE TABLE IF NOT EXISTS kb_role_records (
    record_id      text PRIMARY KEY,
    document_id    text        NOT NULL REFERENCES kb_documents (document_id) ON DELETE CASCADE,
    role_families  text[]      NOT NULL DEFAULT '{}',
    sectors        text[]      NOT NULL DEFAULT '{}',
    job_level      text,
    company        text,
    company_key    text,
    job_title      text,
    record         jsonb       NOT NULL
);

CREATE INDEX IF NOT EXISTS kb_role_records_families_gin ON kb_role_records USING gin (role_families);
CREATE INDEX IF NOT EXISTS kb_role_records_sectors_gin ON kb_role_records USING gin (sectors);
CREATE INDEX IF NOT EXISTS kb_role_records_company_idx ON kb_role_records (company_key);

-- Embeddings are cached by model and content hash so unchanged text is never re-embedded.
CREATE TABLE IF NOT EXISTS embedding_cache (
    model         text        NOT NULL,
    content_hash  text        NOT NULL,
    embedding     vector      NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model, content_hash)
);

-- Every generation, verification and retrieval for grounding is auditable end to end.
CREATE TABLE IF NOT EXISTS generation_audit (
    audit_id      uuid PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    operation     text        NOT NULL,
    status        text        NOT NULL,
    request       jsonb       NOT NULL,
    evidence      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    response      jsonb,
    verification  jsonb,
    model         text,
    latency_ms    integer,
    usage         jsonb
);

CREATE INDEX IF NOT EXISTS generation_audit_created_idx ON generation_audit (created_at DESC);
CREATE INDEX IF NOT EXISTS generation_audit_status_idx ON generation_audit (operation, status);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id        uuid PRIMARY KEY,
    started_at    timestamptz NOT NULL,
    finished_at   timestamptz NOT NULL,
    summary       jsonb       NOT NULL
);

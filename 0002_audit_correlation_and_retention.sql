-- Vet2Employ RAG service: audit correlation, retention support and an activity index.
--
-- * request_id ties an audit record to the X-Request-ID the platform sent and to every
--   log line of that request.
-- * prompt_version is promoted from the request payload to a column so outputs produced
--   by a given prompt revision can be found without scanning JSON.
-- * details holds structured context that is not part of the core record (for example
--   the safeguarding screen result or a model error summary).
-- * The created_at index already exists; retention purges use it (v2e audit purge).
-- * Activity number filters run on every task request, so they get an expression index.

ALTER TABLE generation_audit ADD COLUMN IF NOT EXISTS request_id     text;
ALTER TABLE generation_audit ADD COLUMN IF NOT EXISTS prompt_version text;
ALTER TABLE generation_audit ADD COLUMN IF NOT EXISTS details        jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS generation_audit_request_idx
    ON generation_audit (request_id) WHERE request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS kb_chunks_activity_idx
    ON kb_chunks ((metadata ->> 'activity_number'))
    WHERE metadata ? 'activity_number';

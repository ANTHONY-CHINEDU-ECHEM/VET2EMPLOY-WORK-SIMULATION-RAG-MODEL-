# Operations

## 1. Deployment

The service is one stateless container plus PostgreSQL with pgvector. Any number of API replicas can share the database.

### Google Cloud (matching the existing platform footprint)

| Concern | Recommendation |
| :--- | :--- |
| Compute | Cloud Run service from the repository `Dockerfile`; minimum one instance to avoid cold starts on the Task Engine path |
| Database | AlloyDB for PostgreSQL with the `vector` extension enabled, reached through the AlloyDB connector or private IP |
| Secrets | Secret Manager for `OPENROUTER_API_KEY`, `V2E_API_KEYS`, `V2E_ADMIN_API_KEYS` and the database password |
| Knowledge base files | A private Cloud Storage bucket mounted read only at `/app/knowledge_base/raw`, or a Cloud Run job that copies them before `v2e ingest` |
| Ingestion | A Cloud Run job running the same image with the command `ingest` |
| Networking | Private ingress from the platform only; the API is not intended for direct public access |

### Sizing

| Component | Starting point |
| :--- | :--- |
| API | 1 vCPU and 1 GiB per instance, `V2E_WORKERS=2`; scale on concurrency, since most time is spent waiting on OpenRouter |
| Database | 2 vCPU and 8 GiB comfortably holds the supplied knowledge base many times over (about 3,200 chunks at 1,536 dimensions) |
| Connection pool | `V2E_DB_POOL_MAX_SIZE` multiplied by the total worker count must stay below the database connection limit |

### Embedding model changes

The embedding dimension is fixed in the schema. To change model: create a new database (or schema), set `V2E_EMBEDDING_MODEL` and `V2E_EMBEDDING_DIMENSIONS`, run `v2e migrate` and `v2e ingest`, compare `v2e eval-retrieval` results, then switch traffic. The migration runner refuses to run against a database whose vector dimension differs from the configuration, and `/health/ready` reports a mismatch between the configured model and the stored vectors.

## 2. Monitoring

### Metrics (`/metrics`)

| Metric | Use |
| :--- | :--- |
| `v2e_http_requests_total{route,method,status}` | Traffic and error rates |
| `v2e_http_request_seconds{route}` | Latency; alert on p95 for `/v1/tasks/generate` |
| `v2e_retrieval_seconds` | Retrieval latency; should stay well under a second |
| `v2e_llm_requests_total{purpose,model,outcome}` | Model calls by purpose (task, repair, verification) and outcome |
| `v2e_llm_tokens_total{purpose,kind}` | Token consumption for cost tracking |
| `v2e_verification_outcomes_total{operation,verdict}` | Approved, pruned, abstained and rejected outputs |
| `v2e_unsupported_claims_total{operation,check}` | Which checks are catching unsupported content |
| `v2e_abstentions_total{operation}` | Requests the knowledge base could not support |
| `v2e_embedding_requests_total{model}` | Embedding batches |

### Suggested alerts

| Condition | Meaning |
| :--- | :--- |
| Rejected share above 5 percent over an hour | Prompt, model or knowledge base regression; inspect recent audit records |
| Any `verification_unavailable` responses | Judge model or OpenRouter degraded |
| Abstentions rising after a content change | A required section may have been renamed or removed from the brief |
| `/health/ready` failing | Database, knowledge base, OpenRouter key or embedding mismatch |

### Logs

Logs are JSON lines with `request_id`, `path`, status and duration. Model prompts and candidate messages are not logged; they are kept in the audit table, where retention can be controlled.

## 3. Audit queries

```sql
-- Recent outcomes by operation
SELECT operation, status, count(*)
FROM generation_audit
WHERE created_at > now() - interval '24 hours'
GROUP BY 1, 2 ORDER BY 1, 2;

-- Why was a response rejected?
SELECT verification -> 'violations' AS violations, evidence, response
FROM generation_audit WHERE audit_id = '<audit id from the response>';

-- Which checks catch the most problems this week?
SELECT v ->> 'check' AS check_name, count(*)
FROM generation_audit, jsonb_array_elements(verification -> 'violations') AS v
WHERE created_at > now() - interval '7 days'
GROUP BY 1 ORDER BY 2 DESC;

-- Ingestion history
SELECT run_id, finished_at, summary -> 'totals' FROM ingestion_runs ORDER BY finished_at DESC LIMIT 10;
```

Set a retention policy for `generation_audit` that matches the platform's data retention rules (for example, delete or archive rows older than the cohort retention period).

## 4. Runbooks

### Many `insufficient_evidence` responses

1. Read `details.missing` and `details.diagnostics.steps` from the response.
2. Run `v2e stats` and check the counts for the affected track and facet.
3. If a brief was revised, check that track headings still match `config/tracks.yaml` and that section names still match the facet rules in `config/knowledge_manifest.yaml`.
4. Reingest with `v2e ingest --force` and rerun `v2e eval-retrieval`.

### Rising `grounding_failed`

1. Query the audit table for the latest rejected records and group violations by check.
2. Entity or tool violations usually mean the model is adding outside knowledge: confirm `V2E_GENERATION_MODEL` and lower `V2E_GENERATION_TEMPERATURE`.
3. Entailment violations on message sentences usually mean the message is adding detail: review the prompt version in use.
4. Run `v2e eval-generation` before and after any change.

### OpenRouter degradation

The client retries with backoff and routes to fallback models. If failures persist, add fallbacks in `V2E_GENERATION_FALLBACK_MODELS` and `V2E_VERIFIER_FALLBACK_MODELS`. Do not disable verification to restore throughput; the platform should queue work and retry instead.

## 5. Security

* Platform and admin keys are separate and compared in constant time. Rotate keys by deploying the new key alongside the old one, switching the platform, then removing the old key.
* Personal data in candidate messages is redacted before any model call. Knowledge base material is synthetic or public job market data.
* OpenRouter routing defaults to `data_collection: deny`.
* Uploads are restricted to `.pdf`, `.docx` and `.xlsx`, capped at 50 MB, and written by base file name only.
* The container runs as a non root user. The knowledge base is mounted read only in `compose.yaml`.
* Dependencies are declared in `pyproject.toml`; pin and scan them in the platform's supply chain process before production.

## 6. Backup and recovery

The knowledge base can always be rebuilt from the source files with `v2e ingest`, and the embedding cache makes a rebuild cheap. Back up the database for the audit trail and the embedding cache; treat the source files in `knowledge_base/raw` as the system of record.

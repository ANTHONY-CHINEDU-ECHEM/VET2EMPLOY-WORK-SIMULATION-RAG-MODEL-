# API reference

Base path: `/v1`. All request and response bodies are JSON. The live OpenAPI schema is served at `/docs` (Swagger UI) and `/redoc` unless `V2E_EXPOSE_DOCS=false`, and the same schema is committed as `docs/openapi.json` for client generation (`v2e openapi` regenerates it).

## Conventions

* Authentication: `X-API-Key` header. Admin endpoints require an admin key.
* Correlation: send `X-Request-ID`; it is echoed on the response and attached to every log line.
* Unknown request fields are rejected with `422 invalid_request` so typing mistakes surface immediately. Validation errors list each problem in `details.errors` (`loc`, `msg`, `type`) and never echo submitted values.
* Errors: every non 2xx response, whatever produced it, is `{"error": "<code>", "message": "<text>", "details": {...}, "request_id": "<id>"}`.
* Limits: request bodies above `V2E_MAX_REQUEST_BODY_BYTES` (1 MB) are rejected with `413 payload_too_large` before they are read; uploads use `V2E_MAX_UPLOAD_BYTES`. Model backed endpoints stop at `V2E_REQUEST_TIMEOUT_SECONDS` (150) with `504 generation_timeout`.

## POST /v1/tasks/generate

Generate one verified work item.

### Request

| Field | Type | Notes |
| :--- | :--- | :--- |
| `track` | `DA`, `SE`, `CY`, `DE` | Work Brief track. Either `track` or `role_family` is required. |
| `role_family` | `cybersecurity`, `data_analyst`, `data_engineer`, `software_developer` | Derived from `track` when omitted; must match it when both are given. |
| `job_level` | `beginner`, `mid_level`, `advanced`, `expert` | Selects market evidence; relaxed automatically if too narrow. |
| `sector` | `construction`, `consulting`, `education`, `healthcare`, `information_technology`, `general` | Selects market evidence; relaxed automatically if too narrow. |
| `task_type` | `core_activity` (default), `colleague_request`, `stakeholder_update`, `complication` | |
| `activity_number` | integer from 1 to the track's `activity_count` (12) | Activity from the track's sequential activities. Requires `track`. Larger values return `422 invalid_request` before any retrieval. |
| `simulation_day` | `Monday` to `Friday` | |
| `focus` | string, up to 600 characters | Optional emphasis. |
| `region` | 2 to 8 letters | Defaults to `UK`. |
| `candidate_first_name` | string | Used in the greeting only. |
| `manager_name` | string | Persona name for the sign off. Never invented when absent. |
| `previous_task_titles` | up to 50 strings of up to 300 characters | Avoids repetition. |
| `include_market_evidence` | boolean | Defaults to `true`. |

### Response 200

| Field | Description |
| :--- | :--- |
| `task.title` | Short task title, verified against the cited evidence like the message |
| `task.sender_role` | Persona role sending the message, as named in the evidence |
| `task.priority` | `P0`, `P1` or `P2` |
| `task.message` | The in character message the candidate reads |
| `task.summary` | Grounded item: `{text, evidence_ids}` |
| `task.context`, `task.objectives`, `task.steps` | Lists of grounded items; `steps` are ordered |
| `task.deliverables[]` | `reference`, `name`, `format`, `file_name`, `due`, `acceptance_criteria[]`, `evidence_ids` |
| `task.tools_and_technologies`, `task.collaboration`, `task.complications` | Lists of grounded items |
| `task.competencies[]` | `competency` (one of the five Vet2Employ competencies), `weight_percent`, `why`, `evidence_ids` |
| `task.schedule` | `assigned_on`, `due`, `estimated_effort_hours`, `evidence_ids` |
| `task.open_questions` | Questions the candidate may need to raise; not presented as facts |
| `evidence[]` | `evidence_id`, `chunk_id`, `citation`, `source_type`, `facet`, `source_url`, `retrieval_purpose`, `score` |
| `verification` | `verdict`, `groundedness`, `claims_total`, `claims_factual`, `claims_supported`, `violations[]`, `pruned[]`, `checks_run[]`, `repair_attempts`, `entailment_model`, `entailment_error` |
| `meta` | `audit_id`, `model`, `prompt_version`, `latency_ms`, `usage`, `diagnostics` (retrieval steps, relaxations, evidence tokens) |

`verification.verdict` is `approved` or `approved_with_pruning` on a `200`. Any other outcome is an error response.

### Errors

| Status | `error` | Cause |
| :--- | :--- | :--- |
| 401 | `unauthorized` | Missing or wrong `X-API-Key` |
| 413 | `payload_too_large` | Body above the configured limit |
| 422 | `invalid_request` | Field validation failed, or the request contradicts the catalogue (`details.activity_count`) |
| 422 | `insufficient_evidence` | A required retrieval step returned nothing. `details.missing` lists the steps, `details.diagnostics` the plan. No model was called. |
| 422 | `grounding_failed` | Unsupported core content (title, summary, objectives, steps) or excessive pruning. `details.verification` and `details.audit_id` explain why. |
| 500 | `internal_error` | Unexpected failure. Quote `request_id`. |
| 502 | `upstream_model_error`, `invalid_model_output` | OpenRouter failure, truncated output, or output that never matched the schema. `details.audit_id` and `details.provider_status` when known. |
| 503 | `verification_unavailable` | The judge could not run while verification is required. Sent with `Retry-After`. |
| 503 | `service_unavailable`, `store_unavailable` | OpenRouter is not configured or a dependency is down |
| 504 | `generation_timeout` | The request passed `V2E_REQUEST_TIMEOUT_SECONDS` |

## POST /v1/manager/reply

Reply in character to a candidate message.

### Request

| Field | Type | Notes |
| :--- | :--- | :--- |
| `track` or `role_family` | as above | One is required; they must match when both are given |
| `job_level`, `sector` | as above | Optional |
| `candidate_message` | string, 1 to 8,000 characters | Redacted for personal data before use |
| `conversation[]` | `{role: "candidate" or "manager", content}` | Up to 40 turns; the last 12 are used and redacted |
| `current_task_title`, `current_task_summary` | string | Focuses retrieval and is the only context that may license numbers and names in the reply. Redacted. |
| `simulation_day`, `candidate_first_name`, `manager_name`, `region` | as above | |

### Response 200

| Field | Description |
| :--- | :--- |
| `reply.reply` | The message to show the candidate |
| `reply.intent` | `answer`, `clarify`, `redirect`, `feedback`, `acknowledge` or `escalate` |
| `reply.claims[]` | Grounded statements made in the reply |
| `reply.needs_human_review`, `reply.review_reason` | Set for wellbeing, fairness, adjustments, safeguarding, or when the service abstained. Always true when `safeguarding.flagged` is true. |
| `safeguarding` | `{flagged, categories[], action}` where `action` is `none`, `flag` or `intercept`. See `docs/safeguarding.md`. |
| `evidence`, `verification`, `meta` | As for tasks. `verification.verdict` is `approved`, `approved_with_pruning`, `abstained` (no evidence answers the message) or `intercepted` (crisis message answered with the fixed support message, no model called). `meta.diagnostics.pii_redactions` counts redactions. |

Errors are the same as for tasks, except that a reply never returns `insufficient_evidence` or `grounding_failed`: it abstains in character instead.

## POST /v1/retrieve

| Field | Type | Notes |
| :--- | :--- | :--- |
| `query` | string | Required |
| `track`, `role_family`, `job_level`, `sector` | as above | Optional filters. A track also scopes market evidence to its role family; a conflicting `role_family` is rejected. |
| `source_types`, `facets` | lists of catalogue values | Optional filters (see `docs/knowledge_base.md` for values). Unknown values are rejected with `422`. |
| `limit` | 1 to 30 | Defaults to 8 |
| `include_engineering_reference` | boolean | Defaults to `false` |

Response: `evidence[]` (as above plus `text`), `grounding_prompt` (grounding rules, voice rules and the rendered evidence, ready to use as a system prompt) and `diagnostics`.

## POST /v1/verify

| Field | Type | Notes |
| :--- | :--- | :--- |
| `text` | string | The output to verify |
| `chunk_ids` | list of 1 to 40 strings | The chunks the text was grounded on (from `/v1/retrieve`) |
| `use_entailment` | boolean | Defaults to `true` |

Response: `verification` (each sentence is a claim; the verdict is `rejected` when groundedness falls below `V2E_VERIFICATION_MIN_GROUNDEDNESS`) and `evidence`. Chunk ids that do not exist return `422 unknown_chunks` with `details.missing`; they are never silently ignored.

## GET /v1/catalog

Returns `tracks[]` (`code`, `name`, `role_family`, `manager_title`, `activity_count`), the allowed values for `role_families`, `sectors`, `job_levels` and `task_types`, and `knowledge_base` coverage counts by source type, facet, role family, sector and track.

## GET /v1/insights/role_profile

Query: `role_family` (required), `sector`, `job_level`, `top_n` (1 to 50). Returns `postings`, `employers`, `job_levels`, `top_skills[]` and `top_tools[]` (`name`, `postings`, `share`), `culture_ratings` (mean, employer count, minimum and maximum per rating), `sample_employers` and `sources`. Computed directly from research records; no model is involved.

## Administration

| Method and path | Purpose |
| :--- | :--- |
| `GET /v1/admin/documents` | List ingested documents with source type, content hash and collection date |
| `POST /v1/admin/ingest?force=false&prune=false` | Start background ingestion of `V2E_KNOWLEDGE_BASE_DIR`; returns `202` with a `run_id`. `prune=true` deletes documents whose source file was removed. `409` if a run is already in progress on this replica; a run that finds another process holding the lock ends with status `conflict`. |
| `GET /v1/admin/ingest/{run_id}` | Run status (`running`, `completed`, `failed`, `conflict`) and the full ingestion report. Held by the replica that started the run; completed runs are also in the `ingestion_runs` table. |
| `POST /v1/admin/documents` | Multipart upload of one `.pdf`, `.docx` or `.xlsx` file (50 MB limit by default), checked against the file signature of its type, written atomically and ingested immediately. `409 ingestion_in_progress` while another run holds the lock. |
| `DELETE /v1/admin/documents/{document_id}` | Remove a document with its chunks and records; `404` for an unknown id. Remove the source file too, or the next directory ingestion adds it back (or use `prune`). |

Admin endpoints return `401` without a key and `403` with a platform key.

## Operations

| Method and path | Purpose |
| :--- | :--- |
| `GET /health/live` | Process is running; returns `version` and `prompt_version` |
| `GET /health/ready` | `200` when the store is reachable, the knowledge base is loaded, OpenRouter is configured and the embedding model and dimensions match the stored vectors; otherwise `503` with the failing checks. Cached for `V2E_READINESS_CACHE_SECONDS`. |
| `GET /metrics` | Prometheus exposition. Requires `Authorization: Bearer <V2E_METRICS_TOKEN>` when that setting is non empty. |

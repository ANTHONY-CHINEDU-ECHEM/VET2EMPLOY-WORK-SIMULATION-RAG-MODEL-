# Changelog

All notable changes to this service. Versions follow semantic versioning; the prompt version is tracked separately in `src/v2e_rag/llm/prompts.py` and returned in every response.

## 1.1.0 (25 October 2026)

A hardening release before handover to the platform developer. Retrieval quality on the golden set is unchanged (hit rate at 5 of 1.00, MRR 0.743, nDCG at 5 of 0.788 offline). The test suite grew from 86 to 172 unit and style tests and from 10 to 17 integration tests, with 89 percent line and branch coverage.

### Contract changes the platform must handle

* **Every error now uses one envelope**, including request validation (previously FastAPI's `{"detail": [...]}`), authentication, unknown routes and unexpected crashes (previously a plain text 500): `{"error", "message", "details", "request_id"}`. Validation errors use the code `invalid_request` and never echo submitted values.
* **New error codes:** `invalid_request` (422, for example an `activity_number` above the track's `activity_count`), `unknown_chunks` (422, `/v1/verify` with chunk ids that do not exist; previously they were silently ignored), `payload_too_large` (413), `generation_timeout` (504), `internal_error` (500) and `ingestion_in_progress` (409).
* **`POST /v1/manager/reply` returns a `safeguarding` block** and two verdicts are now possible beyond approval: `abstained` (previously mislabelled `approved` when no evidence answered the message) and `intercepted` (the safeguarding screen replaced the reply).
* **`DELETE /v1/admin/documents/{id}` returns 404** for an unknown id (previously 204).
* **Admin endpoints return 401** when the key is missing and 403 when a platform key is used.
* **`role_family` must match `track`** on `/v1/manager/reply` and `/v1/retrieve`, as it already did on `/v1/tasks/generate`. `source_types` and `facets` on `/v1/retrieve` are validated against the catalogue.
* `GET /v1/catalog` returns `activity_count` per track.
* Upstream error messages no longer include provider response text; it is logged instead. `details.audit_id` is returned on model failures.

### Fixed

* The API could not start: `python-multipart`, required by the upload endpoint, was missing from the dependencies.
* **Fail open in the entailment judge.** A claim the judge skipped was recorded as a minor problem and the output was approved. Skipped claims are now asked again once and, if still unjudged, treated as unsupported.
* **The task title was never verified**, although candidates read it. It is now a core claim; an unsupported title rejects the task.
* **Pruning could report content as removed when it was not.** Claim ids the pruner did not recognise were skipped silently. They now refuse the output. Sentences are removed by exact character span instead of the first matching substring.
* **A required retrieval step could pass the abstention gate yet contribute nothing to the evidence the model saw** when its passage did not fit the token budget. The best passage of every required step is now reserved before the budget applies.
* Numbers and names asserted by a candidate no longer license the manager to repeat them; only platform supplied task context does.
* Personal data is now redacted from `current_task_title`, `current_task_summary` and every conversation turn, not only the candidate message, before any model call or audit write. Redaction now also covers lower case postcodes, bracketed phone numbers and IBANs.
* Model failures (`upstream_model_error`, `invalid_model_output`) are now written to the audit trail.
* Embedding provider failures surfaced as unhandled 500 errors; they now return `502 upstream_model_error`.
* A database outage during a request surfaced as an unhandled 500; it now returns `503 store_unavailable` with `Retry-After`, without exposing connection details.
* `v2e audit show` could not find records with the memory backend when they were written by another process.
* PDF parsing and file hashing ran on the event loop, so an ingestion started through the API stalled every other request. They now run in a worker thread, as does the upload write.
* Completed background ingestion tasks were never released.
* `datetime.utcnow` (deprecated) replaced; deprecation warnings from this package now fail the build instead of being hidden.
* A blank optional value in `.env` (for example `V2E_AUDIT_RETENTION_DAYS=`) failed settings validation.
* All 17 type errors reported by mypy fixed; stricter type checking is now enforced.

### Security

* Untrusted text (candidate messages, conversation history, task context and knowledge base passages) is fenced in prompt elements the model is told to treat as data. Text that tries to close or forge a fence is neutralised. The judge receives claims as a JSON array, so claim text cannot forge another claim or verdict.
* Production configuration is validated as a whole: platform and admin keys required, distinct and at least 24 characters; OpenRouter configured; no memory backend, hashing embedder, disabled entailment, disabled redaction or wildcard CORS.
* API keys and the metrics token are held as secrets and never appear in logs or `repr`. A log processor scrubs bearer tokens, OpenRouter keys and database passwords from every log line.
* Request bodies are capped (1 MB by default, the upload limit for uploads) before they are read. Uploads are checked against the file signature of their type and written atomically.
* Defensive response headers (`nosniff`, frame denial, no referrer, `no-store`, a restrictive content security policy on API responses).
* `X-Request-ID` values are accepted only when safe to log; otherwise a new id is generated.
* Optional bearer token for `/metrics`; interactive docs can be disabled with `V2E_EXPOSE_DOCS=false`.
* `defusedxml` added so workbook parsing is protected against XML entity attacks.
* Database statements time out (15 seconds by default).

### Added

* **Safeguarding screen** (`config/safeguarding.yaml`, owned by the Programme Office). Crisis messages receive a fixed support message without any model call; wellbeing, harassment, discrimination, reasonable adjustment and assessment fairness messages always set `needs_human_review`. See `docs/safeguarding.md`.
* Whole request deadline (`V2E_REQUEST_TIMEOUT_SECONDS`).
* `v2e doctor` checks configuration, database, migrations, knowledge base and model access in one command.
* `v2e audit show` and `v2e audit purge` for incident review and the retention period.
* `v2e ingest --prune` and `POST /v1/admin/ingest?prune=true` remove documents whose source file was withdrawn.
* `v2e openapi` exports `docs/openapi.json` so the platform can generate a typed client; CI fails if it drifts.
* Quality gates: `v2e eval-retrieval --min-hit-rate --min-mrr` and `v2e eval-generation --min-as-expected`.
* Six new generation scenarios: crisis intercept, harassment flag, two prompt injection attempts, personal data redaction and an activity beyond the track count.
* Migration `0002`: `request_id`, `prompt_version` and `details` on `generation_audit`, and an index for activity filters.
* Advisory locks: concurrent `v2e migrate` runs (several replicas starting together) and concurrent ingestion runs (CLI and API) are serialised across processes.
* Metrics: requests in flight, model latency, embedding cache hits, safeguarding flags, ingestion outcomes and build information.
* Structured output mode rejected by a model is remembered, so later calls skip the failing attempt. Truncated model output is reported as such.
* Judge batches run concurrently (`V2E_VERIFIER_CONCURRENCY`).
* Readiness results are cached briefly so probes do not run aggregate queries continuously.
* Engineering: pinned `requirements/constraints*.txt`, CI with lint, types, coverage gate, two PostgreSQL versions, compatibility on Python 3.11 and 3.13, dependency audit, secret scan, container build, smoke test and image scan; a release workflow publishing a signed provenance image; Dependabot; pre commit hooks; CODEOWNERS; issue and pull request templates; Cloud Run service and job templates; Prometheus alert rules; `scripts/smoke_test.py`.
* Documentation: handover checklist, security model, safeguarding guide, architecture decision records, contributing guide and security policy.

### Changed

* `PROMPT_VERSION` is `2026.10.2` (fenced inputs and the judge format changed).
* The container installs exact versions from the lock file, runs with read only application code, and the entrypoint supports `V2E_SKIP_MIGRATIONS`.
* `compose.yaml` publishes ports on localhost only and mounts the knowledge base writable for the API so uploads work (it was read only, so every upload failed).
* The uvicorn access log is off; requests are logged once, as structured JSON with the request id.

## 1.0.0

Initial release.

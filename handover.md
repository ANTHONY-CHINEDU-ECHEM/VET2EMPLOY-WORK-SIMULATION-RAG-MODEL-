# Handover checklist

This is the path from receiving the repository to serving the AI Manager in production. Work through it in order; each step has a check that proves it is done.

## 1. Run it locally (about 30 minutes)

| Step | Command | Done when |
| :--- | :--- | :--- |
| Install with pinned versions | `make install-locked && make hooks` | `make check` passes |
| Start PostgreSQL with pgvector | `make db-up` | `docker compose ps` shows `db` healthy |
| Configure | `cp .env.example .env`, then set `OPENROUTER_API_KEY` | `v2e doctor` reports only knowledge base checks failing |
| Obtain the knowledge base files | From the approved channel (they are not in git) into `knowledge_base/raw/` | 27 files present |
| Migrate and ingest | `v2e migrate && v2e ingest` | Report shows 0 failures; `v2e doctor` passes |
| Measure retrieval | `make eval-retrieval` | Gate passes; keep `var/reports/retrieval_eval.json` as the production baseline |
| Generate end to end | `make eval-generation` | As expected rate at least 0.9; read three tasks yourself |
| Serve and smoke test | `make serve`, then `make smoke API_KEY=...` | 0 failures |

## 2. Integrate the platform

1. Generate a typed client from `docs/openapi.json` (for example with `openapi-typescript`), or start from the small client in [integration_guide.md](integration_guide.md).
2. Call `POST /v1/tasks/generate` from the Task Engine job, never from the candidate request path. Set the client timeout above `V2E_REQUEST_TIMEOUT_SECONDS` (150 by default).
3. Handle every error code in the contract (section 8 of the integration guide). In particular, never fall back to an ungrounded model call on `422` or `503`.
4. Route `reply.needs_human_review` to the Programme Office queue, and treat `safeguarding.action == "intercept"` as urgent: page someone (see [safeguarding.md](safeguarding.md)).
5. Store `meta.audit_id` and `evidence[].chunk_id` with every task and reply. They are how any output is explained later.
6. Send `X-Request-ID` on every call and log it on the platform side.

## 3. Decisions the product owner must make before launch

| Decision | Where it is configured | Default |
| :--- | :--- | :--- |
| Safeguarding patterns, intercept wording and support contacts reviewed by the Programme Office | `config/safeguarding.yaml` | Draft written for review |
| Crisis messages intercepted or only flagged | `V2E_SAFEGUARDING_CRISIS_ACTION` | `intercept` |
| Audit retention period agreed with data protection | `V2E_AUDIT_RETENTION_DAYS` and the scheduled purge job | Not set (kept indefinitely) |
| Candidate facing product name | `V2E_BRAND_NAME` | `Vet2Employ` |
| Supervisor persona names | `manager_name` on each request | Signs off by role |
| Generation and judge models, after checking price and availability | `V2E_GENERATION_MODEL`, `V2E_VERIFIER_MODEL` | Claude Sonnet 4.5 and Claude Haiku 4.5 through OpenRouter |
| Database host (Supabase or AlloyDB) | `V2E_DATABASE_URL` | Either works |

## 4. Deploy

1. Create the secrets: database URL, OpenRouter key, platform keys, admin keys. Generate keys with `python -c "import secrets; print(secrets.token_urlsafe(32))"`; production requires at least 24 characters and distinct platform and admin keys.
2. Fill in `deploy/cloud_run/service.yaml`, `job_ingest.yaml` and `job_audit_purge.yaml` (or translate them to your platform). Keep ingress internal.
3. Tag a release (`git tag v1.1.0 && git push origin v1.1.0`); the release workflow publishes the image with provenance.
4. Deploy the service, run the ingestion job, then run `v2e doctor` in the deployed environment (for example `gcloud run jobs` with args `["doctor"]`).
5. Load `deploy/prometheus/alerts.yaml` into your alerting and make sure the safeguarding alert reaches a person.
6. Run `scripts/smoke_test.py` against the deployed URL with a platform key.

## 5. Operate

* Read [operations.md](operations.md) for runbooks and audit queries.
* Rerun `make eval-retrieval` after every knowledge base change and `make eval-generation` after every prompt, model or verification change. Treat a drop of more than 0.05 in hit rate or MRR as a release blocker.
* Turn every reported hallucination into a scenario in `eval/generation_scenarios.yaml`.

## 6. Where to look first

| Question | File |
| :--- | :--- |
| How does a request flow through the service? | `src/v2e_rag/generation/service.py` |
| What does the model see? | `src/v2e_rag/llm/prompts.py` |
| How is output checked? | `src/v2e_rag/verification/` |
| Why was a decision taken? | `docs/adr/` |
| What changed in this release? | `CHANGELOG.md` |

# Contributing

## Set up

```bash
python3 -m venv .venv && source .venv/bin/activate
make install-locked      # exact versions CI uses
make hooks               # pre-commit hooks: lint, format, secrets, house style
cp .env.example .env
make db-up               # PostgreSQL with pgvector in Docker
v2e migrate && v2e ingest
v2e doctor
```

Without Docker or an OpenRouter key, use the offline mode in the README (memory backend and the hashing embedder).

## Before opening a pull request

```bash
make check                                    # lint, types, unit tests, OpenAPI schema
make test-integration TEST_DB=postgresql://...  # store, migration or ingestion changes
make eval-retrieval                           # retrieval, chunking or ingestion changes
make eval-generation                          # prompt, verification or model changes (uses OpenRouter)
```

The pull request template lists what each kind of change needs. CI runs the same checks, plus a dependency audit, a secret scan and a container build with a smoke test.

## Rules that keep the service trustworthy

* **Never weaken verification to make a test pass.** If a correct output is rejected, fix the evidence, the prompt or the check, and add a test that shows why.
* **Fail closed.** When the service cannot prove something is supported, it abstains, prunes or refuses. New code paths follow the same rule.
* **Bump `PROMPT_VERSION`** whenever prompt wording changes. Audit records depend on it.
* **Every grounding defect becomes a test or a scenario.** A hallucination report should end as a case in `eval/generation_scenarios.yaml` or a unit test.
* **Migrations are additive and numbered.** They must be safe to apply while the previous version is still serving traffic.
* **The API contract is versioned by `docs/openapi.json`.** Regenerate it with `make openapi` and tell the platform developer about any change.
* **No knowledge base files, secrets or candidate data in git.** The hooks block the first two; reviewers check the third.

## House style

The repository has no em dashes or en dashes anywhere, and documentation prose has no hyphens (code, URLs and file names are exempt). Use `*` for bullet lists. `tests/test_repository_style.py` enforces both.

## Commit messages

Use the imperative mood and explain why, for example "Treat unjudged claims as unsupported so the judge cannot fail open". Reference the issue when there is one.

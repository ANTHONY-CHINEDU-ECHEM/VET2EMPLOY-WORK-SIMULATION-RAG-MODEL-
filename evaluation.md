# Evaluation

Quality is measured, not assumed. The service ships with automated tests, a retrieval golden set and end to end generation scenarios.

## 1. Test suites

| Suite | Command | What it proves |
| :--- | :--- | :--- |
| Unit and contract tests | `make test` | Every parsing rule, filter, fusion and ranking rule, planning step, verification check, pruning rule, the OpenRouter client contract (payload, fallbacks, retries, remembered structured output modes, truncation, errors), the full generation pipeline with scripted models, the HTTP contract and error envelope, request limits and deadlines, the safeguarding screen, personal data redaction, prompt fencing, production settings validation, ingestion pruning, the command line, and the repository style rules |
| PostgreSQL integration | `make test-integration` | Migrations and the dimension guard, concurrent migrations from several processes, filtered HNSW search with exact scan fallback, weighted full text search, atomic replacement and cascades, the embedding cache, audit records with request ids and retention purges, the cross process ingestion lock and the statement timeout |
| Knowledge base integration | `pytest tests/integration/test_knowledge_base.py` | All supplied files ingest without failure; every track has all 12 activities tagged; client, deliverables, rubric and supervisor sections exist for every track; deliverable file names survive parsing; brand voice and all four role families are covered; reingestion is idempotent; retrieval meets the offline threshold |

The generation pipeline tests use scripted models to prove the safety behaviour deterministically:

* a fully grounded task is approved with valid citations and an audit record;
* a hallucinated tool is caught and repaired in one round;
* an invented complication that survives repair is pruned;
* unsupported core content is refused;
* a request without required evidence never reaches the model;
* an unavailable judge fails closed;
* em dashes and banned terms never reach the candidate;
* personal data is redacted before a model call;
* the manager abstains when no evidence answers a message, and says so in its verdict;
* a claim the judge skips is asked again and then treated as unsupported;
* a verdict for a claim that was never asked is ignored;
* an unsupported task title rejects the task;
* the pruner refuses content it cannot locate and removes exact sentence spans;
* every required retrieval step is represented in the evidence pack even when the budget is tight;
* personal data is removed from every candidate field before the model and the audit trail;
* candidate text cannot close its prompt fence or forge evidence;
* a number the candidate asserts is never confirmed by the manager;
* crisis messages are intercepted without any model call, and flagged messages always reach human review.

CI runs the unit tests together with the PostgreSQL integration tests on two PostgreSQL versions and fails below 80 percent line and branch coverage.

## 2. Retrieval evaluation

`eval/retrieval_golden.yaml` holds 24 questions across programme rules, all four tracks, brand guidance, the product overview, market evidence and the rating methodology. Relevance is defined by metadata (track, facet, section, company, activity number, text), so the set stays valid after re chunking.

```bash
v2e eval-retrieval --k 5 --min-hit-rate 0.9 --min-mrr 0.6   # exits 1 below the gate
```

| Metric | Definition |
| :--- | :--- |
| Hit rate at k | Share of questions with at least one relevant passage in the top k |
| MRR | Mean reciprocal rank of the first relevant passage |
| Precision at k | Share of the top k passages that are relevant |
| nDCG at k | Ranking quality with binary relevance |

Results are written to `var/reports/retrieval_eval.json` with the top citations for every question.

**Offline baseline** with the lexical stand in embedder (no network, `V2E_MIN_SEMANTIC_SIMILARITY=0.05`): hit rate at 5 of 1.00, MRR of 0.74 and nDCG at 5 of 0.79 on the in memory backend; hit rate at 5 of 0.96, MRR of 0.78 and nDCG at 5 of 0.79 on PostgreSQL. Free text retrieval deliberately interleaves programme material and market evidence, so a question answered by a job posting usually ranks it second; this lowers MRR slightly and guarantees programme rules are never crowded out. Production embeddings should meet or exceed this. Record the production baseline after the first ingestion and treat a drop of more than 0.05 in hit rate or MRR as a release blocker.

## 3. Generation evaluation

`eval/generation_scenarios.yaml` holds 19 scenarios: core activities in every track, a complication, a stakeholder update, a colleague request, a market only task, two impossible activities (expected to be rejected as `invalid_request`), manager replies to a factual question, a question the evidence cannot answer and a wellbeing concern, and an adversarial set: a crisis message (expected `intercepted`), a harassment report (expected flag), two prompt injection attempts and a message full of personal data.

Scenarios can assert more than the verdict: `expect_review`, `expect_safeguarding` and `must_not_contain` (text that must never appear in the candidate facing output, such as a system prompt fragment or an injected deadline).

```bash
v2e eval-generation --min-as-expected 0.9   # exits 1 below the gate
```

| Measure | Target |
| :--- | :--- |
| Scenarios behaving as expected | 100 percent |
| Mean groundedness before pruning | 0.95 or higher |
| Rejected outputs | 0 on the shipped scenarios |
| Model errors | 0 (one failing scenario is recorded and the run continues) |
| p95 latency | Within the Task Engine timeout (120 seconds recommended) |

Results, including the audit id of every scenario, are written to `var/reports/generation_eval.json`.

## 4. Human review

The Plain English Report sets a release gate: the AI Manager persona must be described as believable by at least three of five external testers. Use the generation evaluation output as the review pack. For each task, reviewers should answer three questions: would a real supervisor in this discipline write this, is anything in it untrue to the brief, and is the tone right for a junior professional. Record findings against the audit ids so each judgement can be traced to the exact evidence and prompt version.

## 5. Extending the sets

Add a retrieval case whenever a new kind of question appears in production or a defect is fixed, and add a generation scenario for every new task type, track or persona. Keep both sets under version control with the code so that every change is measured against the same standard.

"""Prometheus metrics exposed at ``/metrics``."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

REQUESTS = Counter("v2e_http_requests_total", "HTTP requests", ["route", "method", "status"])
REQUEST_LATENCY = Histogram(
    "v2e_http_request_seconds",
    "HTTP request latency",
    ["route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40, 60, 120),
)
RETRIEVAL_LATENCY = Histogram(
    "v2e_retrieval_seconds", "Retrieval plan latency", buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5)
)
LLM_REQUESTS = Counter(
    "v2e_llm_requests_total", "LLM calls through OpenRouter", ["purpose", "model", "outcome"]
)
LLM_TOKENS = Counter("v2e_llm_tokens_total", "LLM tokens", ["purpose", "kind"])
EMBEDDING_REQUESTS = Counter("v2e_embedding_requests_total", "Embedding API batches", ["model"])
VERIFICATION_OUTCOMES = Counter(
    "v2e_verification_outcomes_total", "Verification verdicts", ["operation", "verdict"]
)
UNSUPPORTED_CLAIMS = Counter(
    "v2e_unsupported_claims_total", "Claims rejected by verification", ["operation", "check"]
)
ABSTENTIONS = Counter(
    "v2e_abstentions_total", "Requests declined for insufficient evidence", ["operation"]
)

"""Prometheus metrics exposed at ``/metrics``.

Label values are always drawn from small, fixed sets (route templates, purposes, verdicts,
check names, model ids from configuration) so the series count stays bounded.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info

BUILD_INFO = Info("v2e_build", "Service version and prompt version")

REQUESTS = Counter("v2e_http_requests_total", "HTTP requests", ["route", "method", "status"])
REQUEST_LATENCY = Histogram(
    "v2e_http_request_seconds",
    "HTTP request latency",
    ["route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40, 60, 120),
)
IN_FLIGHT = Gauge("v2e_http_requests_in_flight", "HTTP requests currently being served")
RETRIEVAL_LATENCY = Histogram(
    "v2e_retrieval_seconds", "Retrieval plan latency", buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5)
)
LLM_REQUESTS = Counter(
    "v2e_llm_requests_total", "LLM calls through OpenRouter", ["purpose", "model", "outcome"]
)
LLM_LATENCY = Histogram(
    "v2e_llm_request_seconds",
    "LLM call latency including retries",
    ["purpose"],
    buckets=(0.5, 1, 2, 5, 10, 20, 40, 60, 90, 120),
)
LLM_TOKENS = Counter("v2e_llm_tokens_total", "LLM tokens", ["purpose", "kind"])
EMBEDDING_REQUESTS = Counter("v2e_embedding_requests_total", "Embedding API batches", ["model"])
EMBEDDING_CACHE = Counter(
    "v2e_embedding_cache_total", "Document embeddings served from cache or computed", ["result"]
)
VERIFICATION_OUTCOMES = Counter(
    "v2e_verification_outcomes_total", "Verification verdicts", ["operation", "verdict"]
)
UNSUPPORTED_CLAIMS = Counter(
    "v2e_unsupported_claims_total", "Claims rejected by verification", ["operation", "check"]
)
ABSTENTIONS = Counter(
    "v2e_abstentions_total", "Requests declined for insufficient evidence", ["operation"]
)
SAFEGUARDING_FLAGS = Counter(
    "v2e_safeguarding_flags_total",
    "Candidate messages flagged by the safeguarding screen",
    ["category", "action"],
)
INGESTION_FILES = Counter(
    "v2e_ingestion_files_total", "Files processed by ingestion runs", ["status"]
)

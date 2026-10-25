"""Service configuration.

Every setting is read from environment variables prefixed with ``V2E_`` (or from a
``.env`` file). The OpenRouter key is also accepted as plain ``OPENROUTER_API_KEY`` so the
service can share the key the platform already uses.

Secrets (API keys, the OpenRouter key, the metrics token) are held as ``SecretStr`` so they
never appear in ``repr`` output, logs or error messages.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="V2E_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        # A blank line such as `V2E_AUDIT_RETENTION_DAYS=` in .env means "use the default".
        env_ignore_empty=True,
    )

    # Service
    environment: Environment = "development"
    service_name: str = "vet2employ_rag"
    log_level: str = "INFO"
    log_json: bool = True
    brand_name: str = "Vet2Employ"
    default_region: str = "UK"
    expose_docs: bool = True
    request_timeout_seconds: float = Field(default=150.0, gt=0)
    max_request_body_bytes: int = Field(default=1_000_000, gt=0)

    # Security: comma separated list of keys accepted in the X-API-Key header.
    api_keys: SecretStr = SecretStr("")
    admin_api_keys: SecretStr = SecretStr("")
    cors_allow_origins: str = ""
    # Optional bearer token for /metrics. Empty means the endpoint is open, which suits a
    # private network where only the scraper can reach the service.
    metrics_token: SecretStr = SecretStr("")

    # OpenRouter (chat completions and embeddings)
    openrouter_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("V2E_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
    )
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_app_url: str = ""
    openrouter_app_title: str = "Vet2Employ RAG Service"
    openrouter_timeout_seconds: float = 90.0
    openrouter_max_retries: int = Field(default=3, ge=0, le=8)
    openrouter_data_collection: Literal["allow", "deny"] = "deny"

    generation_model: str = "anthropic/claude-sonnet-4.5"
    generation_fallback_models: str = "openai/gpt-4o"
    generation_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    generation_max_tokens: int = 4000
    structured_output_mode: Literal["json_schema", "json_object", "prompt"] = "json_schema"

    verifier_model: str = "anthropic/claude-haiku-4.5"
    verifier_fallback_models: str = "openai/gpt-4o-mini"
    verifier_max_tokens: int = 3000
    verifier_batch_size: int = Field(default=20, ge=1, le=60)
    verifier_concurrency: int = Field(default=4, ge=1, le=16)

    # Embeddings
    embedding_provider: Literal["openrouter", "hashing"] = "openrouter"
    embedding_model: str = "openai/text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, ge=8, le=16000)
    embedding_send_dimensions: bool = False
    embedding_batch_size: int = 64

    # Storage
    vector_backend: Literal["pgvector", "memory"] = "pgvector"
    database_url: str = "postgresql://v2e:v2e@localhost:5432/v2e_rag"
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    db_statement_timeout_ms: int = Field(default=15_000, ge=0)
    hnsw_ef_search: int = 80
    memory_snapshot_path: Path = Path("var/memory_store")
    audit_log_path: Path = Path("var/audit/generation_audit.jsonl")
    audit_retention_days: int | None = Field(default=None, ge=1)

    # Knowledge base
    knowledge_base_dir: Path = Path("knowledge_base/raw")
    config_dir: Path = Path("config")
    migrations_dir: Path = Path("migrations")
    reports_dir: Path = Path("var/reports")
    chunk_max_tokens: int = 450
    chunk_overlap_tokens: int = 60
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, gt=0)

    # Retrieval
    retrieval_candidates_per_channel: int = 40
    rrf_k: int = 60
    min_semantic_similarity: float = 0.20
    evidence_token_budget: int = 7000
    max_evidence_items: int = 24
    mmr_lambda: float = 0.75

    # Verification and hallucination control
    verification_enable_entailment: bool = True
    verification_max_repair_attempts: int = Field(default=1, ge=0, le=3)
    verification_min_groundedness: float = Field(default=0.9, ge=0.0, le=1.0)
    style_forbid_dashes: bool = True
    pii_redaction: bool = True

    # Safeguarding of candidate messages (see config/safeguarding.yaml)
    safeguarding_enabled: bool = True
    safeguarding_crisis_action: Literal["intercept", "flag"] = "intercept"

    # Operations
    readiness_cache_seconds: float = Field(default=15.0, ge=0)

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if self.chunk_overlap_tokens >= self.chunk_max_tokens:
            raise ValueError("V2E_CHUNK_OVERLAP_TOKENS must be smaller than V2E_CHUNK_MAX_TOKENS.")
        if self.db_pool_min_size > self.db_pool_max_size:
            raise ValueError("V2E_DB_POOL_MIN_SIZE cannot exceed V2E_DB_POOL_MAX_SIZE.")
        if self.environment == "production":
            problems = self.production_problems()
            if problems:
                raise ValueError(
                    "Production configuration is unsafe: " + " ".join(f"({p})" for p in problems)
                )
        return self

    def production_problems(self) -> list[str]:
        """Every reason this configuration is not safe to run in production."""
        problems: list[str] = []
        if not self.api_key_set:
            problems.append("V2E_API_KEYS must be set")
        if not self.admin_api_key_set:
            problems.append("V2E_ADMIN_API_KEYS must be set")
        if self.api_key_set & self.admin_api_key_set:
            problems.append("platform and admin keys must be different")
        if any(len(k) < 24 for k in self.api_key_set | self.admin_api_key_set):
            problems.append("API keys must be at least 24 characters")
        if not self.openrouter_configured:
            problems.append("OPENROUTER_API_KEY must be set")
        if self.embedding_provider == "hashing":
            problems.append("the hashing embedder is for offline development only")
        if self.vector_backend == "memory":
            problems.append("the memory backend is for tests and offline development only")
        if not self.verification_enable_entailment:
            problems.append("V2E_VERIFICATION_ENABLE_ENTAILMENT must stay true")
        if not self.pii_redaction:
            problems.append("V2E_PII_REDACTION must stay true")
        if "*" in self.cors_origins:
            problems.append("V2E_CORS_ALLOW_ORIGINS must not contain a wildcard")
        return problems

    @staticmethod
    def _split(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def api_key_set(self) -> frozenset[str]:
        return frozenset(self._split(self.api_keys.get_secret_value()))

    @property
    def admin_api_key_set(self) -> frozenset[str]:
        return frozenset(self._split(self.admin_api_keys.get_secret_value()))

    @property
    def generation_fallbacks(self) -> list[str]:
        return self._split(self.generation_fallback_models)

    @property
    def verifier_fallbacks(self) -> list[str]:
        return self._split(self.verifier_fallback_models)

    @property
    def cors_origins(self) -> list[str]:
        return self._split(self.cors_allow_origins)

    @property
    def openrouter_configured(self) -> bool:
        return self.openrouter_api_key is not None and bool(
            self.openrouter_api_key.get_secret_value().strip()
        )

    @property
    def is_local(self) -> bool:
        """Development and test environments allow unauthenticated access when no keys
        are configured. Every other environment fails closed."""
        return self.environment in ("development", "test")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

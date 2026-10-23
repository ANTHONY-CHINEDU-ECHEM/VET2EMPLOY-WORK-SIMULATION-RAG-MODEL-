"""Service configuration.

Every setting is read from environment variables prefixed with ``V2E_`` (or from a
``.env`` file). The OpenRouter key is also accepted as plain ``OPENROUTER_API_KEY`` so the
service can share the key the platform already uses.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="V2E_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Service
    environment: Literal["development", "test", "staging", "production"] = "development"
    service_name: str = "vet2employ_rag"
    log_level: str = "INFO"
    log_json: bool = True
    brand_name: str = "Vet2Employ"
    default_region: str = "UK"

    # Security: comma separated list of keys accepted in the X-API-Key header.
    api_keys: str = ""
    admin_api_keys: str = ""
    cors_allow_origins: str = ""

    # OpenRouter (chat completions and embeddings)
    openrouter_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("V2E_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
    )
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_app_url: str = ""
    openrouter_app_title: str = "Vet2Employ RAG Service"
    openrouter_timeout_seconds: float = 90.0
    openrouter_max_retries: int = 3
    openrouter_data_collection: Literal["allow", "deny"] = "deny"

    generation_model: str = "anthropic/claude-sonnet-4.5"
    generation_fallback_models: str = "openai/gpt-4o"
    generation_temperature: float = 0.2
    generation_max_tokens: int = 4000
    structured_output_mode: Literal["json_schema", "json_object", "prompt"] = "json_schema"

    verifier_model: str = "anthropic/claude-haiku-4.5"
    verifier_fallback_models: str = "openai/gpt-4o-mini"
    verifier_max_tokens: int = 3000

    # Embeddings
    embedding_provider: Literal["openrouter", "hashing"] = "openrouter"
    embedding_model: str = "openai/text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_send_dimensions: bool = False
    embedding_batch_size: int = 64

    # Storage
    vector_backend: Literal["pgvector", "memory"] = "pgvector"
    database_url: str = "postgresql://v2e:v2e@localhost:5432/v2e_rag"
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    hnsw_ef_search: int = 80
    memory_snapshot_path: Path = Path("var/memory_store")
    audit_log_path: Path = Path("var/audit/generation_audit.jsonl")

    # Knowledge base
    knowledge_base_dir: Path = Path("knowledge_base/raw")
    config_dir: Path = Path("config")
    migrations_dir: Path = Path("migrations")
    reports_dir: Path = Path("var/reports")
    chunk_max_tokens: int = 450
    chunk_overlap_tokens: int = 60

    # Retrieval
    retrieval_candidates_per_channel: int = 40
    rrf_k: int = 60
    min_semantic_similarity: float = 0.20
    evidence_token_budget: int = 7000
    max_evidence_items: int = 24
    mmr_lambda: float = 0.75

    # Verification and hallucination control
    verification_enable_entailment: bool = True
    verification_max_repair_attempts: int = 1
    verification_min_groundedness: float = 0.9
    style_forbid_dashes: bool = True
    pii_redaction: bool = True

    @model_validator(mode="after")
    def _validate_production(self) -> Settings:
        if self.environment == "production":
            if not self.api_key_set:
                raise ValueError("V2E_API_KEYS must be set in production.")
            if self.embedding_provider == "hashing":
                raise ValueError("The hashing embedder is for offline development only.")
        return self

    @staticmethod
    def _split(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def api_key_set(self) -> frozenset[str]:
        return frozenset(self._split(self.api_keys))

    @property
    def admin_api_key_set(self) -> frozenset[str]:
        return frozenset(self._split(self.admin_api_keys))

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

"""Composition root: builds every component from settings in one place."""

from __future__ import annotations

from dataclasses import dataclass

from v2e_rag.config import Settings
from v2e_rag.domain.catalog import Catalog, load_catalog
from v2e_rag.embeddings.base import CachedEmbedder, Embedder, HashingEmbedder, OpenRouterEmbedder
from v2e_rag.errors import ServiceNotConfiguredError
from v2e_rag.generation.service import GenerationService, GenerationSettings
from v2e_rag.ingestion.pipeline import IngestionPipeline
from v2e_rag.llm.openrouter import ChatModel, OpenRouterClient
from v2e_rag.retrieval.evidence import EvidenceBuilder
from v2e_rag.retrieval.hybrid import HybridRetriever
from v2e_rag.retrieval.planner import RetrievalPlanner
from v2e_rag.safeguarding import SafeguardingPolicy, load_policy
from v2e_rag.store.base import VectorStore
from v2e_rag.store.memory_store import MemoryStore
from v2e_rag.verification.entailment import EntailmentJudge
from v2e_rag.verification.verifier import Verifier


@dataclass
class Container:
    settings: Settings
    catalog: Catalog
    store: VectorStore
    embedder: Embedder
    chat: ChatModel | None
    retriever: HybridRetriever
    planner: RetrievalPlanner
    evidence: EvidenceBuilder
    verifier: Verifier
    service: GenerationService

    def ingestion_pipeline(self) -> IngestionPipeline:
        return IngestionPipeline(
            self.store,
            self.embedder,
            self.catalog,
            self.settings.chunk_max_tokens,
            self.settings.chunk_overlap_tokens,
        )

    async def start(self) -> None:
        await self.store.open()

    async def stop(self) -> None:
        await self.embedder.aclose()
        if self.chat is not None:
            await self.chat.aclose()
        await self.store.close()


def build_store(settings: Settings) -> VectorStore:
    if settings.vector_backend == "memory":
        return MemoryStore(
            snapshot_path=settings.memory_snapshot_path, audit_path=settings.audit_log_path
        )
    from v2e_rag.store.pgvector_store import PgVectorStore

    return PgVectorStore(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        ef_search=settings.hnsw_ef_search,
        statement_timeout_ms=settings.db_statement_timeout_ms,
    )


def build_safeguarding(settings: Settings) -> SafeguardingPolicy | None:
    if not settings.safeguarding_enabled:
        return None
    policy = load_policy(str(settings.config_dir))
    if policy is None:
        if settings.environment == "production":
            raise ServiceNotConfiguredError(
                f"{settings.config_dir}/safeguarding.yaml is required in production."
            )
        return None
    return policy.with_crisis_action(settings.safeguarding_crisis_action)


def build_embedder(settings: Settings, store: VectorStore) -> Embedder:
    if settings.embedding_provider == "hashing":
        inner: Embedder = HashingEmbedder(settings.embedding_dimensions)
    else:
        if not settings.openrouter_configured:
            raise ServiceNotConfiguredError(
                "OPENROUTER_API_KEY is required for OpenRouter embeddings. Set it, or use "
                "V2E_EMBEDDING_PROVIDER=hashing for offline development."
            )
        assert settings.openrouter_api_key is not None
        inner = OpenRouterEmbedder(
            api_key=settings.openrouter_api_key.get_secret_value(),
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            base_url=settings.openrouter_base_url,
            batch_size=settings.embedding_batch_size,
            send_dimensions=settings.embedding_send_dimensions,
            timeout=settings.openrouter_timeout_seconds,
            max_retries=settings.openrouter_max_retries,
            app_url=settings.openrouter_app_url,
            app_title=settings.openrouter_app_title,
            data_collection=settings.openrouter_data_collection,
        )
    return CachedEmbedder(inner, store)


def build_chat(settings: Settings) -> ChatModel | None:
    if not settings.openrouter_configured:
        return None
    assert settings.openrouter_api_key is not None
    return OpenRouterClient(
        api_key=settings.openrouter_api_key.get_secret_value(),
        base_url=settings.openrouter_base_url,
        default_model=settings.generation_model,
        default_fallbacks=settings.generation_fallbacks,
        structured_output_mode=settings.structured_output_mode,
        timeout=settings.openrouter_timeout_seconds,
        max_retries=settings.openrouter_max_retries,
        app_url=settings.openrouter_app_url,
        app_title=settings.openrouter_app_title,
        data_collection=settings.openrouter_data_collection,
    )


def build_container(
    settings: Settings,
    *,
    store: VectorStore | None = None,
    embedder: Embedder | None = None,
    chat: ChatModel | None = None,
    judge_chat: ChatModel | None = None,
) -> Container:
    catalog = load_catalog(str(settings.config_dir), settings.brand_name)
    store = store or build_store(settings)
    embedder = embedder or build_embedder(settings, store)
    chat = chat if chat is not None else build_chat(settings)
    retriever = HybridRetriever(
        store,
        embedder,
        candidates_per_channel=settings.retrieval_candidates_per_channel,
        rrf_k=settings.rrf_k,
        min_semantic_similarity=settings.min_semantic_similarity,
    )
    planner = RetrievalPlanner(catalog)
    evidence = EvidenceBuilder(
        retriever, settings.evidence_token_budget, settings.max_evidence_items
    )
    judge_backend = judge_chat or chat
    judge = (
        EntailmentJudge(
            judge_backend,
            settings.verifier_model,
            settings.verifier_fallbacks,
            batch_size=settings.verifier_batch_size,
            max_tokens=settings.verifier_max_tokens,
            concurrency=settings.verifier_concurrency,
        )
        if judge_backend is not None and settings.verification_enable_entailment
        else None
    )
    verifier = Verifier(
        catalog.brand_voice,
        judge,
        forbid_dashes=settings.style_forbid_dashes,
        require_entailment=settings.verification_enable_entailment,
        min_groundedness=settings.verification_min_groundedness,
    )
    service = GenerationService(
        catalog,
        planner,
        evidence,
        chat,
        verifier,
        store,
        GenerationSettings(
            brand_name=settings.brand_name,
            generation_model=settings.generation_model,
            generation_fallbacks=settings.generation_fallbacks,
            temperature=settings.generation_temperature,
            max_tokens=settings.generation_max_tokens,
            max_repair_attempts=settings.verification_max_repair_attempts,
            min_groundedness=settings.verification_min_groundedness,
            forbid_dashes=settings.style_forbid_dashes,
            pii_redaction=settings.pii_redaction,
            use_entailment=settings.verification_enable_entailment,
            safeguarding_enabled=settings.safeguarding_enabled,
        ),
        safeguarding=build_safeguarding(settings),
    )
    return Container(
        settings, catalog, store, embedder, chat, retriever, planner, evidence, verifier, service
    )

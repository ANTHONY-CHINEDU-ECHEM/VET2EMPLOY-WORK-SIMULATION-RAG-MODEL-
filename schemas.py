"""Public response models. These define the OpenAPI contract the platform integrates with."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from v2e_rag.llm.schemas import GeneratedTask, ManagerReply


class EvidenceReference(BaseModel):
    evidence_id: str = Field(
        description="Short id used in citations within this response, such as E3."
    )
    chunk_id: str = Field(description="Stable knowledge base chunk id. Use it with /v1/verify.")
    citation: str = Field(description="Human readable source label.")
    source_type: str
    facet: str
    source_url: str | None = None
    retrieval_purpose: str
    score: float


class ViolationOut(BaseModel):
    claim_id: str
    check: str
    severity: str
    message: str
    details: list[str] = Field(default_factory=list)


class VerificationOut(BaseModel):
    verdict: str = Field(
        description=(
            "approved, approved_with_pruning, abstained (no evidence answers the message) or "
            "intercepted (the safeguarding screen replaced the reply). Error responses carry "
            "rejected or unavailable reports in details.verification."
        )
    )
    groundedness: float = Field(
        description="Share of factual claims supported by evidence before pruning."
    )
    claims_total: int
    claims_factual: int
    claims_supported: int
    violations: list[ViolationOut]
    pruned: list[str]
    checks_run: list[str]
    repair_attempts: int
    entailment_model: str | None = None
    entailment_error: str | None = None


class GenerationMeta(BaseModel):
    audit_id: str = Field(description="Audit record id. Quote it when reporting an issue.")
    model: str
    prompt_version: str
    latency_ms: int
    usage: dict[str, int] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class TaskResponse(BaseModel):
    task: GeneratedTask
    evidence: list[EvidenceReference]
    verification: VerificationOut
    meta: GenerationMeta


class SafeguardingOut(BaseModel):
    flagged: bool = Field(description="True when any safeguarding category matched.")
    categories: list[str] = Field(
        default_factory=list,
        description="Matched categories from config/safeguarding.yaml, such as wellbeing.",
    )
    action: Literal["none", "flag", "intercept"] = Field(
        description=(
            "none, flag (reply generated, human review forced) or intercept (no model was "
            "called; a fixed support message was returned)."
        )
    )


class ManagerReplyResponse(BaseModel):
    reply: ManagerReply
    evidence: list[EvidenceReference]
    verification: VerificationOut
    safeguarding: SafeguardingOut
    meta: GenerationMeta


class RetrievedEvidence(EvidenceReference):
    text: str


class RetrieveResponse(BaseModel):
    evidence: list[RetrievedEvidence]
    grounding_prompt: str = Field(
        description="Ready to use system prompt block containing the grounding rules and this evidence."
    )
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class VerifyResponse(BaseModel):
    verification: VerificationOut
    evidence: list[EvidenceReference]


class TrackOut(BaseModel):
    code: str
    name: str
    role_family: str
    manager_title: str
    activity_count: int | None = Field(
        default=None, description="Highest valid activity_number for this track."
    )


class CatalogResponse(BaseModel):
    tracks: list[TrackOut]
    role_families: list[str]
    sectors: list[str]
    job_levels: list[str]
    task_types: list[str]
    knowledge_base: dict[str, Any]


class ErrorResponse(BaseModel):
    error: str = Field(description="Stable machine readable code, such as insufficient_evidence.")
    message: str = Field(description="Human readable explanation. Safe to log.")
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = Field(
        default=None, description="Correlates with the X-Request-ID header and service logs."
    )


class DocumentOut(BaseModel):
    document_id: str
    source_file: str
    title: str
    source_type: str
    content_hash: str
    collected_on: date | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestionOutcomeOut(BaseModel):
    file: str
    status: str
    documents: int = 0
    chunks: int = 0
    records: int = 0
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0


class IngestionRunOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    status: str = Field(description="running, completed, failed or conflict.")
    started_at: str | None = None
    finished_at: str | None = None
    totals: dict[str, int] | None = None
    files: list[IngestionOutcomeOut] | None = None
    pruned: list[str] | None = None
    notes: list[str] | None = None
    error: str | None = None


class RankedItem(BaseModel):
    name: str
    postings: int
    share: float


class RatingSummary(BaseModel):
    mean: float
    employers: int
    min: float
    max: float


class RoleProfileResponse(BaseModel):
    filters: dict[str, str | None]
    postings: int
    employers: int
    job_levels: dict[str, int]
    top_skills: list[RankedItem]
    top_tools: list[RankedItem]
    culture_ratings: dict[str, RatingSummary]
    rating_basis: str
    sample_employers: list[str]
    sources: list[str]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, Any]

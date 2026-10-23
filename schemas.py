"""Public response models. These define the OpenAPI contract the platform integrates with."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

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
    verdict: str = Field(description="approved, approved_with_pruning, abstained or rejected.")
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


class ManagerReplyResponse(BaseModel):
    reply: ManagerReply
    evidence: list[EvidenceReference]
    verification: VerificationOut
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


class CatalogResponse(BaseModel):
    tracks: list[TrackOut]
    role_families: list[str]
    sectors: list[str]
    job_levels: list[str]
    task_types: list[str]
    knowledge_base: dict[str, Any]


class ErrorResponse(BaseModel):
    error: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)

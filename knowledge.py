"""Knowledge endpoints: retrieval for external prompts, verification, catalogue and insights."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from v2e_rag.api.routes.generation import ERROR_RESPONSES
from v2e_rag.api.schemas import (
    CatalogResponse,
    ErrorResponse,
    EvidenceReference,
    RetrievedEvidence,
    RetrieveResponse,
    RoleProfileResponse,
    TrackOut,
    VerificationOut,
    VerifyResponse,
)
from v2e_rag.api.security import require_api_key
from v2e_rag.domain.taxonomy import JobLevel, RoleFamily, Sector, TaskType
from v2e_rag.generation.requests import RetrieveRequest, VerifyRequest
from v2e_rag.generation.service import public_evidence
from v2e_rag.insights.role_profile import build_role_profile

router = APIRouter(
    prefix="/v1",
    tags=["Knowledge"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"model": ErrorResponse, "description": "Missing or invalid API key"}},
)


@router.post("/retrieve", response_model=RetrieveResponse, responses=ERROR_RESPONSES)
async def retrieve(body: RetrieveRequest, request: Request) -> RetrieveResponse:
    """Hybrid retrieval only. Returns cited evidence and a grounding prompt so existing
    OpenRouter calls in the platform can be grounded without changing their flow."""
    container = request.app.state.container
    async with asyncio.timeout(container.settings.request_timeout_seconds):
        pack, prompt = await container.service.retrieve(body)
    texts = {item.evidence_id: item.text for item in pack.items}
    return RetrieveResponse(
        evidence=[
            RetrievedEvidence(**ref, text=texts[ref["evidence_id"]])
            for ref in public_evidence(pack)
        ],
        grounding_prompt=prompt,
        diagnostics=pack.diagnostics,
    )


@router.post("/verify", response_model=VerifyResponse, responses=ERROR_RESPONSES)
async def verify(body: VerifyRequest, request: Request) -> VerifyResponse:
    """Verify any model output (for example one produced directly through OpenRouter)
    against the knowledge base chunks it was grounded on. Unknown chunk ids are rejected
    with ``422 unknown_chunks`` rather than silently ignored."""
    container = request.app.state.container
    async with asyncio.timeout(container.settings.request_timeout_seconds):
        report, pack = await container.service.verify_external(body)
    return VerifyResponse(
        verification=VerificationOut(**report.to_dict()),
        evidence=[EvidenceReference(**e) for e in public_evidence(pack)],
    )


@router.get("/catalog", response_model=CatalogResponse)
async def catalog(request: Request) -> CatalogResponse:
    """Valid filter values and a summary of what the knowledge base contains."""
    container = request.app.state.container
    stats = await container.store.stats()
    return CatalogResponse(
        tracks=[
            TrackOut(
                code=t.code.value,
                name=t.name,
                role_family=t.role_family.value,
                manager_title=t.manager_title,
                activity_count=t.activity_count,
            )
            for t in container.catalog.tracks.values()
        ],
        role_families=[r.value for r in RoleFamily],
        sectors=[s.value for s in Sector],
        job_levels=[j.value for j in JobLevel],
        task_types=[t.value for t in TaskType],
        knowledge_base=stats.__dict__,
    )


@router.get("/insights/role_profile", response_model=RoleProfileResponse)
async def role_profile(
    request: Request,
    role_family: RoleFamily,
    sector: Sector | None = None,
    job_level: JobLevel | None = None,
    top_n: int = Query(default=15, ge=1, le=50),
) -> dict[str, Any]:
    """Deterministic profile of a role computed from structured research records: posting
    counts, the most common skills and tools, and employee culture ratings. No model involved."""
    records = await request.app.state.container.store.role_records(
        role_families=(role_family.value,),
        sectors=(sector.value,) if sector else (),
        job_levels=(job_level.value,) if job_level else (),
    )
    profile = build_role_profile(records, top_n=top_n)
    return {
        "filters": {
            "role_family": role_family.value,
            "sector": sector.value if sector else None,
            "job_level": job_level.value if job_level else None,
        },
        **profile,
    }

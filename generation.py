"""AI Manager endpoints: grounded task generation and in character replies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from v2e_rag.api.schemas import (
    ErrorResponse,
    GenerationMeta,
    ManagerReplyResponse,
    TaskResponse,
    VerificationOut,
)
from v2e_rag.api.security import require_api_key
from v2e_rag.generation.requests import ManagerReplyRequest, TaskRequest
from v2e_rag.generation.service import GenerationOutcome, public_evidence
from v2e_rag.llm.prompts import PROMPT_VERSION

router = APIRouter(prefix="/v1", tags=["AI Manager"], dependencies=[Depends(require_api_key)])

_ERRORS = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
    422: {
        "model": ErrorResponse,
        "description": "Insufficient evidence or output failed grounding",
    },
    502: {"model": ErrorResponse, "description": "Upstream model error"},
    503: {"model": ErrorResponse, "description": "Model or verification unavailable"},
}


def _meta(outcome: GenerationOutcome) -> GenerationMeta:
    return GenerationMeta(
        audit_id=outcome.audit_id,
        model=outcome.model,
        prompt_version=PROMPT_VERSION,
        latency_ms=outcome.latency_ms,
        usage=outcome.usage,
        diagnostics=outcome.diagnostics,
    )


@router.post(
    "/tasks/generate",
    response_model=TaskResponse,
    responses=_ERRORS,
    summary="Generate one grounded work item from the AI Manager",
)
async def generate_task(body: TaskRequest, request: Request) -> TaskResponse:
    """Plans retrieval for the request, generates the task from evidence only, verifies every
    claim, repairs or prunes anything unsupported, and returns the task with its citations."""
    outcome = await request.app.state.container.service.generate_task(body)
    return TaskResponse(
        task=outcome.output,
        evidence=public_evidence(outcome.evidence),
        verification=VerificationOut(**outcome.report.to_dict()),
        meta=_meta(outcome),
    )


@router.post(
    "/manager/reply",
    response_model=ManagerReplyResponse,
    responses=_ERRORS,
    summary="Reply in character to a candidate message",
)
async def manager_reply(body: ManagerReplyRequest, request: Request) -> ManagerReplyResponse:
    """Answers only from evidence. When evidence does not cover the question the manager
    abstains in character and flags the conversation for human review."""
    outcome = await request.app.state.container.service.manager_reply(body)
    return ManagerReplyResponse(
        reply=outcome.output,
        evidence=public_evidence(outcome.evidence),
        verification=VerificationOut(**outcome.report.to_dict()),
        meta=_meta(outcome),
    )

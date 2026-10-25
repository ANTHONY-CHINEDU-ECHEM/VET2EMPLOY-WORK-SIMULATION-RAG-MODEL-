"""AI Manager endpoints: grounded task generation and in character replies."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Request

from v2e_rag.api.schemas import (
    ErrorResponse,
    EvidenceReference,
    GenerationMeta,
    ManagerReplyResponse,
    SafeguardingOut,
    TaskResponse,
    VerificationOut,
)
from v2e_rag.api.security import require_api_key
from v2e_rag.generation.requests import ManagerReplyRequest, TaskRequest
from v2e_rag.generation.service import GenerationOutcome, public_evidence
from v2e_rag.llm.prompts import PROMPT_VERSION
from v2e_rag.llm.schemas import GeneratedTask, ManagerReply

router = APIRouter(prefix="/v1", tags=["AI Manager"], dependencies=[Depends(require_api_key)])

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
    413: {"model": ErrorResponse, "description": "Request body too large"},
    422: {
        "model": ErrorResponse,
        "description": (
            "invalid_request, insufficient_evidence (nothing was generated) or "
            "grounding_failed (output failed verification; see details.audit_id)"
        ),
    },
    502: {
        "model": ErrorResponse,
        "description": "upstream_model_error or invalid_model_output. Retry with backoff.",
    },
    503: {
        "model": ErrorResponse,
        "description": "verification_unavailable or service_unavailable. Retry with backoff.",
    },
    504: {"model": ErrorResponse, "description": "generation_timeout. Retry with backoff."},
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


def _deadline(request: Request) -> float:
    seconds: float = request.app.state.container.settings.request_timeout_seconds
    return seconds


@router.post(
    "/tasks/generate",
    response_model=TaskResponse,
    responses=ERROR_RESPONSES,
    summary="Generate one grounded work item from the AI Manager",
)
async def generate_task(body: TaskRequest, request: Request) -> TaskResponse:
    """Plans retrieval for the request, generates the task from evidence only, verifies every
    claim, repairs or prunes anything unsupported, and returns the task with its citations."""
    async with asyncio.timeout(_deadline(request)):
        outcome = await request.app.state.container.service.generate_task(body)
    assert isinstance(outcome.output, GeneratedTask)
    return TaskResponse(
        task=outcome.output,
        evidence=[EvidenceReference(**e) for e in public_evidence(outcome.evidence)],
        verification=VerificationOut(**outcome.report.to_dict()),
        meta=_meta(outcome),
    )


@router.post(
    "/manager/reply",
    response_model=ManagerReplyResponse,
    responses=ERROR_RESPONSES,
    summary="Reply in character to a candidate message",
)
async def manager_reply(body: ManagerReplyRequest, request: Request) -> ManagerReplyResponse:
    """Answers only from evidence. When evidence does not cover the question the manager
    abstains in character and flags the conversation for human review. Messages that match
    the safeguarding screen always set ``reply.needs_human_review``; crisis messages are
    answered with a fixed support message without calling a model."""
    async with asyncio.timeout(_deadline(request)):
        outcome = await request.app.state.container.service.manager_reply(body)
    assert isinstance(outcome.output, ManagerReply)
    safeguarding = outcome.safeguarding or {"flagged": False, "categories": [], "action": "none"}
    return ManagerReplyResponse(
        reply=outcome.output,
        evidence=[EvidenceReference(**e) for e in public_evidence(outcome.evidence)],
        verification=VerificationOut(**outcome.report.to_dict()),
        safeguarding=SafeguardingOut(**safeguarding),
        meta=_meta(outcome),
    )

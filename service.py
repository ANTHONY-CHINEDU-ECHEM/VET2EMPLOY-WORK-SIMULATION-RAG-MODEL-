"""Grounded generation service: the single entry point used by the API and the CLI.

Pipeline for every generation:

    plan retrieval -> execute plan -> abstain if required evidence is missing
    -> generate with evidence only -> validate schema (repair once if invalid)
    -> verify every claim -> repair with the verification findings
    -> verify again -> prune what is still unsupported -> apply style rules
    -> audit everything -> return the output with its citations and report
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from v2e_rag.domain.catalog import Catalog
from v2e_rag.domain.models import EvidenceItem, EvidencePack
from v2e_rag.domain.taxonomy import TaskType
from v2e_rag.generation.pii import redact
from v2e_rag.generation.pruning import (
    PruningError,
    finalise_reply,
    finalise_task,
    prune_reply,
    prune_task,
)
from v2e_rag.generation.requests import (
    ManagerReplyRequest,
    RetrieveRequest,
    TaskRequest,
    VerifyRequest,
)
from v2e_rag.llm.openrouter import ChatModel, LLMOutputError
from v2e_rag.llm.prompts import (
    PROMPT_VERSION,
    grounding_prompt_for_external_use,
    manager_messages,
    repair_messages,
    task_messages,
)
from v2e_rag.llm.schemas import GeneratedTask, ManagerReply
from v2e_rag.observability.logging import get_logger
from v2e_rag.observability.metrics import ABSTENTIONS, VERIFICATION_OUTCOMES
from v2e_rag.retrieval.evidence import EvidenceBuilder, InsufficientEvidenceError
from v2e_rag.retrieval.planner import RetrievalPlanner
from v2e_rag.store.base import VectorStore
from v2e_rag.verification.claims import Claim, reply_claims, split_sentences, task_claims
from v2e_rag.verification.verifier import VerificationReport, Verifier

log = get_logger(__name__)

ANSWER_STEPS = {"brief_answer", "programme_answer", "market_context"}

_AI_QUESTION = (
    "are you an ai",
    "are you a bot",
    "are you real",
    "is this an ai",
    "am i talking to",
    "are you a human",
    "real person",
    "are you human",
)


class GenerationRejected(Exception):
    """Output could not be verified as grounded. Nothing is returned to the candidate."""

    def __init__(self, message: str, report: VerificationReport | None, audit_id: str) -> None:
        super().__init__(message)
        self.report = report
        self.audit_id = audit_id


class VerificationUnavailable(GenerationRejected):
    """The entailment judge could not run while verification is required."""


@dataclass
class GenerationSettings:
    brand_name: str
    generation_model: str
    generation_fallbacks: list[str]
    temperature: float
    max_tokens: int
    max_repair_attempts: int
    min_groundedness: float
    forbid_dashes: bool
    pii_redaction: bool
    use_entailment: bool
    max_pruned_share: float = 0.35


@dataclass
class GenerationOutcome:
    audit_id: str
    output: BaseModel
    evidence: EvidencePack
    report: VerificationReport
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)


def public_evidence(pack: EvidencePack) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": i.evidence_id,
            "chunk_id": i.chunk_id,
            "citation": i.citation,
            "source_type": i.source_type.value,
            "facet": i.facet.value,
            "source_url": i.source_url,
            "retrieval_purpose": i.retrieval_purpose,
            "score": i.score,
        }
        for i in pack.items
    ]


class GenerationService:
    def __init__(
        self,
        catalog: Catalog,
        planner: RetrievalPlanner,
        evidence_builder: EvidenceBuilder,
        chat: ChatModel | None,
        verifier: Verifier,
        store: VectorStore,
        settings: GenerationSettings,
    ) -> None:
        self.catalog = catalog
        self.planner = planner
        self.evidence = evidence_builder
        self.chat = chat
        self.verifier = verifier
        self.store = store
        self.settings = settings

    # Shared helpers ----------------------------------------------------------------------

    def _require_chat(self) -> ChatModel:
        if self.chat is None:
            raise RuntimeError("OpenRouter is not configured. Set OPENROUTER_API_KEY.")
        return self.chat

    def _allowlist(self, *names: str | None) -> set[str]:
        words: set[str] = {self.settings.brand_name}
        for track in self.catalog.tracks.values():
            words.update(track.name.split())
            words.update(track.manager_title.split())
        for name in names:
            if name:
                words.update(name.split())
        return words

    async def _audit(
        self,
        audit_id: str,
        operation: str,
        status: str,
        request: BaseModel | dict[str, Any],
        pack: EvidencePack | None,
        response: dict[str, Any] | None,
        report: VerificationReport | None,
        model: str | None,
        started: float,
        usage: dict[str, int] | None,
    ) -> None:
        entry = {
            "audit_id": audit_id,
            "created_at": datetime.now(UTC).isoformat(),
            "operation": operation,
            "status": status,
            "request": request.model_dump(mode="json")
            if isinstance(request, BaseModel)
            else request,
            "evidence": [
                {"evidence_id": i.evidence_id, "chunk_id": i.chunk_id, "citation": i.citation}
                for i in pack.items
            ]
            if pack
            else [],
            "response": response,
            "verification": report.to_dict() if report else None,
            "model": model,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "usage": usage,
            "prompt_version": PROMPT_VERSION,
        }
        try:
            await self.store.record_audit(entry)
        except Exception as exc:  # audit must never break a user request
            log.error("audit_write_failed", audit_id=audit_id, error=str(exc))

    @staticmethod
    def _merge_usage(total: dict[str, int], extra: dict[str, int]) -> None:
        for key, value in extra.items():
            total[key] = total.get(key, 0) + value

    async def _generate_validated(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        schema_name: str,
        purpose: str,
        usage: dict[str, int],
    ) -> tuple[BaseModel, dict[str, Any], str]:
        chat = self._require_chat()
        result = await chat.complete_json(
            messages,
            purpose=purpose,
            schema_name=schema_name,
            schema=schema.model_json_schema(),
            model=self.settings.generation_model,
            fallbacks=self.settings.generation_fallbacks,
            temperature=self.settings.temperature,
            max_tokens=self.settings.max_tokens,
        )
        self._merge_usage(usage, result.usage)
        try:
            return schema.model_validate(result.data), result.data or {}, result.model
        except ValidationError as exc:
            problems = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()][
                :20
            ]
            retry = await chat.complete_json(
                repair_messages(
                    messages, result.data or {}, ["The JSON does not match the schema.", *problems]
                ),
                purpose=purpose,
                schema_name=schema_name,
                schema=schema.model_json_schema(),
                model=self.settings.generation_model,
                fallbacks=self.settings.generation_fallbacks,
                temperature=0.0,
                max_tokens=self.settings.max_tokens,
            )
            self._merge_usage(usage, retry.usage)
            try:
                return schema.model_validate(retry.data), retry.data or {}, retry.model
            except ValidationError as final:
                raise LLMOutputError(f"Model output did not match the schema: {final}") from final

    # Task generation ---------------------------------------------------------------------

    async def generate_task(self, request: TaskRequest) -> GenerationOutcome:
        started = time.perf_counter()
        audit_id = str(uuid.uuid4())
        usage: dict[str, int] = {}
        plan = self.planner.plan_task(request)
        try:
            pack, _ = await self.evidence.execute(plan)
        except InsufficientEvidenceError:
            ABSTENTIONS.labels(operation="task").inc()
            await self._audit(
                audit_id,
                "task",
                "insufficient_evidence",
                request,
                None,
                None,
                None,
                None,
                started,
                None,
            )
            raise

        track = self.catalog.track(request.track) if request.track else None
        track_label = f"{track.code.value} ({track.name})" if track else None
        manager_title = track.manager_title if track else "Line Manager"
        messages = task_messages(
            request,
            pack.render(),
            self.settings.brand_name,
            track_label,
            self.catalog.brand_voice.display_terms,
            manager_title,
        )
        task, raw, model = await self._generate_validated(
            messages, GeneratedTask, "generated_task", "task", usage
        )
        assert isinstance(task, GeneratedTask)

        request_text = request.model_dump_json()
        allow = self._allowlist(request.candidate_first_name, request.manager_name)
        report = await self._verify_task(task, pack, request_text, allow)
        attempts = 0
        while report.major and attempts < self.settings.max_repair_attempts:
            if report.entailment_error and self.verifier.require_entailment:
                break
            attempts += 1
            repaired, raw, model = await self._generate_validated(
                repair_messages(messages, raw, report.problems()),
                GeneratedTask,
                "generated_task",
                "task_repair",
                usage,
            )
            assert isinstance(repaired, GeneratedTask)
            task = repaired
            report = await self._verify_task(task, pack, request_text, allow)
        report.repair_attempts = attempts

        if report.entailment_error and self.verifier.require_entailment:
            await self._audit(
                audit_id,
                "task",
                "verification_unavailable",
                request,
                pack,
                raw,
                report,
                model,
                started,
                usage,
            )
            VERIFICATION_OUTCOMES.labels(operation="task", verdict="unavailable").inc()
            raise VerificationUnavailable(
                "Verification is required but the judge model is unavailable.", report, audit_id
            )

        unsupported = {v.claim_id for v in report.major}
        if unsupported:
            try:
                task, pruned = prune_task(task, unsupported)
            except PruningError as exc:
                report.verdict = "rejected"
                await self._audit(
                    audit_id, "task", "rejected", request, pack, raw, report, model, started, usage
                )
                VERIFICATION_OUTCOMES.labels(operation="task", verdict="rejected").inc()
                raise GenerationRejected(str(exc), report, audit_id) from exc
            report.pruned = pruned
            pruned_share = len(pruned) / max(report.claims_factual, 1)
            if pruned_share > self.settings.max_pruned_share:
                report.verdict = "rejected"
                await self._audit(
                    audit_id, "task", "rejected", request, pack, raw, report, model, started, usage
                )
                VERIFICATION_OUTCOMES.labels(operation="task", verdict="rejected").inc()
                raise GenerationRejected(
                    "Too much of the task was unsupported by evidence.", report, audit_id
                )
            report.verdict = "approved_with_pruning"
        else:
            report.verdict = "approved"

        if request.task_type == TaskType.COMPLICATION and not task.complications:
            report.verdict = "rejected"
            await self._audit(
                audit_id, "task", "rejected", request, pack, raw, report, model, started, usage
            )
            VERIFICATION_OUTCOMES.labels(operation="task", verdict="rejected").inc()
            raise GenerationRejected(
                "No complication supported by evidence could be produced.", report, audit_id
            )

        task = finalise_task(task, self.catalog.brand_voice, self.settings.forbid_dashes)
        VERIFICATION_OUTCOMES.labels(operation="task", verdict=report.verdict).inc()
        response = task.model_dump(mode="json")
        await self._audit(
            audit_id, "task", report.verdict, request, pack, response, report, model, started, usage
        )
        return GenerationOutcome(
            audit_id=audit_id,
            output=task,
            evidence=pack,
            report=report,
            model=model,
            usage=usage,
            latency_ms=int((time.perf_counter() - started) * 1000),
            diagnostics=pack.diagnostics,
        )

    async def _verify_task(
        self, task: GeneratedTask, pack: EvidencePack, request_text: str, allow: set[str]
    ) -> VerificationReport:
        return await self.verifier.verify(
            task_claims(task),
            pack,
            request_text,
            allow,
            operation="task",
            use_entailment=self.settings.use_entailment,
        )

    # Manager replies ---------------------------------------------------------------------

    async def manager_reply(self, request: ManagerReplyRequest) -> GenerationOutcome:
        started = time.perf_counter()
        audit_id = str(uuid.uuid4())
        usage: dict[str, int] = {}
        message, redactions = (
            redact(request.candidate_message)
            if self.settings.pii_redaction
            else (request.candidate_message, {})
        )
        history: list[tuple[str, str]] = []
        for turn in request.conversation[-12:]:
            text = redact(turn.content)[0] if self.settings.pii_redaction else turn.content
            history.append((turn.role, text))
        safe_request = request.model_copy(update={"candidate_message": message})
        plan = self.planner.plan_manager_reply(safe_request)
        pack, results = await self.evidence.execute(plan)
        answer_hits = sum(len(r.hits) for r in results if r.step.name in ANSWER_STEPS)

        track = self.catalog.track(request.track) if request.track else None
        manager_title = track.manager_title if track else "Line Manager"
        track_name = f"{track.name} track" if track else "simulation"
        if answer_hits == 0:
            reply = self._abstain_reply(request, manager_title)
            report = VerificationReport("approved", 1.0, 0, 0, 0, checks_run=["abstention"])
            await self._audit(
                audit_id,
                "manager_reply",
                "abstained",
                safe_request,
                pack,
                reply.model_dump(),
                report,
                None,
                started,
                usage,
            )
            ABSTENTIONS.labels(operation="manager_reply").inc()
            return GenerationOutcome(
                audit_id, reply, pack, report, "none", usage, 0, pack.diagnostics
            )

        messages = manager_messages(
            request,
            pack.render(),
            self.settings.brand_name,
            track_name,
            manager_title,
            self.catalog.brand_voice.display_terms,
            message,
            history,
        )
        reply, raw, model = await self._generate_validated(
            messages, ManagerReply, "manager_reply", "manager_reply", usage
        )
        assert isinstance(reply, ManagerReply)
        allow_ai = any(q in request.candidate_message.lower() for q in _AI_QUESTION)
        request_text = (
            f"{message} {request.current_task_title or ''} {request.current_task_summary or ''}"
        )
        allow = self._allowlist(request.candidate_first_name, request.manager_name)

        async def verify(r: ManagerReply) -> VerificationReport:
            return await self.verifier.verify(
                reply_claims(r),
                pack,
                request_text,
                allow,
                "manager_reply",
                use_entailment=self.settings.use_entailment,
                allow_ai_disclosure=allow_ai,
            )

        report = await verify(reply)
        attempts = 0
        while report.major and attempts < self.settings.max_repair_attempts:
            if report.entailment_error and self.verifier.require_entailment:
                break
            attempts += 1
            repaired, raw, model = await self._generate_validated(
                repair_messages(messages, raw, report.problems()),
                ManagerReply,
                "manager_reply",
                "manager_repair",
                usage,
            )
            assert isinstance(repaired, ManagerReply)
            reply = repaired
            report = await verify(reply)
        report.repair_attempts = attempts

        if report.entailment_error and self.verifier.require_entailment:
            await self._audit(
                audit_id,
                "manager_reply",
                "verification_unavailable",
                safe_request,
                pack,
                raw,
                report,
                model,
                started,
                usage,
            )
            raise VerificationUnavailable(
                "Verification is required but the judge model is unavailable.", report, audit_id
            )

        unsupported = {v.claim_id for v in report.major}
        if unsupported:
            try:
                reply, pruned = prune_reply(reply, unsupported)
                report.pruned = pruned
                report.verdict = "approved_with_pruning"
            except PruningError:
                reply = self._abstain_reply(request, manager_title)
                report.pruned = sorted(unsupported)
                report.verdict = "abstained"
        else:
            report.verdict = "approved"
        reply = finalise_reply(reply, self.catalog.brand_voice, self.settings.forbid_dashes)
        VERIFICATION_OUTCOMES.labels(operation="manager_reply", verdict=report.verdict).inc()
        await self._audit(
            audit_id,
            "manager_reply",
            report.verdict,
            safe_request,
            pack,
            reply.model_dump(mode="json"),
            report,
            model,
            started,
            usage,
        )
        diagnostics = pack.diagnostics | {"pii_redactions": redactions}
        return GenerationOutcome(
            audit_id,
            reply,
            pack,
            report,
            model,
            usage,
            int((time.perf_counter() - started) * 1000),
            diagnostics,
        )

    @staticmethod
    def _abstain_reply(request: ManagerReplyRequest, manager_title: str) -> ManagerReply:
        name = f" {request.candidate_first_name}" if request.candidate_first_name else ""
        signoff = request.manager_name or f"Your {manager_title}"
        text = (
            f"Thanks{name}. I want to give you an accurate answer rather than a quick one, so I will check "
            f"and come back to you. In the meantime, note your current assumption so we can review it together.\n\n"
            f"{signoff}"
        )
        return ManagerReply(
            reply=text,
            intent="acknowledge",
            claims=[],
            needs_human_review=True,
            review_reason="The knowledge base does not contain evidence that answers this message.",
        )

    # Retrieval only and external verification --------------------------------------------

    async def retrieve(self, request: RetrieveRequest) -> tuple[EvidencePack, str]:
        plan = self.planner.plan_retrieve(request)
        pack, _ = await self.evidence.execute(plan)
        prompt = grounding_prompt_for_external_use(
            pack.render(), self.settings.brand_name, self.catalog.brand_voice.display_terms
        )
        return pack, prompt

    async def verify_external(
        self, request: VerifyRequest
    ) -> tuple[VerificationReport, EvidencePack]:
        chunks = await self.store.get_chunks(request.chunk_ids)
        items = [
            EvidenceItem(
                evidence_id=f"E{i + 1}",
                chunk_id=c.chunk_id,
                facet=c.facet,
                source_type=c.source_type,
                authority=c.authority,
                citation=c.citation_label(),
                text=c.text,
                source_url=c.source_url,
                score=1.0,
                retrieval_purpose="external_verification",
            )
            for i, c in enumerate(chunks)
        ]
        pack = EvidencePack(items=items)
        ids = [i.evidence_id for i in items]
        claims = [
            Claim(f"sentence[{i}]", "text", s, ids, kind="item")
            for i, s in enumerate(split_sentences(request.text))
        ]
        report = await self.verifier.verify(
            claims,
            pack,
            "",
            self._allowlist(),
            operation="external_verify",
            use_entailment=request.use_entailment and self.settings.use_entailment,
        )
        if report.verdict != "rejected" and report.groundedness < self.settings.min_groundedness:
            report.verdict = "rejected"
        return report, pack

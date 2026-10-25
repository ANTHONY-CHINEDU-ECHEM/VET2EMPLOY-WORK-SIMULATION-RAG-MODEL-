"""Grounded generation service: the single entry point used by the API and the CLI.

Pipeline for every generation:

    validate against the catalogue -> plan retrieval -> execute plan
    -> abstain if required evidence is missing -> generate with evidence only
    -> validate schema (repair once if invalid) -> verify every claim
    -> repair with the verification findings -> verify again -> prune what is still
    unsupported -> apply style rules -> audit everything -> return the output with its
    citations and report

Manager replies additionally pass through the safeguarding screen before anything else
and have personal data redacted from every free text field before any model call.

Every outcome is audited, including model failures, so any request id or audit id can be
traced to exactly what happened.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from v2e_rag.domain.catalog import Catalog
from v2e_rag.domain.models import EvidenceItem, EvidencePack
from v2e_rag.domain.taxonomy import TaskType
from v2e_rag.errors import InvalidRequestError, ServiceNotConfiguredError, UnknownChunksError
from v2e_rag.generation.pii import redact
from v2e_rag.generation.pruning import (
    PruningError,
    finalise_reply,
    finalise_task,
    prune_reply,
    prune_task,
)
from v2e_rag.generation.requests import (
    ConversationTurn,
    ManagerReplyRequest,
    RetrieveRequest,
    TaskRequest,
    VerifyRequest,
)
from v2e_rag.llm.openrouter import ChatModel, LLMError, LLMOutputError
from v2e_rag.llm.prompts import (
    PROMPT_VERSION,
    grounding_prompt_for_external_use,
    manager_messages,
    repair_messages,
    task_messages,
)
from v2e_rag.llm.schemas import GeneratedTask, ManagerReply
from v2e_rag.observability.logging import get_logger
from v2e_rag.observability.metrics import ABSTENTIONS, SAFEGUARDING_FLAGS, VERIFICATION_OUTCOMES
from v2e_rag.retrieval.evidence import EvidenceBuilder, InsufficientEvidenceError
from v2e_rag.retrieval.planner import RetrievalPlanner
from v2e_rag.safeguarding import SafeguardingPolicy, ScreenResult, intercept_text, screen
from v2e_rag.store.base import VectorStore
from v2e_rag.verification.claims import Claim, reply_claims, split_sentences, task_claims
from v2e_rag.verification.verifier import VerificationReport, Verifier

log = get_logger(__name__)

ANSWER_STEPS = {"brief_answer", "programme_answer", "market_context"}
HISTORY_TURNS = 12

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

ModelT = TypeVar("ModelT", bound=BaseModel)


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
    safeguarding_enabled: bool = True


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
    safeguarding: dict[str, Any] | None = None


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


def _current_request_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("request_id")
    return value if isinstance(value, str) else None


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
        safeguarding: SafeguardingPolicy | None = None,
    ) -> None:
        self.catalog = catalog
        self.planner = planner
        self.evidence = evidence_builder
        self.chat = chat
        self.verifier = verifier
        self.store = store
        self.settings = settings
        self.safeguarding = safeguarding if settings.safeguarding_enabled else None

    # Shared helpers ----------------------------------------------------------------------

    def _require_chat(self) -> ChatModel:
        if self.chat is None:
            raise ServiceNotConfiguredError("OpenRouter is not configured. Set OPENROUTER_API_KEY.")
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

    def _redact(self, text: str | None, counts: dict[str, int]) -> str | None:
        if text is None or not self.settings.pii_redaction:
            return text
        clean, found = redact(text)
        for key, value in found.items():
            counts[key] = counts.get(key, 0) + value
        return clean

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
        extra: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "audit_id": audit_id,
            "request_id": _current_request_id(),
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
            **(extra or {}),
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
        schema: type[ModelT],
        schema_name: str,
        purpose: str,
        usage: dict[str, int],
    ) -> tuple[ModelT, dict[str, Any], str]:
        chat = self._require_chat()
        json_schema = schema.model_json_schema()
        result = await chat.complete_json(
            messages,
            purpose=purpose,
            schema_name=schema_name,
            schema=json_schema,
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
                schema=json_schema,
                model=self.settings.generation_model,
                fallbacks=self.settings.generation_fallbacks,
                temperature=0.0,
                max_tokens=self.settings.max_tokens,
            )
            self._merge_usage(usage, retry.usage)
            try:
                return schema.model_validate(retry.data), retry.data or {}, retry.model
            except ValidationError as final:
                raise LLMOutputError(
                    f"Model output did not match the schema after one repair: "
                    f"{final.error_count()} problem(s)."
                ) from final

    async def _model_failure(
        self,
        exc: LLMError,
        audit_id: str,
        operation: str,
        request: BaseModel,
        pack: EvidencePack | None,
        started: float,
        usage: dict[str, int],
    ) -> None:
        """Audit a model failure and attach the audit id so the API can return it."""
        status = "invalid_model_output" if isinstance(exc, LLMOutputError) else "model_error"
        await self._audit(
            audit_id,
            operation,
            status,
            request,
            pack,
            None,
            None,
            None,
            started,
            usage,
            {"error": str(exc)[:500]},
        )
        VERIFICATION_OUTCOMES.labels(operation=operation, verdict=status).inc()
        exc.audit_id = audit_id  # type: ignore[attr-defined]

    def validate_task_request(self, request: TaskRequest) -> None:
        """Reject requests that contradict the configured catalogue before any retrieval."""
        if request.track is None or request.activity_number is None:
            return
        track = self.catalog.track(request.track)
        if track.activity_count and request.activity_number > track.activity_count:
            raise InvalidRequestError(
                f"Track {track.code.value} defines {track.activity_count} activities; "
                f"activity_number {request.activity_number} does not exist.",
                {"activity_count": track.activity_count},
            )

    # Task generation ---------------------------------------------------------------------

    async def generate_task(self, request: TaskRequest) -> GenerationOutcome:
        started = time.perf_counter()
        audit_id = str(uuid.uuid4())
        usage: dict[str, int] = {}
        self.validate_task_request(request)
        self._require_chat()
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
        request_text = request.model_dump_json()
        allow = self._allowlist(request.candidate_first_name, request.manager_name)
        try:
            task, raw, model = await self._generate_validated(
                messages, GeneratedTask, "generated_task", "task", usage
            )
            report = await self._verify_task(task, pack, request_text, allow)
            attempts = 0
            while report.major and attempts < self.settings.max_repair_attempts:
                if report.entailment_error and self.verifier.require_entailment:
                    break
                attempts += 1
                task, raw, model = await self._generate_validated(
                    repair_messages(messages, raw, report.problems()),
                    GeneratedTask,
                    "generated_task",
                    "task_repair",
                    usage,
                )
                report = await self._verify_task(task, pack, request_text, allow)
            report.repair_attempts = attempts
        except LLMError as exc:
            await self._model_failure(exc, audit_id, "task", request, pack, started, usage)
            raise

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

        async def reject(message: str, cause: Exception | None = None) -> GenerationRejected:
            report.verdict = "rejected"
            await self._audit(
                audit_id, "task", "rejected", request, pack, raw, report, model, started, usage
            )
            VERIFICATION_OUTCOMES.labels(operation="task", verdict="rejected").inc()
            error = GenerationRejected(message, report, audit_id)
            error.__cause__ = cause
            return error

        unsupported = {v.claim_id for v in report.major}
        if unsupported:
            try:
                task, pruned = prune_task(task, unsupported)
            except PruningError as exc:
                raise await reject(str(exc), exc) from exc
            report.pruned = pruned
            pruned_share = len(pruned) / max(report.claims_factual, 1)
            if pruned_share > self.settings.max_pruned_share:
                raise await reject("Too much of the task was unsupported by evidence.")
            report.verdict = "approved_with_pruning"
        else:
            report.verdict = "approved"

        if request.task_type == TaskType.COMPLICATION and not task.complications:
            raise await reject("No complication supported by evidence could be produced.")

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

    def _safe_reply_request(
        self, request: ManagerReplyRequest, redactions: dict[str, int]
    ) -> ManagerReplyRequest:
        """A copy of the request with personal data removed from every free text field."""
        turns = [
            # model_construct: redaction markers can lengthen text past the input limit.
            ConversationTurn.model_construct(
                role=t.role, content=self._redact(t.content, redactions) or ""
            )
            for t in request.conversation[-HISTORY_TURNS:]
        ]
        return request.model_copy(
            update={
                "candidate_message": self._redact(request.candidate_message, redactions),
                "current_task_title": self._redact(request.current_task_title, redactions),
                "current_task_summary": self._redact(request.current_task_summary, redactions),
                "conversation": turns,
            }
        )

    async def manager_reply(self, request: ManagerReplyRequest) -> GenerationOutcome:
        started = time.perf_counter()
        audit_id = str(uuid.uuid4())
        usage: dict[str, int] = {}
        redactions: dict[str, int] = {}
        safe_request = self._safe_reply_request(request, redactions)

        track = self.catalog.track(request.track) if request.track else None
        manager_title = track.manager_title if track else "Line Manager"
        track_name = f"{track.name} track" if track else "simulation"

        screened = (
            screen(request.candidate_message, self.safeguarding)
            if self.safeguarding is not None
            else ScreenResult()
        )
        for category in screened.categories:
            SAFEGUARDING_FLAGS.labels(category=category, action=screened.action).inc()
        if screened.action == "intercept":
            return await self._intercept(
                request, safe_request, screened, audit_id, started, redactions
            )

        self._require_chat()
        plan = self.planner.plan_manager_reply(safe_request)
        pack, results = await self.evidence.execute(plan)
        answer_hits = sum(len(r.hits) for r in results if r.step.name in ANSWER_STEPS)
        diagnostics = pack.diagnostics | {"pii_redactions": redactions}

        if answer_hits == 0:
            reply = self._abstain_reply(request, manager_title)
            reply = self._apply_screen(reply, screened)
            report = VerificationReport("abstained", 1.0, 0, 0, 0, checks_run=["abstention"])
            await self._audit(
                audit_id,
                "manager_reply",
                "abstained",
                safe_request,
                pack,
                reply.model_dump(mode="json"),
                report,
                None,
                started,
                usage,
                {"safeguarding": screened.to_dict()},
            )
            ABSTENTIONS.labels(operation="manager_reply").inc()
            VERIFICATION_OUTCOMES.labels(operation="manager_reply", verdict="abstained").inc()
            return GenerationOutcome(
                audit_id,
                reply,
                pack,
                report,
                "none",
                usage,
                int((time.perf_counter() - started) * 1000),
                diagnostics,
                screened.to_dict(),
            )

        message = safe_request.candidate_message
        history: list[tuple[str, str]] = [(t.role, t.content) for t in safe_request.conversation]
        messages = manager_messages(
            safe_request,
            pack.render(),
            self.settings.brand_name,
            track_name,
            manager_title,
            self.catalog.brand_voice.display_terms,
            message,
            history,
        )
        allow_ai = any(q in request.candidate_message.lower() for q in _AI_QUESTION)
        # Only platform supplied context may license numbers and names in the reply. The
        # candidate's own message is deliberately excluded: a fact the candidate asserts
        # must never become something the manager confirms.
        request_text = (
            f"{safe_request.current_task_title or ''} {safe_request.current_task_summary or ''}"
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

        try:
            reply, raw, model = await self._generate_validated(
                messages, ManagerReply, "manager_reply", "manager_reply", usage
            )
            report = await verify(reply)
            attempts = 0
            while report.major and attempts < self.settings.max_repair_attempts:
                if report.entailment_error and self.verifier.require_entailment:
                    break
                attempts += 1
                reply, raw, model = await self._generate_validated(
                    repair_messages(messages, raw, report.problems()),
                    ManagerReply,
                    "manager_reply",
                    "manager_repair",
                    usage,
                )
                report = await verify(reply)
            report.repair_attempts = attempts
        except LLMError as exc:
            await self._model_failure(
                exc, audit_id, "manager_reply", safe_request, pack, started, usage
            )
            raise

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
            VERIFICATION_OUTCOMES.labels(operation="manager_reply", verdict="unavailable").inc()
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
        reply = self._apply_screen(reply, screened)
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
            {"safeguarding": screened.to_dict()},
        )
        return GenerationOutcome(
            audit_id,
            reply,
            pack,
            report,
            model,
            usage,
            int((time.perf_counter() - started) * 1000),
            diagnostics,
            screened.to_dict(),
        )

    @staticmethod
    def _apply_screen(reply: ManagerReply, screened: ScreenResult) -> ManagerReply:
        """The screen only ever adds caution: it forces review, never removes it."""
        if not screened.flagged:
            return reply
        reasons = [r for r in (reply.review_reason, screened.review_reason) if r]
        reason = " ".join(dict.fromkeys(reasons))[:500] or None
        return reply.model_copy(update={"needs_human_review": True, "review_reason": reason})

    async def _intercept(
        self,
        request: ManagerReplyRequest,
        safe_request: ManagerReplyRequest,
        screened: ScreenResult,
        audit_id: str,
        started: float,
        redactions: dict[str, int],
    ) -> GenerationOutcome:
        """Crisis path: no retrieval, no model. A fixed message written by people."""
        assert self.safeguarding is not None
        reply = ManagerReply(
            reply=intercept_text(self.safeguarding, request.candidate_first_name),
            intent="escalate",
            claims=[],
            needs_human_review=True,
            review_reason=(screened.review_reason or "Safeguarding concern.")[:500],
        )
        report = VerificationReport("intercepted", 1.0, 0, 0, 0, checks_run=["safeguarding"])
        pack = EvidencePack()
        log.warning("safeguarding_intercept", audit_id=audit_id, categories=screened.categories)
        await self._audit(
            audit_id,
            "manager_reply",
            "intercepted",
            safe_request,
            pack,
            reply.model_dump(mode="json"),
            report,
            None,
            started,
            {},
            {"safeguarding": screened.to_dict()},
        )
        VERIFICATION_OUTCOMES.labels(operation="manager_reply", verdict="intercepted").inc()
        return GenerationOutcome(
            audit_id,
            reply,
            pack,
            report,
            "none",
            {},
            int((time.perf_counter() - started) * 1000),
            {"pii_redactions": redactions},
            screened.to_dict(),
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
        requested = list(dict.fromkeys(request.chunk_ids))
        chunks = await self.store.get_chunks(requested)
        found = {c.chunk_id for c in chunks}
        missing = [cid for cid in requested if cid not in found]
        if missing:
            raise UnknownChunksError(missing)
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

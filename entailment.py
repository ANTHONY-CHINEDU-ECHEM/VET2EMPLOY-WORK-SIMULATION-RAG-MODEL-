"""Semantic entailment check using an independent judge model through OpenRouter.

Each claim is judged only against the passages it cites (or, for free text sentences, the
passages cited anywhere in the output). Passages are sent once per batch and referenced by
id to keep token cost low. A different model from the generator is used by default so the
output is not marking its own work.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from v2e_rag.llm.openrouter import ChatModel, LLMError
from v2e_rag.llm.prompts import judge_messages
from v2e_rag.llm.schemas import JudgeOutput, JudgeVerdict
from v2e_rag.observability.logging import get_logger
from v2e_rag.verification.claims import Claim

log = get_logger(__name__)


@dataclass
class EntailmentResult:
    verdicts: dict[str, JudgeVerdict]
    model: str | None
    usage: dict[str, int]
    error: str | None = None


class EntailmentJudge:
    def __init__(
        self,
        chat: ChatModel,
        model: str,
        fallbacks: list[str] | None = None,
        batch_size: int = 20,
        max_tokens: int = 3000,
    ) -> None:
        self.chat = chat
        self.model = model
        self.fallbacks = fallbacks or []
        self.batch_size = batch_size
        self.max_tokens = max_tokens

    async def judge(
        self, claims: list[Claim], evidence: dict[str, str], sentence_scope: list[str]
    ) -> EntailmentResult:
        verdicts: dict[str, JudgeVerdict] = {}
        usage: dict[str, int] = {}
        used_model: str | None = None
        for start in range(0, len(claims), self.batch_size):
            batch = claims[start : start + self.batch_size]
            needed: list[str] = []
            payload = []
            for claim in batch:
                ids = claim.evidence_ids if claim.kind == "item" else sentence_scope
                ids = [i for i in ids if i in evidence]
                needed.extend(ids)
                payload.append(
                    {"claim_id": claim.claim_id, "text": claim.text, "evidence_ids": ids}
                )
            passages = {eid: evidence[eid] for eid in dict.fromkeys(needed)}
            if not passages:
                for claim in batch:
                    verdicts[claim.claim_id] = JudgeVerdict(
                        claim_id=claim.claim_id,
                        verdict="UNSUPPORTED",
                        rationale="No evidence was cited.",
                    )
                continue
            try:
                result = await self.chat.complete_json(
                    judge_messages(passages, payload),
                    purpose="verification",
                    schema_name="judge_output",
                    schema=JudgeOutput.model_json_schema(),
                    model=self.model,
                    fallbacks=self.fallbacks,
                    temperature=0.0,
                    max_tokens=self.max_tokens,
                )
                parsed = JudgeOutput.model_validate(result.data)
            except (LLMError, ValidationError) as exc:
                log.warning("entailment_judge_failed", error=str(exc))
                return EntailmentResult(
                    verdicts=verdicts, model=used_model, usage=usage, error=str(exc)
                )
            used_model = result.model
            for key, value in result.usage.items():
                usage[key] = usage.get(key, 0) + value
            for verdict in parsed.verdicts:
                verdicts[verdict.claim_id] = verdict
        return EntailmentResult(verdicts=verdicts, model=used_model, usage=usage)

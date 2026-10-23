"""Verification orchestrator.

Runs deterministic checks and (optionally) the entailment judge over every claim, then
produces a report with a verdict:

* ``approved``                every claim is supported;
* ``approved_with_pruning``   unsupported optional items were removed and the remainder is
                              fully supported and still complete;
* ``rejected``                core content is unsupported, or verification could not run
                              while it is required. Rejected output is never returned to a
                              candidate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from v2e_rag.domain.catalog import BrandVoice
from v2e_rag.domain.models import EvidencePack
from v2e_rag.observability.metrics import UNSUPPORTED_CLAIMS
from v2e_rag.verification.checks import (
    MAJOR,
    MINOR,
    Violation,
    check_citations,
    check_entities,
    check_file_names,
    check_numbers,
    check_style,
    check_tools,
)
from v2e_rag.verification.claims import Claim
from v2e_rag.verification.entailment import EntailmentJudge

Verdict = str


@dataclass
class VerificationReport:
    verdict: Verdict
    groundedness: float
    claims_total: int
    claims_factual: int
    claims_supported: int
    violations: list[Violation] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)
    repair_attempts: int = 0
    entailment_model: str | None = None
    entailment_error: str | None = None
    claim_verdicts: dict[str, str] = field(default_factory=dict)

    @property
    def major(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == MAJOR]

    @property
    def minor(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == MINOR]

    def problems(self) -> list[str]:
        return [v.as_problem() for v in self.violations]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "groundedness": round(self.groundedness, 4),
            "claims_total": self.claims_total,
            "claims_factual": self.claims_factual,
            "claims_supported": self.claims_supported,
            "violations": [v.__dict__ for v in self.violations],
            "pruned": self.pruned,
            "checks_run": self.checks_run,
            "repair_attempts": self.repair_attempts,
            "entailment_model": self.entailment_model,
            "entailment_error": self.entailment_error,
        }


class Verifier:
    def __init__(
        self,
        brand: BrandVoice,
        judge: EntailmentJudge | None,
        forbid_dashes: bool = True,
        require_entailment: bool = True,
        min_groundedness: float = 0.9,
    ) -> None:
        self.brand = brand
        self.judge = judge
        self.forbid_dashes = forbid_dashes
        self.require_entailment = require_entailment and judge is not None
        self.min_groundedness = min_groundedness

    async def verify(
        self,
        claims: list[Claim],
        pack: EvidencePack,
        request_text: str,
        extra_allowlist: set[str],
        operation: str,
        use_entailment: bool = True,
        allow_ai_disclosure: bool = False,
    ) -> VerificationReport:
        evidence = {item.evidence_id: item.text for item in pack.items}
        evidence_text = "\n".join(evidence.values())
        violations: list[Violation] = []
        checks = ["citations", "numbers", "entities", "tools", "file_names", "style"]
        violations += check_citations(claims, set(evidence))
        violations += check_numbers(claims, evidence, request_text)
        violations += check_entities(claims, evidence_text, request_text, extra_allowlist)
        violations += check_tools(claims, evidence)
        violations += check_file_names(claims, evidence_text)
        violations += check_style(claims, self.brand, self.forbid_dashes, allow_ai_disclosure)

        claim_verdicts: dict[str, str] = {}
        entailment_model = None
        entailment_error = None
        factual_ids = {c.claim_id for c in claims}
        if self.judge is not None and use_entailment:
            checks.append("entailment")
            cited = list(
                dict.fromkeys(eid for c in claims for eid in c.evidence_ids if eid in evidence)
            )
            result = await self.judge.judge(claims, evidence, cited or list(evidence))
            entailment_model = result.model
            entailment_error = result.error
            for claim in claims:
                verdict = result.verdicts.get(claim.claim_id)
                if verdict is None:
                    if result.error is None:
                        violations.append(
                            Violation(claim.claim_id, "entailment", MINOR, "was not judged")
                        )
                    continue
                claim_verdicts[claim.claim_id] = verdict.verdict
                if verdict.verdict == "NOT_A_CLAIM":
                    factual_ids.discard(claim.claim_id)
                elif verdict.verdict == "UNSUPPORTED":
                    violations.append(
                        Violation(
                            claim.claim_id,
                            "entailment",
                            MAJOR,
                            "is not supported by its evidence",
                            verdict.unsupported_details or [verdict.rationale],
                        )
                    )
                elif verdict.verdict == "PARTIAL":
                    violations.append(
                        Violation(
                            claim.claim_id,
                            "entailment",
                            MAJOR if claim.kind == "item" else MINOR,
                            "is only partly supported by its evidence",
                            verdict.unsupported_details or [verdict.rationale],
                        )
                    )

        unsupported = {v.claim_id for v in violations if v.severity == MAJOR}
        factual = len(factual_ids)
        supported = len(factual_ids - unsupported)
        groundedness = supported / factual if factual else 1.0
        for violation in violations:
            UNSUPPORTED_CLAIMS.labels(operation=operation, check=violation.check).inc()

        outcome = "approved" if not unsupported else "rejected"
        if entailment_error and self.require_entailment and use_entailment:
            outcome = "rejected"
        return VerificationReport(
            verdict=outcome,
            groundedness=groundedness,
            claims_total=len(claims),
            claims_factual=factual,
            claims_supported=supported,
            violations=violations,
            checks_run=checks,
            entailment_model=entailment_model,
            entailment_error=entailment_error,
            claim_verdicts=claim_verdicts,
        )

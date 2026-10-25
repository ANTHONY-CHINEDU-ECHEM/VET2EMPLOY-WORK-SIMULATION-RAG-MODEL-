"""End to end generation evaluation: runs scenarios through the full pipeline and reports
grounding quality, abstention behaviour, safeguarding behaviour and latency.

A scenario can assert more than its outcome:

* ``expect``: approved (approved or approved_with_pruning), abstained, intercepted,
  insufficient_evidence, invalid_request or rejected;
* ``expect_review``: whether ``reply.needs_human_review`` must be set;
* ``expect_safeguarding``: the safeguarding action (none, flag or intercept);
* ``must_not_contain``: strings that must not appear anywhere in the candidate facing
  output (used by the prompt injection and personal data scenarios).

One failing scenario (for example an upstream model error) is recorded and the run
continues, so a single outage never hides the rest of the results.
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from v2e_rag.errors import InvalidRequestError
from v2e_rag.generation.requests import ManagerReplyRequest, TaskRequest
from v2e_rag.generation.service import GenerationRejected, VerificationUnavailable
from v2e_rag.llm.openrouter import LLMError
from v2e_rag.retrieval.evidence import InsufficientEvidenceError


def _percentile(values: list[int], share: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(share * len(ordered)) - 1)
    return ordered[index]


async def _run_scenario(container: Any, scenario: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    row: dict[str, Any] = {
        "id": scenario["id"],
        "kind": scenario["kind"],
        "expect": scenario.get("expect", "approved"),
    }
    checks: list[str] = []
    try:
        if scenario["kind"] == "task":
            outcome = await container.service.generate_task(TaskRequest(**scenario["request"]))
        else:
            outcome = await container.service.manager_reply(
                ManagerReplyRequest(**scenario["request"])
            )
        report = outcome.report
        output = outcome.output.model_dump(mode="json")
        row.update(
            outcome=report.verdict,
            groundedness=report.groundedness,
            violations=len(report.violations),
            pruned=len(report.pruned),
            repairs=report.repair_attempts,
            audit_id=outcome.audit_id,
        )
        if "expect_review" in scenario:
            actual = bool(output.get("needs_human_review"))
            if actual != bool(scenario["expect_review"]):
                checks.append(f"needs_human_review was {actual}")
        if "expect_safeguarding" in scenario:
            action = (outcome.safeguarding or {}).get("action", "none")
            if action != scenario["expect_safeguarding"]:
                checks.append(f"safeguarding action was {action}")
        rendered = json.dumps(output, ensure_ascii=False).lower()
        for needle in scenario.get("must_not_contain", []):
            if str(needle).lower() in rendered:
                checks.append(f"output contains forbidden text {needle!r}")
    except InsufficientEvidenceError as exc:
        row.update(outcome="insufficient_evidence", missing=exc.missing_steps)
    except (ValidationError, InvalidRequestError) as exc:
        row.update(outcome="invalid_request", error=str(exc)[:300])
    except VerificationUnavailable as exc:
        row.update(outcome="verification_unavailable", audit_id=exc.audit_id)
    except GenerationRejected as exc:
        row.update(outcome="rejected", audit_id=exc.audit_id)
    except LLMError as exc:
        row.update(outcome="model_error", error=str(exc)[:300])
    row["latency_ms"] = int((time.perf_counter() - started) * 1000)
    expected = row["expect"]
    outcome_ok = (
        str(row["outcome"]).startswith("approved")
        if expected == "approved"
        else row["outcome"] == expected
    )
    row["failed_checks"] = checks
    row["as_expected"] = outcome_ok and not checks
    return row


async def run_generation_eval(container: Any, dataset: Path) -> dict[str, Any]:
    text = await asyncio.to_thread(dataset.read_text, encoding="utf-8")
    scenarios = yaml.safe_load(text)["scenarios"]
    rows = [await _run_scenario(container, scenario) for scenario in scenarios]

    grounded = [r["groundedness"] for r in rows if "groundedness" in r]
    latencies = [r["latency_ms"] for r in rows]
    summary = {
        "scenarios": len(rows),
        "as_expected_rate": round(sum(r["as_expected"] for r in rows) / max(len(rows), 1), 3),
        "approved": sum(1 for r in rows if str(r["outcome"]).startswith("approved")),
        "abstained_or_insufficient": sum(
            1 for r in rows if r["outcome"] in ("abstained", "insufficient_evidence")
        ),
        "intercepted": sum(1 for r in rows if r["outcome"] == "intercepted"),
        "rejected": sum(1 for r in rows if r["outcome"] == "rejected"),
        "errors": sum(
            1 for r in rows if r["outcome"] in ("model_error", "verification_unavailable")
        ),
        "mean_groundedness_before_pruning": round(statistics.mean(grounded), 3)
        if grounded
        else None,
        "p50_latency_ms": _percentile(latencies, 0.5),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "unexpected": [r["id"] for r in rows if not r["as_expected"]],
    }
    return {"summary": summary, "results": rows}

"""End to end generation evaluation: runs scenarios through the full pipeline and reports
grounding quality, abstention behaviour and latency."""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Any

import yaml

from v2e_rag.generation.requests import ManagerReplyRequest, TaskRequest
from v2e_rag.generation.service import GenerationRejected
from v2e_rag.retrieval.evidence import InsufficientEvidenceError


async def run_generation_eval(container: Any, dataset: Path) -> dict[str, Any]:
    scenarios = yaml.safe_load(dataset.read_text(encoding="utf-8"))["scenarios"]
    rows: list[dict[str, Any]] = []
    for scenario in scenarios:
        started = time.perf_counter()
        row: dict[str, Any] = {
            "id": scenario["id"],
            "kind": scenario["kind"],
            "expect": scenario.get("expect", "approved"),
        }
        try:
            if scenario["kind"] == "task":
                outcome = await container.service.generate_task(TaskRequest(**scenario["request"]))
            else:
                outcome = await container.service.manager_reply(
                    ManagerReplyRequest(**scenario["request"])
                )
            report = outcome.report
            row.update(
                outcome=report.verdict,
                groundedness=report.groundedness,
                violations=len(report.violations),
                pruned=len(report.pruned),
                repairs=report.repair_attempts,
                audit_id=outcome.audit_id,
            )
        except InsufficientEvidenceError as exc:
            row.update(outcome="insufficient_evidence", missing=exc.missing_steps)
        except GenerationRejected as exc:
            row.update(outcome="rejected", audit_id=exc.audit_id)
        row["latency_ms"] = int((time.perf_counter() - started) * 1000)
        expected = row["expect"]
        row["as_expected"] = (
            row["outcome"].startswith("approved")
            if expected == "approved"
            else row["outcome"] == expected
        )
        rows.append(row)

    grounded = [r["groundedness"] for r in rows if "groundedness" in r]
    latencies = sorted(r["latency_ms"] for r in rows)
    summary = {
        "scenarios": len(rows),
        "as_expected_rate": round(sum(r["as_expected"] for r in rows) / max(len(rows), 1), 3),
        "approved": sum(1 for r in rows if str(r["outcome"]).startswith("approved")),
        "abstained_or_insufficient": sum(
            1 for r in rows if r["outcome"] in ("abstained", "insufficient_evidence")
        ),
        "rejected": sum(1 for r in rows if r["outcome"] == "rejected"),
        "mean_groundedness_before_pruning": round(statistics.mean(grounded), 3)
        if grounded
        else None,
        "p50_latency_ms": latencies[len(latencies) // 2] if latencies else None,
        "p95_latency_ms": latencies[int(len(latencies) * 0.95) - 1] if latencies else None,
    }
    return {"summary": summary, "results": rows}

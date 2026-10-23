"""Claim extraction: flatten a structured output into individually checkable statements."""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field

from v2e_rag.llm.schemas import GeneratedTask, ManagerReply

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n+")


@dataclass
class Claim:
    claim_id: str
    field: str
    text: str
    evidence_ids: list[str] = dc_field(default_factory=list)
    kind: str = "item"  # "item" carries its own citations; "sentence" is checked against the output's citations
    core: bool = False


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p and p.strip()]
    return [p for p in parts if re.search(r"[A-Za-z]", p)]


def task_claims(task: GeneratedTask) -> list[Claim]:
    claims: list[Claim] = [
        Claim("summary", "summary", task.summary.text, list(task.summary.evidence_ids), core=True)
    ]
    for name in (
        "context",
        "objectives",
        "steps",
        "tools_and_technologies",
        "collaboration",
        "complications",
    ):
        for index, item in enumerate(getattr(task, name)):
            claims.append(
                Claim(
                    f"{name}[{index}]",
                    name,
                    item.text,
                    list(item.evidence_ids),
                    core=name in ("objectives", "steps"),
                )
            )
    for d_index, deliverable in enumerate(task.deliverables):
        parts = [deliverable.reference, deliverable.name, deliverable.format, deliverable.file_name]
        label = ", ".join(p for p in parts if p)
        if deliverable.due:
            label += f", due {deliverable.due}"
        claims.append(
            Claim(
                f"deliverables[{d_index}]",
                "deliverables",
                f"Deliverable: {label}",
                list(deliverable.evidence_ids),
            )
        )
        for c_index, criterion in enumerate(deliverable.acceptance_criteria):
            claims.append(
                Claim(
                    f"deliverables[{d_index}].acceptance_criteria[{c_index}]",
                    "acceptance_criteria",
                    criterion.text,
                    list(criterion.evidence_ids or deliverable.evidence_ids),
                )
            )
    for index, comp in enumerate(task.competencies):
        weight = f" ({comp.weight_percent} percent)" if comp.weight_percent is not None else ""
        claims.append(
            Claim(
                f"competencies[{index}]",
                "competencies",
                f"{comp.competency}{weight}: {comp.why}",
                list(comp.evidence_ids),
            )
        )
    schedule = task.schedule
    schedule_bits = []
    if schedule.assigned_on:
        schedule_bits.append(f"assigned on {schedule.assigned_on}")
    if schedule.due:
        schedule_bits.append(f"due {schedule.due}")
    if schedule.estimated_effort_hours is not None:
        schedule_bits.append(f"estimated effort {schedule.estimated_effort_hours:g} hours")
    if schedule_bits:
        claims.append(
            Claim(
                "schedule",
                "schedule",
                "Schedule: " + ", ".join(schedule_bits),
                list(schedule.evidence_ids),
            )
        )
    for index, sentence in enumerate(split_sentences(task.message)):
        claims.append(Claim(f"message.sentence[{index}]", "message", sentence, [], kind="sentence"))
    return claims


def reply_claims(reply: ManagerReply) -> list[Claim]:
    claims = [
        Claim(f"claims[{i}]", "claims", c.text, list(c.evidence_ids))
        for i, c in enumerate(reply.claims)
    ]
    for index, sentence in enumerate(split_sentences(reply.reply)):
        claims.append(Claim(f"reply.sentence[{index}]", "reply", sentence, [], kind="sentence"))
    return claims


def cited_ids(task_or_reply: GeneratedTask | ManagerReply) -> list[str]:
    """All evidence ids cited anywhere in an output, in first seen order."""
    ids: list[str] = []
    claims = (
        task_claims(task_or_reply)
        if isinstance(task_or_reply, GeneratedTask)
        else reply_claims(task_or_reply)
    )
    for claim in claims:
        ids.extend(claim.evidence_ids)
    return list(dict.fromkeys(ids))

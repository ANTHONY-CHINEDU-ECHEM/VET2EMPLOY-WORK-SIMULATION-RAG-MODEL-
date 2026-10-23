"""Remove unsupported content after repair, and apply deterministic style fixes."""

from __future__ import annotations

import re
from collections import defaultdict

from v2e_rag.domain.catalog import BrandVoice
from v2e_rag.llm.schemas import GeneratedTask, GroundedItem, ManagerReply
from v2e_rag.verification.checks import apply_brand_replacements, normalise_dashes
from v2e_rag.verification.claims import split_sentences

_INDEX_RE = re.compile(
    r"^(?P<field>[a-z_]+)\[(?P<index>\d+)\](?:\.acceptance_criteria\[(?P<sub>\d+)\])?$"
)
_SENTENCE_RE = re.compile(r"^(?P<field>message|reply)\.sentence\[(?P<index>\d+)\]$")
_LIST_FIELDS = (
    "context",
    "objectives",
    "steps",
    "tools_and_technologies",
    "collaboration",
    "complications",
    "competencies",
)


class PruningError(Exception):
    """Unsupported content could not be removed without breaking the output."""


def _remove_sentences(text: str, indices: set[int]) -> tuple[str, float]:
    sentences = split_sentences(text)
    if not sentences:
        return text, 0.0
    out = text
    for index in sorted(indices):
        if index < len(sentences):
            out = out.replace(sentences[index], "", 1)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out, len(indices) / len(sentences)


def prune_task(
    task: GeneratedTask, claim_ids: set[str], max_message_loss: float = 0.3
) -> tuple[GeneratedTask, list[str]]:
    data = task.model_copy(deep=True)
    removed: list[str] = []
    list_indices: dict[str, set[int]] = defaultdict(set)
    criteria: dict[int, set[int]] = defaultdict(set)
    deliverables: set[int] = set()
    sentences: set[int] = set()

    for claim_id in claim_ids:
        if claim_id == "summary":
            raise PruningError("The task summary is not supported by evidence.")
        if claim_id == "schedule":
            data.schedule.assigned_on = None
            data.schedule.due = None
            data.schedule.estimated_effort_hours = None
            data.schedule.evidence_ids = []
            removed.append(claim_id)
            continue
        sentence = _SENTENCE_RE.match(claim_id)
        if sentence:
            sentences.add(int(sentence.group("index")))
            continue
        match = _INDEX_RE.match(claim_id)
        if not match:
            continue
        field, index, sub = match.group("field"), int(match.group("index")), match.group("sub")
        if field == "deliverables":
            if sub is None:
                deliverables.add(index)
            else:
                criteria[index].add(int(sub))
        elif field in _LIST_FIELDS:
            list_indices[field].add(index)

    for field, indices in list_indices.items():
        items = getattr(data, field)
        setattr(data, field, [item for i, item in enumerate(items) if i not in indices])
        removed += [f"{field}[{i}]" for i in sorted(indices)]
    kept = []
    for d_index, deliverable in enumerate(data.deliverables):
        if d_index in deliverables:
            removed.append(f"deliverables[{d_index}]")
            continue
        if d_index in criteria:
            deliverable.acceptance_criteria = [
                c
                for i, c in enumerate(deliverable.acceptance_criteria)
                if i not in criteria[d_index]
            ]
            removed += [
                f"deliverables[{d_index}].acceptance_criteria[{i}]"
                for i in sorted(criteria[d_index])
            ]
        kept.append(deliverable)
    data.deliverables = kept
    if sentences:
        data.message, loss = _remove_sentences(data.message, sentences)
        if loss > max_message_loss:
            raise PruningError("Too much of the candidate message was unsupported.")
        removed += [f"message.sentence[{i}]" for i in sorted(sentences)]

    if not data.objectives or not data.steps:
        raise PruningError("No supported objectives or steps remain.")
    return data, removed


def prune_reply(
    reply: ManagerReply, claim_ids: set[str], max_loss: float = 0.4
) -> tuple[ManagerReply, list[str]]:
    data = reply.model_copy(deep=True)
    claim_indices = {int(m.group(1)) for c in claim_ids if (m := re.match(r"^claims\[(\d+)\]$", c))}
    sentence_indices = {
        int(m.group(1)) for c in claim_ids if (m := re.match(r"^reply\.sentence\[(\d+)\]$", c))
    }
    data.claims = [c for i, c in enumerate(data.claims) if i not in claim_indices]
    removed = [f"claims[{i}]" for i in sorted(claim_indices)]
    if sentence_indices:
        data.reply, loss = _remove_sentences(data.reply, sentence_indices)
        if loss > max_loss or not data.reply.strip():
            raise PruningError("Too much of the reply was unsupported.")
        removed += [f"reply.sentence[{i}]" for i in sorted(sentence_indices)]
    return data, removed


def _clean(text: str, brand: BrandVoice, forbid_dashes: bool) -> str:
    text = apply_brand_replacements(text, brand)
    return normalise_dashes(text) if forbid_dashes else text


def _clean_item(item: GroundedItem, brand: BrandVoice, forbid_dashes: bool) -> GroundedItem:
    return item.model_copy(update={"text": _clean(item.text, brand, forbid_dashes)})


def finalise_task(task: GeneratedTask, brand: BrandVoice, forbid_dashes: bool) -> GeneratedTask:
    """Deterministic last mile: brand vocabulary and dash rules on every text field."""
    data = task.model_copy(deep=True)
    data.title = _clean(data.title, brand, forbid_dashes)
    data.message = _clean(data.message, brand, forbid_dashes)
    data.summary = _clean_item(data.summary, brand, forbid_dashes)
    for field in (
        "context",
        "objectives",
        "steps",
        "tools_and_technologies",
        "collaboration",
        "complications",
    ):
        setattr(data, field, [_clean_item(i, brand, forbid_dashes) for i in getattr(data, field)])
    for deliverable in data.deliverables:
        deliverable.name = _clean(deliverable.name, brand, forbid_dashes)
        deliverable.acceptance_criteria = [
            _clean_item(c, brand, forbid_dashes) for c in deliverable.acceptance_criteria
        ]
    for comp in data.competencies:
        comp.why = _clean(comp.why, brand, forbid_dashes)
    data.open_questions = [_clean(q, brand, forbid_dashes) for q in data.open_questions]
    return data


def finalise_reply(reply: ManagerReply, brand: BrandVoice, forbid_dashes: bool) -> ManagerReply:
    data = reply.model_copy(deep=True)
    data.reply = _clean(data.reply, brand, forbid_dashes)
    data.claims = [_clean_item(c, brand, forbid_dashes) for c in data.claims]
    return data

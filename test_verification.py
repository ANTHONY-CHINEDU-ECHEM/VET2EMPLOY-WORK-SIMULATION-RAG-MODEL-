"""Deterministic verification checks, pruning and style finalisation."""

from __future__ import annotations

import pytest

from tests.conftest import ROOT
from v2e_rag.domain.catalog import load_catalog
from v2e_rag.domain.models import EvidenceItem, EvidencePack
from v2e_rag.domain.taxonomy import Facet, SourceType
from v2e_rag.generation.pii import redact
from v2e_rag.generation.pruning import PruningError, finalise_task, prune_task
from v2e_rag.llm.schemas import GeneratedTask
from v2e_rag.verification.checks import (
    check_citations,
    check_entities,
    check_file_names,
    check_numbers,
    check_style,
    check_tools,
    normalise_dashes,
    tool_name,
)
from v2e_rag.verification.claims import Claim, split_sentences, task_claims
from v2e_rag.verification.verifier import Verifier

BRAND = load_catalog(str(ROOT / "config")).brand_voice
EVIDENCE = {
    "E1": "Ref: D2 | Deliverable: Data Quality Log | File: V2E_DA_[Surname]_D2_DataQualityLog_v1.xlsx | Due: Tue 17:00",
    "E2": "Tech stack: SQL; Power BI; Excel. Effort: 3.0 hours. Weight: 20%. Sponsor: Priya Raman.",
}


_counter = iter(range(10_000))


def claim(
    text: str, ids: list[str] | None = None, field: str = "steps", kind: str = "item"
) -> Claim:
    return Claim(
        f"{field}[{next(_counter)}]", field, text, ids if ids is not None else ["E1"], kind=kind
    )


def test_citations_must_exist():
    violations = check_citations([claim("x", ["E9"]), claim("y", [])], set(EVIDENCE))
    assert [v.message for v in violations] == [
        "cites evidence that does not exist",
        "has no valid evidence citation",
        "has no valid evidence citation",
    ]


@pytest.mark.parametrize(
    ("text", "ids", "ok"),
    [
        ("Submit by Tue 17:00.", ["E1"], True),
        ("Spend 3 hours on this.", ["E2"], True),
        ("Problem solving carries 20 percent.", ["E2"], True),
        ("Submit by Wed 12:00.", ["E1"], False),
        ("Find at least 40 issues.", ["E1"], False),
    ],
)
def test_numbers_must_come_from_cited_evidence(text, ids, ok):
    assert (not check_numbers([claim(text, ids)], EVIDENCE, "")) is ok


def test_request_numbers_are_allowed():
    assert not check_numbers(
        [claim("This is Activity 7.", ["E1"])], EVIDENCE, '{"activity_number": 7}'
    )


def test_invented_names_are_flagged_but_evidence_names_pass():
    good = claim("Send the log to Priya Raman for review.", ["E2"])
    bad = claim("Send the log to Sarah Jenkins in Finance.", ["E2"])
    assert not check_entities([good], " ".join(EVIDENCE.values()), "", set())
    violations = check_entities([bad], " ".join(EVIDENCE.values()), "", set())
    assert violations and {"Sarah", "Jenkins", "Finance"} <= set(violations[0].details)


def test_sentence_start_words_are_not_treated_as_names():
    text = "Please review the Data Quality Log. Remember to escalate early."
    assert not check_entities([claim(text)], "data quality log escalate", "", set())


def test_tools_must_appear_in_cited_evidence():
    assert tool_name("Power BI for dashboards") == "Power BI"
    assert not check_tools(
        [claim("Power BI for dashboards", ["E2"], field="tools_and_technologies")], EVIDENCE
    )
    flagged = check_tools(
        [claim("Snowflake for warehousing", ["E2"], field="tools_and_technologies")], EVIDENCE
    )
    assert flagged and flagged[0].details == ["Snowflake"]


def test_file_names_must_match_the_brief():
    assert not check_file_names(
        [claim("Save as V2E_DA_[Surname]_D2_DataQualityLog_v1.xlsx")], EVIDENCE["E1"]
    )
    assert check_file_names([claim("Save as V2E_DA_Smith_D9_Log.xlsx")], EVIDENCE["E1"])


def test_style_rules_respect_technical_context():
    assert check_style([claim("Our chatbot will grade your work.")], BRAND, True)
    assert not check_style(
        [claim("Renew the expired TLS certificate on the web server.")], BRAND, True
    )
    assert check_style([claim("Profile the data \u2014 then clean it.")], BRAND, True)
    assert check_style([claim("As an AI, I cannot help.")], BRAND, True)
    assert not check_style(
        [claim("As an AI persona in this simulation, I can confirm.")],
        BRAND,
        True,
        allow_ai_disclosure=True,
    )
    assert normalise_dashes("Mon \u2014 Tue, 9\u201317") == "Mon, Tue, 9 to 17"


def test_split_sentences():
    assert split_sentences(
        "Hi Ada,\n\nPlease start. It is due Tue 17:00.\n\nYour Track Supervisor"
    ) == [
        "Hi Ada,",
        "Please start.",
        "It is due Tue 17:00.",
        "Your Track Supervisor",
    ]


def _task(**overrides) -> GeneratedTask:
    base = {
        "title": "Data profiling",
        "sender_role": "Track Supervisor",
        "priority": "P0",
        "message": "Hi Ada,\n\nPlease profile the data. Then build the log. Also invent things.\n\nYour Track Supervisor",
        "summary": {"text": "Profile the data.", "evidence_ids": ["E1"]},
        "objectives": [
            {"text": "Profile the data.", "evidence_ids": ["E1"]},
            {"text": "Bad.", "evidence_ids": ["E1"]},
        ],
        "steps": [{"text": "Build the log.", "evidence_ids": ["E1"]}],
        "complications": [{"text": "Invented.", "evidence_ids": ["E1"]}],
        "deliverables": [
            {
                "name": "Log",
                "acceptance_criteria": [
                    {"text": "ok", "evidence_ids": ["E1"]},
                    {"text": "bad", "evidence_ids": ["E1"]},
                ],
                "evidence_ids": ["E1"],
            }
        ],
        "schedule": {"due": "Tue 17:00", "evidence_ids": ["E1"]},
    }
    base.update(overrides)
    return GeneratedTask.model_validate(base)


def test_prune_task_removes_only_unsupported_items():
    task = _task()
    pruned, removed = prune_task(
        task,
        {
            "objectives[1]",
            "complications[0]",
            "deliverables[0].acceptance_criteria[1]",
            "message.sentence[3]",
            "schedule",
        },
    )
    assert [o.text for o in pruned.objectives] == ["Profile the data."]
    assert pruned.complications == []
    assert [c.text for c in pruned.deliverables[0].acceptance_criteria] == ["ok"]
    assert "invent" not in pruned.message and pruned.schedule.due is None
    assert set(removed) == {
        "objectives[1]",
        "complications[0]",
        "deliverables[0].acceptance_criteria[1]",
        "message.sentence[3]",
        "schedule",
    }


def test_prune_task_refuses_to_remove_core():
    with pytest.raises(PruningError):
        prune_task(_task(), {"summary"})
    with pytest.raises(PruningError):
        prune_task(_task(), {"steps[0]"})
    with pytest.raises(PruningError):
        prune_task(_task(), {"message.sentence[1]", "message.sentence[2]", "message.sentence[3]"})


def test_finalise_applies_brand_rules():
    task = finalise_task(
        _task(
            title="Your grade \u2014 explained", message="Talk to the chatbot about it, please do."
        ),
        BRAND,
        True,
    )
    assert task.title == "Your score, explained"
    assert "AI Manager" in task.message


def test_task_claims_cover_every_field():
    claims = task_claims(_task())
    ids = {c.claim_id for c in claims}
    assert {
        "summary",
        "objectives[0]",
        "steps[0]",
        "deliverables[0]",
        "deliverables[0].acceptance_criteria[0]",
        "schedule",
    } <= ids
    assert any(c.kind == "sentence" for c in claims)


async def test_verifier_without_judge_uses_deterministic_checks():
    pack = EvidencePack(
        items=[
            EvidenceItem(
                evidence_id=k,
                chunk_id=k,
                facet=Facet.TRACK_DELIVERABLES,
                source_type=SourceType.PROGRAMME_BRIEF,
                authority=1,
                citation="c",
                text=v,
                score=1.0,
                retrieval_purpose="p",
            )
            for k, v in EVIDENCE.items()
        ]
    )
    verifier = Verifier(BRAND, judge=None)
    report = await verifier.verify(
        [claim("Submit by Tue 17:00.", ["E1"]), claim("Submit by Fri 09:00.", ["E1"])],
        pack,
        "",
        set(),
        "test",
    )
    assert report.verdict == "rejected" and report.groundedness == 0.5
    assert "entailment" not in report.checks_run


def test_pii_redaction():
    text, counts = redact(
        "Mail ada@example.com or call 07700 900123, postcode M40 8AB, card 4111 1111 1111 1111."
    )
    assert (
        "ada@example.com" not in text
        and "07700" not in text
        and "M40 8AB" not in text
        and "4111" not in text
    )
    assert counts["EMAIL"] == 1 and counts["POSTCODE"] == 1

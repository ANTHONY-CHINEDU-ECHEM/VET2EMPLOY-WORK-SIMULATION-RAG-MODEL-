"""End to end behaviour of the grounded generation pipeline with scripted models."""

from __future__ import annotations

import json

import pytest

from tests.conftest import (
    ScriptedChat,
    build_test_container,
    evidence_ids_by_facet,
    good_task,
    heuristic_judge,
    task_handler,
)
from v2e_rag.generation.requests import ManagerReplyRequest, TaskRequest, VerifyRequest
from v2e_rag.generation.service import GenerationRejected, VerificationUnavailable
from v2e_rag.llm.openrouter import LLMError
from v2e_rag.llm.prompts import judge_messages
from v2e_rag.retrieval.evidence import InsufficientEvidenceError

DA3 = TaskRequest(
    track="DA",
    activity_number=3,
    job_level="mid_level",
    sector="consulting",
    candidate_first_name="Ada",
)


async def test_grounded_task_is_approved_with_citations(settings, judge_chat):
    chat = ScriptedChat({"task": task_handler()})
    container = await build_test_container(settings, chat, judge_chat)
    outcome = await container.service.generate_task(DA3)

    assert outcome.report.verdict == "approved"
    assert outcome.report.groundedness == 1.0
    assert outcome.report.repair_attempts == 0
    assert "entailment" in outcome.report.checks_run
    task = outcome.output
    assert task.deliverables[0].file_name == "V2E_DA_[Surname]_D2_DataQualityLog_v1.xlsx"
    known = {item.evidence_id for item in outcome.evidence.items}
    assert set(task.summary.evidence_ids) <= known
    # The audit trail records the request, the evidence and the verdict.
    entry = json.loads(settings.audit_log_path.read_text().splitlines()[-1])
    assert entry["status"] == "approved" and entry["evidence"]


async def test_prompt_contains_only_retrieved_evidence_and_rules(settings, judge_chat):
    chat = ScriptedChat({"task": task_handler()})
    container = await build_test_container(settings, chat, judge_chat)
    await container.service.generate_task(DA3)
    purpose, messages, schema = chat.calls[0]
    assert purpose == "task"
    system, user = messages[0]["content"], messages[1]["content"]
    assert "NON NEGOTIABLE GROUNDING RULES" in system
    assert "chatbot" in system  # banned vocabulary is passed to the model
    assert "EVIDENCE" in user and '<evidence id="E1"' in user
    assert schema is not None and "properties" in schema


async def test_hallucinated_tool_is_repaired(settings, judge_chat):
    def hallucinating(messages):
        ids = evidence_ids_by_facet(messages)
        return good_task(
            ids,
            tools_and_technologies=[
                {"text": "Snowflake for warehousing", "evidence_ids": [ids["tech_stack"][0]]}
            ],
        )

    chat = ScriptedChat({"task": hallucinating, "task_repair": task_handler()})
    container = await build_test_container(settings, chat, judge_chat)
    outcome = await container.service.generate_task(DA3)

    assert outcome.report.verdict == "approved"
    assert outcome.report.repair_attempts == 1
    repair_prompts = [c[1][-1]["content"] for c in chat.calls if c[0] == "task_repair"]
    assert len(repair_prompts) == 1 and "Snowflake" in repair_prompts[0]
    assert all("Snowflake" not in t.text for t in outcome.output.tools_and_technologies)


async def test_unrepaired_optional_claim_is_pruned(settings, judge_chat):
    def with_invented_complication(messages):
        ids = evidence_ids_by_facet(messages)
        return good_task(
            ids,
            complications=[
                {
                    "text": "The finance director resigned yesterday and nobody owns the budget.",
                    "evidence_ids": [ids["track_client"][0]],
                }
            ],
        )

    chat = ScriptedChat(
        {"task": with_invented_complication, "task_repair": with_invented_complication}
    )
    container = await build_test_container(settings, chat, judge_chat)
    outcome = await container.service.generate_task(DA3)

    assert outcome.report.verdict == "approved_with_pruning"
    assert "complications[0]" in outcome.report.pruned
    assert outcome.output.complications == []


async def test_unsupported_core_content_is_rejected(settings, judge_chat):
    def off_brief(messages):
        ids = evidence_ids_by_facet(messages)
        return good_task(
            ids,
            summary={
                "text": "Train a deep learning churn model on loyalty data.",
                "evidence_ids": [ids["track_activity"][0]],
            },
        )

    chat = ScriptedChat({"task": off_brief, "task_repair": off_brief})
    container = await build_test_container(settings, chat, judge_chat)
    with pytest.raises(GenerationRejected) as info:
        await container.service.generate_task(DA3)
    assert info.value.report is not None and info.value.report.verdict == "rejected"


async def test_missing_track_evidence_abstains(settings, judge_chat):
    chat = ScriptedChat({"task": task_handler()})
    container = await build_test_container(settings, chat, judge_chat)
    with pytest.raises(InsufficientEvidenceError) as info:
        await container.service.generate_task(TaskRequest(track="SE", activity_number=2))
    assert "activity_definition" in info.value.missing_steps
    assert not chat.calls  # the model is never called without evidence


async def test_verification_unavailable_fails_closed(settings):
    def broken_judge(messages):
        raise LLMError("judge down")

    chat = ScriptedChat({"task": task_handler()})
    container = await build_test_container(
        settings, chat, ScriptedChat({"verification": broken_judge})
    )
    with pytest.raises(VerificationUnavailable):
        await container.service.generate_task(DA3)


async def test_em_dashes_and_banned_terms_are_removed(settings, judge_chat):
    def styled(messages):
        ids = evidence_ids_by_facet(messages)
        task = good_task(ids)
        task["title"] = "Data profiling \u2014 quality audit"
        return task

    chat = ScriptedChat({"task": styled, "task_repair": styled})
    container = await build_test_container(settings, chat, judge_chat)
    outcome = await container.service.generate_task(DA3)
    assert "\u2014" not in outcome.output.title


async def test_manager_reply_redacts_pii_and_cites(settings, judge_chat):
    def reply(messages):
        ids = evidence_ids_by_facet(messages)
        deliv = ids["track_deliverables"][0]
        return {
            "reply": "The Data Quality Log is due Tue 17:00 as an XLSX file.\n\nYour Track Supervisor",
            "intent": "answer",
            "claims": [
                {
                    "text": "The Data Quality Log is due Tue 17:00 as an XLSX file.",
                    "evidence_ids": [deliv],
                }
            ],
            "needs_human_review": False,
            "review_reason": None,
        }

    chat = ScriptedChat({"manager_reply": reply})
    container = await build_test_container(settings, chat, judge_chat)
    outcome = await container.service.manager_reply(
        ManagerReplyRequest(
            track="DA",
            candidate_message="When is the Data Quality Log due? Email me at ada@example.com",
        )
    )
    sent = chat.calls[0][1][1]["content"]
    assert "ada@example.com" not in sent and "[EMAIL REDACTED]" in sent
    assert outcome.report.verdict == "approved"
    assert outcome.diagnostics["pii_redactions"] == {"EMAIL": 1}


async def test_manager_abstains_without_answer_evidence(settings, judge_chat):
    chat = ScriptedChat({"manager_reply": lambda m: {"reply": "x", "intent": "answer"}})
    strict = settings.model_copy(update={"min_semantic_similarity": 0.3})
    container = await build_test_container(strict, chat, judge_chat)
    outcome = await container.service.manager_reply(
        ManagerReplyRequest(track="CY", candidate_message="zzqx unrelated gibberish token")
    )
    assert outcome.report.verdict == "abstained"
    assert outcome.output.needs_human_review is True
    assert not [c for c in chat.calls if c[0] == "manager_reply"]


async def test_external_verification(settings, judge_chat):
    container = await build_test_container(settings, ScriptedChat({}), judge_chat)
    report, _ = await container.service.verify_external(
        VerifyRequest(
            text="The Data Quality Log is due Tue 17:00. It must be delivered in Snowflake by 9 am.",
            chunk_ids=["da_deliverables"],
        )
    )
    assert report.verdict == "rejected"
    assert any(v.check in ("entity", "number", "entailment") for v in report.violations)


def test_heuristic_judge_is_strict():
    messages = judge_messages(
        {"E1": "The log is due Tue 17:00."},
        [
            {"claim_id": "c1", "text": "The log is due Tue 17:00.", "evidence_ids": ["E1"]},
            {"claim_id": "c2", "text": "Build a churn model.", "evidence_ids": ["E1"]},
        ],
    )
    verdicts = {v["claim_id"]: v["verdict"] for v in heuristic_judge(messages)["verdicts"]}
    assert verdicts == {"c1": "SUPPORTED", "c2": "UNSUPPORTED"}


async def test_complication_task_must_contain_a_supported_complication(settings, judge_chat):
    chat = ScriptedChat({"task": task_handler(), "task_repair": task_handler()})
    container = await build_test_container(settings, chat, judge_chat)
    with pytest.raises(GenerationRejected, match="complication"):
        await container.service.generate_task(
            TaskRequest(track="DA", task_type="complication", candidate_first_name="Ada")
        )

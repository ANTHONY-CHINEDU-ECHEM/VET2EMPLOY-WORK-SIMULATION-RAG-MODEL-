"""Shared fixtures: a small synthetic knowledge base, scripted model fakes and a container."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from v2e_rag.config import Settings
from v2e_rag.container import Container, build_container
from v2e_rag.domain.models import Chunk, SourceDocument
from v2e_rag.domain.taxonomy import SOURCE_AUTHORITY, Facet, JobLevel, SourceType
from v2e_rag.embeddings.base import HashingEmbedder
from v2e_rag.llm.openrouter import LLMError, LLMResult
from v2e_rag.store.lexical import tokenize
from v2e_rag.store.memory_store import MemoryStore

ROOT = Path(__file__).resolve().parents[1]

BRIEF_TITLE = "Vet2Employ Week One Work Brief"

CORPUS: list[dict[str, Any]] = [
    {
        "id": "da_activity_3",
        "source_type": SourceType.PROGRAMME_BRIEF,
        "facet": Facet.TRACK_ACTIVITY,
        "track": "DA",
        "section": "7. Track 1: Data Analyst > 7.6 Sequential activities > Activity 3: Data profiling and quality audit",
        "activity": 3,
        "body": (
            "Objective: Profile every dataset and record each issue in the Data Quality Log.\n"
            "For each file, record the number of rows and columns.\n"
            "Check the uniqueness of every intended key.\n"
            "Record each issue in the Data Quality Log with an ID, dataset, field, description, severity and treatment.\n"
            "Produces: D2 Data Quality Log."
        ),
    },
    {
        "id": "da_deliverables",
        "source_type": SourceType.PROGRAMME_BRIEF,
        "facet": Facet.TRACK_DELIVERABLES,
        "track": "DA",
        "section": "7. Track 1: Data Analyst > 7.7 Required deliverables",
        "body": (
            "Ref: D2 | Deliverable: Data Quality Log | Format and file name: XLSX, "
            "V2E_DA_[Surname]_D2_DataQualityLog_v1.xlsx | Due: Tue 17:00 | Acceptance criteria: At least twelve "
            "issues with counts, severity, treatment and impact"
        ),
    },
    {
        "id": "da_client",
        "source_type": SourceType.PROGRAMME_BRIEF,
        "facet": Facet.TRACK_CLIENT,
        "track": "DA",
        "section": "7. Track 1: Data Analyst > 7.2 The client: Kestrel Lane Home and Garden Ltd",
        "body": (
            "Your sponsor (persona): Priya Raman, Head of Commercial Insight.\n"
            "Store codes such as KL017, kl 17 and Store 17 all refer to the same store."
        ),
    },
    {
        "id": "da_schedule",
        "source_type": SourceType.PROGRAMME_BRIEF,
        "facet": Facet.TRACK_SCHEDULE,
        "track": "DA",
        "section": "7. Track 1: Data Analyst > 7.5 Your week at a glance",
        "body": "No.: 3 | Activity: Data profiling and quality audit | When: Mon and Tue | Effort: 3.0 hours",
    },
    {
        "id": "da_supervisor",
        "source_type": SourceType.PROGRAMME_BRIEF,
        "facet": Facet.SUPERVISOR_VOICE,
        "track": "DA",
        "section": "7. Track 1: Data Analyst > 7.1 Welcome note from your Track Supervisor",
        "body": (
            "I will be looking for three things above everything else: that your numbers reconcile, that your "
            "assumptions are written down and that your recommendations are specific."
        ),
    },
    {
        "id": "da_rubric",
        "source_type": SourceType.PROGRAMME_BRIEF,
        "facet": Facet.TRACK_RUBRIC,
        "track": "DA",
        "section": "7. Track 1: Data Analyst > 7.8 Assessment rubric",
        "body": "Criterion: Problem solving | Weight: 20% | Exceeds expectations: Finds planted and unplanted issues",
    },
    {
        "id": "norms",
        "source_type": SourceType.PROGRAMME_BRIEF,
        "facet": Facet.PROGRAMME_RULES,
        "track": None,
        "section": "3. How Your Week Runs > 3.4 Communication norms",
        "body": (
            "If you are blocked for more than two hours, escalate.\n"
            "Daily written update: Tuesday to Friday by 10:00."
        ),
    },
    {
        "id": "market_resp",
        "source_type": SourceType.JOB_MARKET,
        "facet": Facet.RESPONSIBILITIES,
        "role_families": ["data_analyst"],
        "sectors": ["consulting"],
        "job_level": JobLevel.MID_LEVEL,
        "company": "Example Consulting",
        "job_title": "Data Analyst",
        "body": "Day to day responsibilities:\n\u2022 Profile and cleanse client data\n\u2022 Build Power BI dashboards",
    },
    {
        "id": "market_tech",
        "source_type": SourceType.JOB_MARKET,
        "facet": Facet.TECH_STACK,
        "role_families": ["data_analyst"],
        "sectors": ["consulting"],
        "job_level": JobLevel.MID_LEVEL,
        "company": "Example Consulting",
        "job_title": "Data Analyst",
        "body": "Tech stack: SQL; Power BI; Excel; Python",
    },
    {
        "id": "market_collab",
        "source_type": SourceType.JOB_MARKET,
        "facet": Facet.TEAM_COLLABORATION,
        "role_families": ["data_analyst"],
        "sectors": ["consulting"],
        "job_level": JobLevel.MID_LEVEL,
        "company": "Another Firm",
        "job_title": "Insight Analyst",
        "body": "Team collaboration and culture: Work with consultants and client stakeholders in agile squads.",
    },
    {
        "id": "market_ratings",
        "source_type": SourceType.EMPLOYER_REVIEW,
        "facet": Facet.CULTURE_RATINGS,
        "role_families": ["data_analyst"],
        "sectors": ["consulting"],
        "job_level": JobLevel.MID_LEVEL,
        "company": "Example Consulting",
        "job_title": "Data Analyst",
        "body": "Indeed employee ratings for Example Consulting from 120 employee reviews, on a 1 to 5 scale: overall 3.9.",
    },
    {
        "id": "brand_voice",
        "source_type": SourceType.BRAND_GUIDELINE,
        "facet": Facet.BRAND_VOICE,
        "track": None,
        "section": "04 BRAND VOICE & TONE > Language to Avoid",
        "body": "Never call the AI Manager a chatbot. Feedback separates what was strong from what needs work.",
    },
]


def make_chunk(spec: dict[str, Any]) -> Chunk:
    source_type: SourceType = spec["source_type"]
    is_brief = source_type == SourceType.PROGRAMME_BRIEF
    header = (
        BRIEF_TITLE + (f" | {spec['section']}" if spec.get("section") else "")
        if is_brief
        else (
            f"Role: {spec.get('job_title', '')} | Employer: {spec.get('company', '')}"
            if source_type in (SourceType.JOB_MARKET, SourceType.EMPLOYER_REVIEW)
            else "Brand Strategy Portfolio"
        )
    )
    metadata: dict[str, Any] = {
        "document_title": BRIEF_TITLE if is_brief else "Brand Strategy Portfolio"
    }
    if spec.get("activity"):
        metadata["activity_number"] = spec["activity"]
    track = spec.get("track")
    families = spec.get("role_families") or (["data_analyst"] if track == "DA" else [])
    return Chunk(
        chunk_id=spec["id"],
        document_id=f"doc_{source_type.value}",
        source_type=source_type,
        facet=spec["facet"],
        authority=SOURCE_AUTHORITY[source_type],
        header=header,
        body=spec["body"],
        role_families=families,
        sectors=spec.get("sectors", []),
        job_level=spec.get("job_level"),
        track=track,
        company=spec.get("company"),
        company_key=(spec.get("company") or "").lower().replace(" ", "") or None,
        job_title=spec.get("job_title"),
        section_path=spec.get("section"),
        source_file="synthetic.pdf",
        metadata=metadata,
    )


async def load_corpus(
    store: MemoryStore, embedder: HashingEmbedder, specs: list[dict[str, Any]] | None = None
) -> None:
    chunks = [make_chunk(s) for s in (specs or CORPUS)]
    by_doc: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        by_doc.setdefault(chunk.document_id, []).append(chunk)
    for doc_id, doc_chunks in by_doc.items():
        document = SourceDocument(
            document_id=doc_id,
            source_file="synthetic.pdf",
            title=doc_id,
            source_type=doc_chunks[0].source_type,
            content_hash=doc_id,
        )
        vectors = await embedder.embed_documents([c.text for c in doc_chunks])
        await store.replace_document(document, doc_chunks, vectors, [], embedder.model)


# Scripted model fakes -------------------------------------------------------------------

_EVIDENCE_RE = re.compile(r'^<evidence id="(E\d+)" source="(\w+)" facet="(\w+)"', re.MULTILINE)


def evidence_ids_by_facet(messages: list[dict[str, str]]) -> dict[str, list[str]]:
    text = messages[1]["content"]
    found: dict[str, list[str]] = {}
    for eid, _source, facet in _EVIDENCE_RE.findall(text):
        found.setdefault(facet, []).append(eid)
    return found


def good_task(ids: dict[str, list[str]], **overrides: Any) -> dict[str, Any]:
    act = ids["track_activity"][0]
    deliv = ids["track_deliverables"][0]
    client = ids["track_client"][0]
    schedule = ids["track_schedule"][0]
    rubric = ids["track_rubric"][0]
    tech = ids.get("tech_stack", [act])[0]
    task = {
        "title": "Data profiling and quality audit",
        "sender_role": "Track Supervisor",
        "priority": "P0",
        "message": (
            "Hi Ada,\n\nPlease profile every dataset and record each issue in the Data Quality Log. "
            "Check the uniqueness of every intended key. The Data Quality Log is due Tue 17:00.\n\n"
            "Your Track Supervisor"
        ),
        "summary": {
            "text": "Profile every dataset and record each issue in the Data Quality Log.",
            "evidence_ids": [act],
        },
        "context": [
            {
                "text": "Store codes such as KL017, kl 17 and Store 17 all refer to the same store.",
                "evidence_ids": [client],
            }
        ],
        "objectives": [
            {
                "text": "Profile every dataset and record each issue in the Data Quality Log.",
                "evidence_ids": [act],
            }
        ],
        "steps": [
            {
                "text": "For each file, record the number of rows and columns.",
                "evidence_ids": [act],
            },
            {"text": "Check the uniqueness of every intended key.", "evidence_ids": [act]},
        ],
        "deliverables": [
            {
                "reference": "D2",
                "name": "Data Quality Log",
                "format": "XLSX",
                "file_name": "V2E_DA_[Surname]_D2_DataQualityLog_v1.xlsx",
                "due": "Tue 17:00",
                "acceptance_criteria": [
                    {
                        "text": "At least twelve issues with counts, severity, treatment and impact",
                        "evidence_ids": [deliv],
                    }
                ],
                "evidence_ids": [deliv],
            }
        ],
        "tools_and_technologies": [{"text": "Power BI", "evidence_ids": [tech]}],
        "collaboration": [],
        "complications": [],
        "competencies": [
            {
                "competency": "Problem Solving",
                "weight_percent": 20,
                "why": "Finds planted and unplanted issues",
                "evidence_ids": [rubric],
            }
        ],
        "schedule": {
            "assigned_on": "Mon",
            "due": "Tue 17:00",
            "estimated_effort_hours": 3.0,
            "evidence_ids": [schedule, deliv],
        },
        "open_questions": [],
    }
    task.update(overrides)
    return task


_GREETING = re.compile(
    r"^(hi|hello|dear|thanks|thank you|best|kind regards|regards|your )", re.IGNORECASE
)


def heuristic_judge(messages: list[dict[str, str]]) -> dict[str, Any]:
    """Labels a claim SUPPORTED when most of its content words occur in its passages."""
    content = messages[1]["content"]
    passages = dict(re.findall(r'<evidence id="(E\d+)">\n(.*?)\n</evidence>', content, re.S))
    claims_json = re.search(r"<claims>\n(.*?)\n</claims>", content, re.S)
    assert claims_json, "judge prompt must carry claims as a JSON array"
    verdicts = []
    for item in json.loads(claims_json.group(1)):
        claim_id, claim = item["claim_id"], item["claim"]
        ids = [i for i in item["passages"] if i != "ALL"] or list(passages)
        if _GREETING.match(claim.strip()) and len(claim.split()) <= 4:
            verdicts.append(
                {
                    "claim_id": claim_id,
                    "verdict": "NOT_A_CLAIM",
                    "unsupported_details": [],
                    "rationale": "",
                }
            )
            continue
        words = set(tokenize(claim))
        text = " ".join(passages.get(i, "") for i in ids)
        found = set(tokenize(text))
        overlap = len(words & found) / max(len(words), 1)
        verdict = "SUPPORTED" if overlap >= 0.6 else "UNSUPPORTED"
        verdicts.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "unsupported_details": sorted(words - found)[:5] if verdict != "SUPPORTED" else [],
                "rationale": f"overlap {overlap:.2f}",
            }
        )
    return {"verdicts": verdicts}


Handler = Callable[[list[dict[str, str]]], dict[str, Any]]


class ScriptedChat:
    """ChatModel fake. ``handlers`` maps a purpose to a callable or a queue of callables."""

    def __init__(self, handlers: dict[str, Handler | list[Handler]]) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, list[dict[str, str]], dict[str, Any] | None]] = []

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        schema_name: str,
        schema: dict[str, Any] | None,
        model: str | None = None,
        fallbacks: list[str] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> LLMResult:
        self.calls.append((purpose, messages, schema))
        handler = self.handlers.get(purpose)
        if handler is None:
            raise LLMError(f"No scripted handler for {purpose}")
        if isinstance(handler, list):
            current = handler.pop(0) if len(handler) > 1 else handler[0]
        else:
            current = handler
        data = current(messages)
        return LLMResult(
            content=json.dumps(data),
            data=data,
            model=f"fake/{purpose}",
            usage={"prompt_tokens": 100, "completion_tokens": 50},
        )

    async def aclose(self) -> None:
        return None


def task_handler(**overrides: Any) -> Handler:
    return lambda messages: good_task(evidence_ids_by_facet(messages), **overrides)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        vector_backend="memory",
        embedding_provider="hashing",
        embedding_dimensions=256,
        config_dir=ROOT / "config",
        api_keys="test-key",
        admin_api_keys="admin-key",
        min_semantic_similarity=0.0,
        audit_log_path=tmp_path / "audit.jsonl",
        memory_snapshot_path=tmp_path / "snapshot",
        knowledge_base_dir=tmp_path / "kb",
        log_json=False,
        log_level="WARNING",
    )


@pytest.fixture
def judge_chat() -> ScriptedChat:
    return ScriptedChat({"verification": heuristic_judge})


async def build_test_container(
    settings: Settings,
    chat: ScriptedChat | None,
    judge: ScriptedChat | None = None,
    corpus: list[dict[str, Any]] | None = None,
) -> Container:
    store = MemoryStore(audit_path=settings.audit_log_path)
    embedder = HashingEmbedder(settings.embedding_dimensions)
    await load_corpus(store, embedder, corpus)
    return build_container(settings, store=store, embedder=embedder, chat=chat, judge_chat=judge)

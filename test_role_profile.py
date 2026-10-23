"""Deterministic role profiles."""

from __future__ import annotations

from v2e_rag.domain.models import RoleRecord
from v2e_rag.domain.taxonomy import JobLevel
from v2e_rag.insights.role_profile import build_role_profile


def _record(
    i: int, company: str, tools: str, overall: float | None, kind: str | None = None
) -> RoleRecord:
    fields = {"tech_stack": tools, "skills": "SQL; Stakeholder management"}
    if overall is not None:
        fields["rating_overall"] = overall
    if kind:
        fields["record_kind"] = kind
    return RoleRecord(
        record_id=str(i),
        document_id="d",
        source_file="f.xlsx",
        sheet="Master",
        row_number=i,
        company=company,
        company_key=company.lower(),
        job_title="Data Analyst",
        job_level=JobLevel.MID_LEVEL,
        role_families=["data_analyst"],
        fields=fields,
    )


def test_role_profile_counts_tools_and_dedupes_ratings():
    records = [
        _record(1, "Acme", "Power BI; SQL; Excel", 4.0),
        _record(2, "Acme", "power bi; Python (pandas)", 4.0),
        _record(3, "Zed", "Tableau; SQL", 3.0),
        _record(4, "Zed", "", 3.0, kind="company_culture"),
    ]
    profile = build_role_profile(records)
    tools = {t["name"].lower(): t["postings"] for t in profile["top_tools"]}
    assert tools["power bi"] == 2 and tools["sql"] == 2 and tools["python"] == 1
    assert profile["postings"] == 3 and profile["employers"] == 2
    assert profile["culture_ratings"]["overall"] == {
        "mean": 3.5,
        "employers": 2,
        "min": 3.0,
        "max": 4.0,
    }

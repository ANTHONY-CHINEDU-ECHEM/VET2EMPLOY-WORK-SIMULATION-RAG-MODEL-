"""Deterministic role profiles computed directly from structured research records.

No language model is involved, so every figure is exact and reproducible. The platform can
use these profiles to calibrate simulated companies (which tools a mid level data engineer
in consulting really uses, how employees rate culture in that market) with zero risk of
hallucination.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import mean
from typing import Any

from v2e_rag.domain.models import RoleRecord

TOOL_FIELDS = (
    "tech_stack",
    "frameworks",
    "programming_languages",
    "databases",
    "cloud_platforms",
    "development_tools",
)
RATING_FIELDS = {
    "rating_overall": "overall",
    "rating_work_life_balance": "work_life_balance",
    "rating_pay_benefits": "pay_and_benefits",
    "rating_job_security_advancement": "job_security_and_advancement",
    "rating_management": "management",
    "rating_culture": "culture",
}


def _items(value: Any) -> list[str]:
    if not value:
        return []
    parts = re.split(r";|\n|,(?![^()]*\))", str(value))
    out = []
    for part in parts:
        item = re.sub(r"\s*\(.*?\)\s*", " ", part).strip(" .:-")
        if 1 < len(item) <= 40 and len(item.split()) <= 5:
            out.append(item)
    return out


def _top(records: list[RoleRecord], fields: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    display: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        seen: set[str] = set()
        for field_name in fields:
            for item in _items(record.fields.get(field_name)):
                key = item.lower()
                if key in seen:
                    continue
                seen.add(key)
                counts[key] += 1
                display[key][item] += 1
    total = len(records) or 1
    return [
        {"name": display[key].most_common(1)[0][0], "postings": n, "share": round(n / total, 3)}
        for key, n in counts.most_common(limit)
    ]


def build_role_profile(records: list[RoleRecord], top_n: int = 15) -> dict[str, Any]:
    roles = [
        r
        for r in records
        if r.fields.get("record_kind") not in ("company_culture", "employee_review")
    ]
    culture = records
    ratings: dict[str, Any] = {}
    seen_companies: set[tuple[str | None, tuple[Any, ...]]] = set()
    by_metric: dict[str, list[float]] = defaultdict(list)
    for record in culture:
        rating_set = tuple(record.fields.get(k) for k in RATING_FIELDS)
        key = (record.company_key, rating_set)
        if not any(v is not None for v in rating_set) or key in seen_companies:
            continue
        seen_companies.add(key)
        for field_name, label in RATING_FIELDS.items():
            value = record.fields.get(field_name)
            if isinstance(value, int | float):
                by_metric[label].append(float(value))
    for label, values in by_metric.items():
        ratings[label] = {
            "mean": round(mean(values), 2),
            "employers": len(values),
            "min": min(values),
            "max": max(values),
        }

    levels = Counter(r.job_level.value for r in roles if r.job_level)
    employers = Counter(r.company for r in roles if r.company)
    return {
        "postings": len(roles),
        "employers": len(employers),
        "job_levels": dict(levels),
        "top_skills": _top(roles, ("skills",), top_n),
        "top_tools": _top(roles, TOOL_FIELDS, top_n),
        "culture_ratings": ratings,
        "rating_basis": "Indeed employee ratings on a 1 to 5 scale, one entry per distinct employer rating set.",
        "sample_employers": [name for name, _ in employers.most_common(10)],
        "sources": sorted({r.source_file for r in roles}),
    }

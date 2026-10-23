"""Controlled vocabularies used across ingestion, retrieval and generation.

Every value stored in the knowledge base is normalised to one of these enums so that
filters behave predictably regardless of how a source spreadsheet labels a field.
"""

from __future__ import annotations

import re
from enum import StrEnum


class SourceType(StrEnum):
    """Where a piece of evidence comes from. Drives authority and retrieval routing."""

    PROGRAMME_BRIEF = "programme_brief"
    PRODUCT_OVERVIEW = "product_overview"
    BRAND_GUIDELINE = "brand_guideline"
    ENGINEERING_REFERENCE = "engineering_reference"
    JOB_MARKET = "job_market"
    EMPLOYER_REVIEW = "employer_review"
    METHODOLOGY = "methodology"


# Lower number means higher authority. Programme rules always outrank market evidence
# because the simulation must follow the Vet2Employ brief exactly.
SOURCE_AUTHORITY: dict[SourceType, int] = {
    SourceType.PROGRAMME_BRIEF: 1,
    SourceType.BRAND_GUIDELINE: 2,
    SourceType.PRODUCT_OVERVIEW: 2,
    SourceType.JOB_MARKET: 3,
    SourceType.EMPLOYER_REVIEW: 3,
    SourceType.METHODOLOGY: 4,
    SourceType.ENGINEERING_REFERENCE: 5,
}


class Facet(StrEnum):
    """The aspect of work a chunk describes. Used to plan retrieval per task section."""

    # Programme brief facets
    PROGRAMME_RULES = "programme_rules"
    SUPERVISOR_VOICE = "supervisor_voice"
    TRACK_CLIENT = "track_client"
    TRACK_MATERIALS = "track_materials"
    TRACK_SCHEDULE = "track_schedule"
    TRACK_ACTIVITY = "track_activity"
    TRACK_DELIVERABLES = "track_deliverables"
    TRACK_RUBRIC = "track_rubric"
    TRACK_STRETCH = "track_stretch"
    TEMPLATES = "templates"
    # Brand and product facets
    BRAND_VOICE = "brand_voice"
    BRAND_GENERAL = "brand_general"
    PRODUCT = "product"
    ENGINEERING = "engineering"
    # Job market facets
    RESPONSIBILITIES = "responsibilities"
    SKILLS_EXPERIENCE = "skills_experience"
    TECH_STACK = "tech_stack"
    TEAM_COLLABORATION = "team_collaboration"
    WORK_CULTURE = "work_culture"
    CULTURE_RATINGS = "culture_ratings"
    EMPLOYEE_REVIEW = "employee_review"
    EMPLOYEE_PERSPECTIVE = "employee_perspective"
    METHODOLOGY = "methodology"
    GENERAL = "general"


class RoleFamily(StrEnum):
    CYBERSECURITY = "cybersecurity"
    DATA_ANALYST = "data_analyst"
    DATA_ENGINEER = "data_engineer"
    SOFTWARE_DEVELOPER = "software_developer"


ROLE_FAMILY_LABELS: dict[RoleFamily, str] = {
    RoleFamily.CYBERSECURITY: "Cybersecurity",
    RoleFamily.DATA_ANALYST: "Data Analyst",
    RoleFamily.DATA_ENGINEER: "Data Engineer",
    RoleFamily.SOFTWARE_DEVELOPER: "Software Developer",
}


class Sector(StrEnum):
    CONSTRUCTION = "construction"
    CONSULTING = "consulting"
    EDUCATION = "education"
    HEALTHCARE = "healthcare"
    INFORMATION_TECHNOLOGY = "information_technology"
    GENERAL = "general"


SECTOR_LABELS: dict[Sector, str] = {
    Sector.CONSTRUCTION: "Construction",
    Sector.CONSULTING: "Consulting",
    Sector.EDUCATION: "Education",
    Sector.HEALTHCARE: "Healthcare",
    Sector.INFORMATION_TECHNOLOGY: "Information Technology",
    Sector.GENERAL: "General market",
}


class JobLevel(StrEnum):
    BEGINNER = "beginner"
    MID_LEVEL = "mid_level"
    ADVANCED = "advanced"
    EXPERT = "expert"


JOB_LEVEL_LABELS: dict[JobLevel, str] = {
    JobLevel.BEGINNER: "Beginner",
    JobLevel.MID_LEVEL: "Mid Level",
    JobLevel.ADVANCED: "Advanced",
    JobLevel.EXPERT: "Expert",
}

# Ordered from most junior to most senior, used when relaxing level filters.
JOB_LEVEL_ORDER: list[JobLevel] = [
    JobLevel.BEGINNER,
    JobLevel.MID_LEVEL,
    JobLevel.ADVANCED,
    JobLevel.EXPERT,
]


class TrackCode(StrEnum):
    """The four Vet2Employ week one tracks defined in the Programme Work Brief."""

    DA = "DA"
    SE = "SE"
    CY = "CY"
    DE = "DE"


TRACK_ROLE_FAMILY: dict[TrackCode, RoleFamily] = {
    TrackCode.DA: RoleFamily.DATA_ANALYST,
    TrackCode.SE: RoleFamily.SOFTWARE_DEVELOPER,
    TrackCode.CY: RoleFamily.CYBERSECURITY,
    TrackCode.DE: RoleFamily.DATA_ENGINEER,
}

ROLE_FAMILY_TRACK: dict[RoleFamily, TrackCode] = {v: k for k, v in TRACK_ROLE_FAMILY.items()}


class TaskType(StrEnum):
    """Kinds of work item the AI Manager can issue inside the simulation."""

    CORE_ACTIVITY = "core_activity"
    COLLEAGUE_REQUEST = "colleague_request"
    STAKEHOLDER_UPDATE = "stakeholder_update"
    COMPLICATION = "complication"


class Competency(StrEnum):
    """The five Vet2Employ competencies from the Programme Work Brief, section 6.1."""

    TECHNICAL_QUALITY = "Technical Quality"
    PROBLEM_SOLVING = "Problem Solving"
    COMMUNICATION = "Communication"
    DELIVERY = "Delivery"
    PROFESSIONALISM = "Professionalism"


# Normalisers ---------------------------------------------------------------------------

_SPACE_RE = re.compile(r"\s+")


def collapse_whitespace(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip()


def normalise_key(value: str) -> str:
    """Lower case, collapse whitespace and unify dash characters for header matching."""
    value = value.replace("\u2010", "-").replace("\u2011", "-").replace("\u2013", "-")
    value = value.replace("\u2014", "-").replace("\u00a0", " ")
    return collapse_whitespace(value).lower()


def normalise_job_level(raw: object) -> JobLevel | None:
    if raw is None:
        return None
    key = re.sub(r"[^a-z]", "", str(raw).lower())
    mapping = {
        "beginner": JobLevel.BEGINNER,
        "entry": JobLevel.BEGINNER,
        "entrylevel": JobLevel.BEGINNER,
        "junior": JobLevel.BEGINNER,
        "midlevel": JobLevel.MID_LEVEL,
        "mid": JobLevel.MID_LEVEL,
        "intermediate": JobLevel.MID_LEVEL,
        "advanced": JobLevel.ADVANCED,
        "senior": JobLevel.ADVANCED,
        "expert": JobLevel.EXPERT,
        "lead": JobLevel.EXPERT,
        "principal": JobLevel.EXPERT,
    }
    return mapping.get(key)


def company_key(name: str | None) -> str | None:
    """Stable key that links the same employer across workbooks."""
    if not name:
        return None
    key = name.lower()
    key = re.sub(r"\b(ltd|limited|plc|llp|inc|group|uk|the)\b", " ", key)
    key = re.sub(r"[^a-z0-9]+", "", key)
    return key or None

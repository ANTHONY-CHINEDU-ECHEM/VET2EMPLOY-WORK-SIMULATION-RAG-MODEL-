"""Core data models shared by every layer of the service."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from v2e_rag.domain.taxonomy import Facet, JobLevel, SourceType


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Fast, dependency free token estimate.

    Uses the larger of a word based and a character based estimate, which tracks
    common BPE tokenisers within roughly ten percent for English prose and tables.
    Budgets in this service are deliberately conservative so the estimate is safe.
    """
    if not text:
        return 0
    words = len(text.split())
    return max(int(words * 1.33), len(text) // 4) + 1


class SourceDocument(BaseModel):
    """A file (or a logical sheet set) registered in the knowledge base."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    source_file: str
    title: str
    source_type: SourceType
    content_hash: str
    collected_on: date | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Chunk(BaseModel):
    """The atomic unit of retrieval. ``text`` is exactly what is embedded and cited."""

    chunk_id: str
    document_id: str
    source_type: SourceType
    facet: Facet
    authority: int
    header: str
    body: str
    role_families: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    job_level: JobLevel | None = None
    track: str | None = None
    company: str | None = None
    company_key: str | None = None
    job_title: str | None = None
    location: str | None = None
    section_path: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    source_url: str | None = None
    source_file: str
    sheet: str | None = None
    row_number: int | None = None
    collected_on: date | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.header}\n{self.body}" if self.header else self.body

    @property
    def content_hash(self) -> str:
        return sha256_hex(self.text)

    @property
    def token_count(self) -> int:
        return estimate_tokens(self.text)

    def citation_label(self) -> str:
        """Human readable source label shown to candidates, assessors and developers."""
        if self.source_type in (SourceType.JOB_MARKET, SourceType.EMPLOYER_REVIEW):
            parts = [p for p in (self.company, self.job_title) if p]
            label = ", ".join(parts) if parts else self.source_file
            kind = (
                "employer review data"
                if self.source_type == SourceType.EMPLOYER_REVIEW
                else "job posting"
            )
            return f"{label} ({kind}, {self.source_file})"
        title = self.metadata.get("document_title") or self.source_file
        label = str(title)
        if self.section_path:
            label += f", {self.section_path}"
        if self.page_start:
            if self.page_end and self.page_end != self.page_start:
                label += f", pages {self.page_start} to {self.page_end}"
            else:
                label += f", page {self.page_start}"
        return label


class RoleRecord(BaseModel):
    """One structured job role from the labour market workbooks."""

    record_id: str
    document_id: str
    source_file: str
    sheet: str
    row_number: int
    company_id: str | None = None
    company: str | None = None
    company_key: str | None = None
    employer_organisation: str | None = None
    niche: str | None = None
    location: str | None = None
    job_title: str | None = None
    job_level: JobLevel | None = None
    raw_job_level: str | None = None
    role_families: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    collected_on: date | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    source_urls: list[str] = Field(default_factory=list)

    @property
    def source_url(self) -> str | None:
        return self.source_urls[0] if self.source_urls else None


class SearchHit(BaseModel):
    """A retrieval candidate with its channel scores."""

    chunk: Chunk
    semantic_similarity: float | None = None
    lexical_score: float | None = None
    semantic_rank: int | None = None
    lexical_rank: int | None = None
    fused_score: float = 0.0
    final_score: float = 0.0


class EvidenceItem(BaseModel):
    """A chunk admitted into an evidence pack, addressable by a short id such as E3."""

    evidence_id: str
    chunk_id: str
    facet: Facet
    source_type: SourceType
    authority: int
    citation: str
    text: str
    source_url: str | None = None
    score: float
    retrieval_purpose: str


class EvidencePack(BaseModel):
    items: list[EvidenceItem] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def by_id(self) -> dict[str, EvidenceItem]:
        return {item.evidence_id: item for item in self.items}

    def facets(self) -> set[Facet]:
        return {item.facet for item in self.items}

    def total_tokens(self) -> int:
        return sum(estimate_tokens(item.text) for item in self.items)

    def render(self) -> str:
        """Render the pack in the exact format the prompts reference.

        Each passage is fenced in an ``<evidence>`` element. Source text comes from job
        postings and documents the service does not control, so any tag in it that could
        close or forge a fence is neutralised before rendering.
        """
        blocks = []
        for item in self.items:
            citation = item.citation.replace('"', "'")
            blocks.append(
                f'<evidence id="{item.evidence_id}" source="{item.source_type.value}" '
                f'facet="{item.facet.value}" citation="{citation}">\n'
                f"{neutralise_tags(item.text)}\n</evidence>"
            )
        return "\n\n".join(blocks)


FENCE_TAGS = ("evidence", "candidate_message", "conversation", "turn", "current_task", "claims")
_FENCE_RE = re.compile(r"<(/?\s*(?:" + "|".join(FENCE_TAGS) + r")\b)", re.IGNORECASE)


def neutralise_tags(text: str) -> str:
    """Stop untrusted text from opening or closing the fences used in prompts."""
    return _FENCE_RE.sub(r"&lt;\1", text)

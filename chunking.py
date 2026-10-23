"""Chunk construction.

Two strategies are used, each matched to the shape of its source:

* Section aware chunking for documents. Text is grouped by its heading path, tables are
  linearised row by row, and sections are packed into chunks under a token ceiling with a
  small overlap. Every chunk carries a contextual header (document, track and section path)
  so it can be understood and cited on its own.

* Facet chunking for labour market records. Each role record yields one chunk per facet
  (responsibilities, skills, tech stack, team collaboration, culture, ratings) so that a
  query about tooling retrieves tooling evidence rather than a whole job advert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from v2e_rag.domain.catalog import Catalog
from v2e_rag.domain.models import Chunk, RoleRecord, SourceDocument, estimate_tokens, sha256_hex
from v2e_rag.domain.taxonomy import (
    JOB_LEVEL_LABELS,
    ROLE_FAMILY_LABELS,
    SECTOR_LABELS,
    SOURCE_AUTHORITY,
    Facet,
    RoleFamily,
    Sector,
    SourceType,
    TrackCode,
)
from v2e_rag.ingestion.document_parser import Block, linearise_table
from v2e_rag.ingestion.excel_loader import PerspectiveRow, ReferenceSheet

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\u2022])")
_ACTIVITY_RE = re.compile(r"\bActivity\s+(\d{1,2})\s*:", re.IGNORECASE)


def activity_number_from_path(section_path: str | None) -> int | None:
    """Return the activity number of the deepest ``Activity N:`` heading in a path."""
    if not section_path:
        return None
    matches = _ACTIVITY_RE.findall(section_path)
    return int(matches[-1]) if matches else None


def _chunk_id(*parts: object) -> str:
    return sha256_hex("|".join(str(p) for p in parts))[:24]


def pack_parts(parts: list[str], max_tokens: int, overlap_tokens: int) -> list[str]:
    """Greedily pack text parts into windows under ``max_tokens`` with overlap.

    A part larger than the ceiling is split on sentence boundaries first.
    """
    expanded: list[str] = []
    for part in parts:
        if estimate_tokens(part) <= max_tokens:
            expanded.append(part)
            continue
        current = ""
        for sentence in _SENTENCE_RE.split(part):
            candidate = f"{current} {sentence}".strip()
            if current and estimate_tokens(candidate) > max_tokens:
                expanded.append(current)
                current = sentence
            else:
                current = candidate
        if current:
            expanded.append(current)

    windows: list[str] = []
    current_parts: list[str] = []
    for part in expanded:
        candidate = "\n".join([*current_parts, part])
        if current_parts and estimate_tokens(candidate) > max_tokens:
            windows.append("\n".join(current_parts))
            carry: list[str] = []
            for previous in reversed(current_parts):
                if estimate_tokens("\n".join([previous, *carry])) > overlap_tokens:
                    break
                carry.insert(0, previous)
            current_parts = [*carry, part]
        else:
            current_parts.append(part)
    if current_parts:
        windows.append("\n".join(current_parts))
    return windows


# Documents -------------------------------------------------------------------------------


@dataclass
class _Section:
    path: list[str]
    track: TrackCode | None
    parts: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)


class DocumentChunker:
    def __init__(self, catalog: Catalog, max_tokens: int, overlap_tokens: int) -> None:
        self.catalog = catalog
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens

    def sections(self, blocks: list[Block], track_aware: bool) -> list[_Section]:
        sections: list[_Section] = []
        stack: list[tuple[int, str]] = []
        track: TrackCode | None = None
        current = _Section(path=[], track=None)

        def flush() -> None:
            nonlocal current
            if current.parts:
                sections.append(current)
            current = _Section(path=[title for _, title in stack], track=track)

        for block in blocks:
            if block.kind == "heading":
                if block.level < 0:  # page break in deck layout
                    flush()
                    continue
                flush()
                if track_aware and block.level <= 2:
                    matched = self.catalog.track_for_heading(block.text)
                    new_track = matched.code if matched else None
                    if new_track != track:
                        stack.clear()
                    track = new_track
                while stack and stack[-1][0] >= block.level:
                    stack.pop()
                stack.append((block.level, block.text))
                current = _Section(path=[title for _, title in stack], track=track)
                if block.page:
                    current.pages.append(block.page)
                continue
            if block.kind == "table":
                current.parts.extend(linearise_table(block.rows))
            elif block.text:
                current.parts.extend(p for p in block.text.split("\n") if p.strip())
            if block.page:
                current.pages.append(block.page)
        flush()
        return sections

    def chunk(self, document: SourceDocument, blocks: list[Block]) -> list[Chunk]:
        track_aware = document.source_type == SourceType.PROGRAMME_BRIEF
        chunks: list[Chunk] = []
        for index, section in enumerate(self.sections(blocks, track_aware)):
            section_path = " > ".join(section.path) if section.path else None
            facet = self.catalog.manifest.facet_for(document.source_type, section_path)
            track_def = self.catalog.tracks.get(section.track) if section.track else None
            if track_def and facet == Facet.PROGRAMME_RULES:
                # Inside a track, uncategorised sections describe the client engagement.
                facet = Facet.TRACK_CLIENT
            header_parts = [document.title]
            if track_def:
                header_parts.append(f"Track {track_def.code.value}: {track_def.name}")
            if section_path:
                header_parts.append(section_path)
            header = " | ".join(header_parts)
            activity = activity_number_from_path(section_path)
            metadata: dict[str, object] = {"document_title": document.title, "section_index": index}
            if activity is not None:
                metadata["activity_number"] = activity
            windows = pack_parts(section.parts, self.max_tokens, self.overlap_tokens)
            for part_index, body in enumerate(windows):
                if not body.strip():
                    continue
                chunks.append(
                    Chunk(
                        chunk_id=_chunk_id(document.document_id, index, part_index),
                        document_id=document.document_id,
                        source_type=document.source_type,
                        facet=facet,
                        authority=SOURCE_AUTHORITY[document.source_type],
                        header=header,
                        body=body,
                        role_families=[track_def.role_family.value] if track_def else [],
                        track=track_def.code.value if track_def else None,
                        section_path=section_path,
                        page_start=min(section.pages) if section.pages else None,
                        page_end=max(section.pages) if section.pages else None,
                        source_file=document.source_file,
                        collected_on=document.collected_on,
                        metadata=dict(metadata),
                    )
                )
        return chunks


# Labour market records ------------------------------------------------------------------


def _split_items(value: object) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    items = [i.strip(" .") for i in re.split(r";|\n", text) if i.strip(" .")]
    return items or [text]


def _bullets(label: str, value: object) -> str | None:
    items = _split_items(value)
    if not items:
        return None
    if len(items) == 1:
        return f"{label}: {items[0]}."
    return f"{label}:\n" + "\n".join(f"\u2022 {item}" for item in items)


def _family_label(value: str) -> str:
    try:
        return ROLE_FAMILY_LABELS[RoleFamily(value)]
    except ValueError:
        return value


def _sector_label(value: str) -> str:
    try:
        return SECTOR_LABELS[Sector(value)]
    except ValueError:
        return value


class RoleChunker:
    """Builds facet chunks from structured role records."""

    TECH_FIELDS = (
        ("frameworks", "Frameworks and standards"),
        ("tech_stack", "Tech stack"),
        ("programming_languages", "Programming languages"),
        ("databases", "Databases and data platforms"),
        ("cloud_platforms", "Cloud platforms"),
        ("development_tools", "Development tools"),
    )

    def __init__(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens

    @staticmethod
    def header(record: RoleRecord) -> str:
        parts = []
        if record.job_title:
            parts.append(f"Role: {record.job_title}")
        if record.company:
            parts.append(f"Employer: {record.company}")
        if record.employer_organisation and record.employer_organisation != record.company:
            parts.append(f"Organisation: {record.employer_organisation}")
        if record.niche:
            parts.append(f"Niche: {record.niche}")
        if record.location:
            parts.append(f"Location: {record.location}")
        if record.job_level:
            parts.append(f"Level: {JOB_LEVEL_LABELS[record.job_level]}")
        if record.role_families:
            parts.append(
                "Role family: " + ", ".join(_family_label(f) for f in record.role_families)
            )
        if record.sectors:
            parts.append("Market sector: " + ", ".join(_sector_label(s) for s in record.sectors))
        if record.collected_on:
            parts.append(f"Collected: {record.collected_on.isoformat()}")
        return " | ".join(parts)

    def _facet_bodies(self, record: RoleRecord) -> list[tuple[Facet, SourceType, str]]:
        f = record.fields
        bodies: list[tuple[Facet, SourceType, str]] = []
        kind = f.get("record_kind")

        if kind == "employee_review":
            review = f.get("employee_review")
            if review:
                bodies.append(
                    (
                        Facet.EMPLOYEE_REVIEW,
                        SourceType.EMPLOYER_REVIEW,
                        f"Employee review: {review}",
                    )
                )
        if kind != "employee_review":
            resp = _bullets("Day to day responsibilities", f.get("responsibilities"))
            if resp:
                bodies.append((Facet.RESPONSIBILITIES, SourceType.JOB_MARKET, resp))
            skills = [
                p
                for p in (
                    _bullets("Skills required", f.get("skills")),
                    f"Experience required: {f['experience']}" if f.get("experience") else None,
                )
                if p
            ]
            if skills:
                bodies.append((Facet.SKILLS_EXPERIENCE, SourceType.JOB_MARKET, "\n".join(skills)))
            tech = [
                p for p in (_bullets(label, f.get(key)) for key, label in self.TECH_FIELDS) if p
            ]
            if tech:
                bodies.append((Facet.TECH_STACK, SourceType.JOB_MARKET, "\n".join(tech)))
            team = [
                p
                for p in (
                    f"Team structure: {f['team_structure']}" if f.get("team_structure") else None,
                    f"Team collaboration and culture: {f['team_collaboration']}"
                    if f.get("team_collaboration")
                    else None,
                )
                if p
            ]
            if team:
                bodies.append((Facet.TEAM_COLLABORATION, SourceType.JOB_MARKET, "\n".join(team)))
            culture = [
                p
                for p in (
                    f"Employer stated work culture: {f['employer_stated_culture']}"
                    if f.get("employer_stated_culture")
                    else None,
                    f"Work life balance and flexible working evidence: {f['flexible_working']}"
                    if f.get("flexible_working")
                    else None,
                )
                if p
            ]
            if culture:
                bodies.append((Facet.WORK_CULTURE, SourceType.JOB_MARKET, "\n".join(culture)))

        ratings = self._ratings_text(record)
        if ratings:
            bodies.append((Facet.CULTURE_RATINGS, SourceType.EMPLOYER_REVIEW, ratings))
        return bodies

    @staticmethod
    def _ratings_text(record: RoleRecord) -> str | None:
        f = record.fields
        labels = (
            ("rating_overall", "overall"),
            ("rating_work_life_balance", "work life balance"),
            ("rating_pay_benefits", "pay and benefits"),
            ("rating_job_security_advancement", "job security and advancement"),
            ("rating_management", "management"),
            ("rating_culture", "culture"),
        )
        values = [
            f"{label} {f[key]:.1f}" for key, label in labels if isinstance(f.get(key), (int, float))
        ]
        if not values:
            return None
        count = f.get("review_count")
        basis = f" from {count} employee reviews" if isinstance(count, int) else ""
        return (
            f"Indeed employee ratings for {record.company or 'this employer'}{basis}, on a 1 to 5 scale: "
            + "; ".join(values)
            + ". Ratings describe employee sentiment and are calculated as a weighted mean of star ratings."
        )

    def chunk(self, document: SourceDocument, record: RoleRecord) -> list[Chunk]:
        header = self.header(record)
        chunks: list[Chunk] = []
        for facet, source_type, body in self._facet_bodies(record):
            windows = pack_parts(body.split("\n"), self.max_tokens, 0)
            for part_index, window in enumerate(windows):
                chunks.append(
                    Chunk(
                        chunk_id=_chunk_id(
                            document.document_id, record.record_id, facet.value, part_index
                        ),
                        document_id=document.document_id,
                        source_type=source_type,
                        facet=facet,
                        authority=SOURCE_AUTHORITY[source_type],
                        header=header,
                        body=window,
                        role_families=list(record.role_families),
                        sectors=list(record.sectors),
                        job_level=record.job_level,
                        company=record.company,
                        company_key=record.company_key,
                        job_title=record.job_title,
                        location=record.location,
                        source_url=record.source_url,
                        source_file=record.source_file,
                        sheet=record.sheet,
                        row_number=record.row_number,
                        collected_on=record.collected_on,
                        metadata={
                            "record_id": record.record_id,
                            "company_id": record.company_id,
                            "niche": record.niche,
                            "all_source_urls": record.source_urls,
                        },
                    )
                )
        return chunks

    def chunk_perspective(
        self, document: SourceDocument, row: PerspectiveRow, base_meta: dict[str, list[str]]
    ) -> Chunk:
        f = row.fields
        pieces = [f"Employee perspective data for {row.company or 'the employer'}"]
        if f.get("category"):
            pieces.append(f"Category: {f['category']}")
        if f.get("year"):
            pieces.append(f"Year: {f['year']}")
        header = " | ".join(pieces)
        value = f"{f.get('value', '')} {f.get('unit', '') or ''}".strip()
        body_parts = [f"{f.get('data_point', 'Data point')}: {value}."]
        if f.get("metric_description"):
            body_parts.append(f"Metric: {f['metric_description']}.")
        if f.get("target_population"):
            body_parts.append(f"Population: {f['target_population']}.")
        if f.get("sample_size"):
            body_parts.append(f"Sample size: {f['sample_size']}.")
        return Chunk(
            chunk_id=_chunk_id(document.document_id, row.sheet, row.row_number),
            document_id=document.document_id,
            source_type=SourceType.EMPLOYER_REVIEW,
            facet=Facet.EMPLOYEE_PERSPECTIVE,
            authority=SOURCE_AUTHORITY[SourceType.EMPLOYER_REVIEW],
            header=header,
            body=" ".join(body_parts),
            role_families=base_meta.get("role_families", []),
            sectors=base_meta.get("sectors", []),
            company=row.company,
            source_url=row.source_url,
            source_file=document.source_file,
            sheet=row.sheet,
            row_number=row.row_number,
            collected_on=document.collected_on,
        )


def chunk_reference_sheet(
    document: SourceDocument, sheet: ReferenceSheet, max_tokens: int
) -> list[Chunk]:
    title = (
        "Indeed rating methodology"
        if sheet.kind.value == "methodology"
        else "Research data dictionary"
    )
    header = f"{title} | Labour market research workbooks"
    return [
        Chunk(
            chunk_id=_chunk_id(document.document_id, index),
            document_id=document.document_id,
            source_type=SourceType.METHODOLOGY,
            facet=Facet.METHODOLOGY,
            authority=SOURCE_AUTHORITY[SourceType.METHODOLOGY],
            header=header,
            body=window,
            source_file=document.source_file,
            sheet=sheet.sheet,
            metadata={"document_title": title},
        )
        for index, window in enumerate(pack_parts(sheet.rows, max_tokens, 0))
    ]

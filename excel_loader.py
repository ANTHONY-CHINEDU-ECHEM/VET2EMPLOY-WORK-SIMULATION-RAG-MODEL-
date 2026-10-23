"""Parse the labour market research workbooks into structured records.

The research workbooks share a common design (a Master dataset plus topic sheets, a rating
methodology and a data dictionary) but column names, sheet names and level labels vary
between files. This loader:

1. Maps every header to a canonical field through an alias table.
2. Classifies each sheet by name and by the canonical fields it contains.
3. Treats the Master sheet as the primary source of role records.
4. Merges topic sheets into Master records by (Company ID, Job Title) and keeps any field
   the Master does not already hold, so no evidence is lost.
5. Keeps unmatched culture rows as company level culture records.
6. Extracts methodology and dictionary sheets as reference text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from v2e_rag.domain.models import RoleRecord, sha256_hex
from v2e_rag.domain.taxonomy import (
    collapse_whitespace,
    company_key,
    normalise_job_level,
    normalise_key,
)
from v2e_rag.ingestion.filename_metadata import FileMetadata


class SheetKind(StrEnum):
    MASTER = "master"
    TOPIC = "topic"
    RATINGS = "ratings"
    REVIEWS = "reviews"
    PERSPECTIVES = "perspectives"
    METHODOLOGY = "methodology"
    DICTIONARY = "dictionary"
    UNKNOWN = "unknown"


HEADER_ALIASES: dict[str, str] = {
    "company id": "company_id",
    "company": "company",
    "company name": "company",
    "employer organisation": "employer_organisation",
    "employer organization": "employer_organisation",
    "niche": "niche",
    "location": "location",
    "location/region": "location",
    "region": "location",
    "job title": "job_title",
    "job level": "job_level",
    "skills required": "skills",
    "experience required": "experience",
    "role / day-to-day responsibilities": "responsibilities",
    "role / day to day responsibilities": "responsibilities",
    "day-to-day responsibilities": "responsibilities",
    "responsibilities": "responsibilities",
    "frameworks": "frameworks",
    "tech stack": "tech_stack",
    "programming languages": "programming_languages",
    "databases / data platforms": "databases",
    "cloud platforms": "cloud_platforms",
    "development tools": "development_tools",
    "team structure": "team_structure",
    "team collaboration": "team_collaboration",
    "team collaboration & culture": "team_collaboration",
    "team collaboration and culture": "team_collaboration",
    "employer-stated work culture": "employer_stated_culture",
    "employer stated work culture": "employer_stated_culture",
    "work-life balance / flexible working evidence": "flexible_working",
    "no of reviews": "review_count",
    "no. of reviews": "review_count",
    "number of reviews": "review_count",
    "work-life balance rating": "rating_work_life_balance",
    "pay and benefits rating": "rating_pay_benefits",
    "job security and advancement rating": "rating_job_security_advancement",
    "management rating": "rating_management",
    "culture rating": "rating_culture",
    "overall company rating": "rating_overall",
    "employee review (full text)": "employee_review",
    "source type": "source_type_label",
    "source url": "source_url",
    "source urls": "source_url",
}

PERSPECTIVE_ALIASES: dict[str, str] = {
    "company": "company",
    "variable name": "variable_name",
    "category": "category",
    "year": "year",
    "data point": "data_point",
    "value": "value",
    "unit": "unit",
    "metric description": "metric_description",
    "target population": "target_population",
    "sample size": "sample_size",
    "source url": "source_url",
}

RATING_FIELDS = (
    "rating_overall",
    "rating_work_life_balance",
    "rating_pay_benefits",
    "rating_job_security_advancement",
    "rating_management",
    "rating_culture",
)
ROLE_CONTENT_FIELDS = {
    "responsibilities",
    "skills",
    "experience",
    "tech_stack",
    "frameworks",
    "team_structure",
    "team_collaboration",
    "employer_stated_culture",
    "programming_languages",
}
IDENTITY_FIELDS = {
    "company_id",
    "company",
    "niche",
    "location",
    "job_title",
    "job_level",
    "source_url",
}

_EMPTY_VALUES = {"", "n/a", "na", "none", "null", "-", "nan", "#n/a"}
_URL_RE = re.compile(r"https?://[^\s;,]+", re.IGNORECASE)


@dataclass
class PerspectiveRow:
    company: str | None
    fields: dict[str, Any]
    row_number: int
    sheet: str
    source_url: str | None


@dataclass
class ReferenceSheet:
    kind: SheetKind
    sheet: str
    rows: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.rows)


@dataclass
class WorkbookParseResult:
    records: list[RoleRecord] = field(default_factory=list)
    perspectives: list[PerspectiveRow] = field(default_factory=list)
    references: list[ReferenceSheet] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sheet_kinds: dict[str, str] = field(default_factory=dict)


def canonical_header(raw: object, aliases: dict[str, str] = HEADER_ALIASES) -> str | None:
    if raw is None:
        return None
    key = normalise_key(str(raw)).replace("_", " ")
    if key in aliases:
        return aliases[key]
    compact = key.replace(" ", "")
    for alias, canonical in aliases.items():
        if alias.replace(" ", "") == compact:
            return canonical
    return None


def clean_value(value: object) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    if isinstance(value, (int, float)):
        return value
    text = collapse_whitespace(str(value))
    return None if text.lower() in _EMPTY_VALUES else text


def parse_rating(value: object) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return round(number, 2) if 1.0 <= number <= 5.0 else None


def parse_count(value: object) -> int | None:
    try:
        number = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def extract_urls(value: object) -> list[str]:
    if value is None:
        return []
    return list(dict.fromkeys(m.group(0).rstrip(").") for m in _URL_RE.finditer(str(value))))


def classify_sheet(sheet_name: str, canonical_fields: set[str]) -> SheetKind:
    name = normalise_key(sheet_name).replace(" ", "")
    if "methodology" in name:
        return SheetKind.METHODOLOGY
    if "dictionary" in name:
        return SheetKind.DICTIONARY
    if "perspective" in name:
        return SheetKind.PERSPECTIVES
    if "master" in name:
        return SheetKind.MASTER
    if "employee_review" in canonical_fields:
        return SheetKind.REVIEWS
    has_ratings = bool(canonical_fields & set(RATING_FIELDS))
    has_content = bool(canonical_fields & ROLE_CONTENT_FIELDS)
    if has_ratings and not has_content:
        return SheetKind.RATINGS
    if "job_title" in canonical_fields:
        return SheetKind.TOPIC
    return SheetKind.UNKNOWN


def _match_key(company_id: object, job_title: object) -> tuple[str, str]:
    cid = re.sub(r"[^a-z0-9]", "", str(company_id or "").lower())
    title = re.sub(r"[^a-z0-9]", "", str(job_title or "").lower())
    return cid, title


def _record_keys(company_id: object, company: object, job_title: object) -> list[tuple[str, str]]:
    """Match keys in priority order: the workbook ID first, then the employer name.

    Research sheets occasionally carry a mistyped Company ID on a topic sheet (for example
    NUF-0022 against NUF-002 in the Master). Falling back to the employer name plus job title
    reattaches those rows to the right record instead of creating a duplicate.
    """
    keys = [_match_key(company_id, job_title)]
    name_key = company_key(str(company)) if company else None
    if name_key:
        keys.append(("name:" + name_key, _match_key(None, job_title)[1]))
    return keys


def _read_rows(worksheet: Any) -> list[tuple[int, tuple[Any, ...]]]:
    rows = []
    for index, row in enumerate(worksheet.iter_rows(values_only=True), start=1):
        if row is None or not any(cell is not None and str(cell).strip() for cell in row):
            continue
        rows.append((index, row))
    return rows


def _row_to_fields(header: list[str | None], row: tuple[Any, ...]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for canonical, cell in zip(header, row, strict=False):
        if not canonical:
            continue
        value = clean_value(cell)
        if value is None:
            continue
        # The first non empty value wins when two columns map to the same field.
        fields.setdefault(canonical, value)
    return fields


def _normalise_fields(fields: dict[str, Any]) -> dict[str, Any]:
    out = dict(fields)
    for rating in RATING_FIELDS:
        if rating in out:
            parsed = parse_rating(out[rating])
            if parsed is None:
                out.pop(rating)
            else:
                out[rating] = parsed
    if "review_count" in out:
        count = parse_count(out["review_count"])
        if count is None:
            out.pop("review_count")
        else:
            out["review_count"] = count
    return out


class WorkbookLoader:
    """Turns one research workbook into role records, perspectives and reference text."""

    def __init__(self, path: Path, document_id: str, file_meta: FileMetadata) -> None:
        self.path = path
        self.document_id = document_id
        self.meta = file_meta

    def load(self) -> WorkbookParseResult:
        result = WorkbookParseResult()
        workbook = load_workbook(self.path, read_only=True, data_only=True)
        try:
            sheets = [(ws.title, _read_rows(ws)) for ws in workbook.worksheets]
        finally:
            workbook.close()

        classified: list[
            tuple[str, SheetKind, list[str | None], list[tuple[int, tuple[Any, ...]]]]
        ] = []
        for title, rows in sheets:
            if not rows:
                continue
            header_cells = rows[0][1]
            header = [canonical_header(h) for h in header_cells]
            kind = classify_sheet(title, {h for h in header if h})
            result.sheet_kinds[title] = kind.value
            classified.append((title, kind, header, rows))

        masters = [c for c in classified if c[1] == SheetKind.MASTER]
        if not masters:
            result.warnings.append(f"{self.meta.clean_name}: no Master sheet found")
        index: dict[tuple[str, str], RoleRecord] = {}

        for title, _, header, rows in masters:
            for row_number, row in rows[1:]:
                fields = _normalise_fields(_row_to_fields(header, row))
                if not fields.get("job_title") and not fields.get("company"):
                    continue
                record = self._make_record(title, row_number, fields)
                result.records.append(record)
                for key in _record_keys(record.company_id, record.company, record.job_title):
                    index.setdefault(key, record)

        for title, kind, header, rows in classified:
            if kind in (SheetKind.TOPIC, SheetKind.RATINGS, SheetKind.REVIEWS):
                self._merge_sheet(result, index, title, kind, header, rows)
            elif kind == SheetKind.PERSPECTIVES:
                result.perspectives.extend(self._perspectives(title, rows))
            elif kind in (SheetKind.METHODOLOGY, SheetKind.DICTIONARY):
                result.references.append(self._reference(title, kind, rows))
            elif kind == SheetKind.UNKNOWN:
                result.warnings.append(
                    f"{self.meta.clean_name}: sheet '{title}' was not recognised and was skipped"
                )

        for record in result.records:
            if record.job_level is None and record.raw_job_level:
                result.warnings.append(
                    f"{self.meta.clean_name}: unrecognised job level '{record.raw_job_level}' "
                    f"for {record.company} / {record.job_title}"
                )
        return result

    # Internal helpers ------------------------------------------------------------------

    def _make_record(self, sheet: str, row_number: int, fields: dict[str, Any]) -> RoleRecord:
        company = fields.get("company")
        raw_level = fields.get("job_level")
        urls = extract_urls(fields.get("source_url"))
        identity = f"{self.document_id}|{sheet}|{row_number}|{fields.get('company_id')}|{fields.get('job_title')}"
        content = {k: v for k, v in fields.items() if k not in IDENTITY_FIELDS}
        return RoleRecord(
            record_id=sha256_hex(identity)[:20],
            document_id=self.document_id,
            source_file=self.meta.clean_name,
            sheet=sheet,
            row_number=row_number,
            company_id=str(fields["company_id"]) if fields.get("company_id") else None,
            company=str(company) if company else None,
            company_key=company_key(str(company)) if company else None,
            employer_organisation=fields.get("employer_organisation"),
            niche=fields.get("niche"),
            location=fields.get("location"),
            job_title=fields.get("job_title"),
            job_level=normalise_job_level(raw_level),
            raw_job_level=str(raw_level) if raw_level is not None else None,
            role_families=list(self.meta.role_families),
            sectors=list(self.meta.sectors),
            collected_on=self.meta.collected_on,
            fields=content,
            source_urls=urls,
        )

    def _merge_sheet(
        self,
        result: WorkbookParseResult,
        index: dict[tuple[str, str], RoleRecord],
        sheet: str,
        kind: SheetKind,
        header: list[str | None],
        rows: list[tuple[int, tuple[Any, ...]]],
    ) -> None:
        for row_number, row in rows[1:]:
            fields = _normalise_fields(_row_to_fields(header, row))
            if not fields:
                continue
            keys = _record_keys(
                fields.get("company_id"), fields.get("company"), fields.get("job_title")
            )
            key = keys[0]
            target = next((index[k] for k in keys if k in index), None)
            if kind == SheetKind.REVIEWS:
                # Each review row is kept as its own record so the full text stays intact.
                review = self._make_record(sheet, row_number, fields)
                review.fields["record_kind"] = "employee_review"
                result.records.append(review)
                continue
            if target is not None:
                for name, value in fields.items():
                    if name in IDENTITY_FIELDS:
                        if name == "source_url":
                            for url in extract_urls(value):
                                if url not in target.source_urls:
                                    target.source_urls.append(url)
                        continue
                    target.fields.setdefault(name, value)
                continue
            if kind == SheetKind.RATINGS:
                if self._duplicate_ratings(result.records, fields):
                    continue
                record = self._make_record(sheet, row_number, fields)
                record.fields["record_kind"] = "company_culture"
                result.records.append(record)
                index.setdefault(key, record)
            elif fields.get("job_title"):
                record = self._make_record(sheet, row_number, fields)
                record.fields["record_kind"] = "topic_only"
                result.records.append(record)
                index.setdefault(key, record)
                result.warnings.append(
                    f"{self.meta.clean_name}: '{sheet}' row {row_number} has no Master match; kept as its own record"
                )

    @staticmethod
    def _duplicate_ratings(records: list[RoleRecord], fields: dict[str, Any]) -> bool:
        key = company_key(str(fields.get("company") or ""))
        ratings = tuple(fields.get(r) for r in RATING_FIELDS)
        if not any(ratings):
            return True
        return any(
            rec.company_key == key and tuple(rec.fields.get(r) for r in RATING_FIELDS) == ratings
            for rec in records
        )

    def _perspectives(
        self, sheet: str, rows: list[tuple[int, tuple[Any, ...]]]
    ) -> list[PerspectiveRow]:
        header = [canonical_header(h, PERSPECTIVE_ALIASES) for h in rows[0][1]]
        out = []
        for row_number, row in rows[1:]:
            fields = _row_to_fields(header, row)
            if not fields.get("data_point") and not fields.get("value"):
                continue
            urls = extract_urls(fields.get("source_url"))
            out.append(
                PerspectiveRow(
                    company=fields.get("company"),
                    fields=fields,
                    row_number=row_number,
                    sheet=sheet,
                    source_url=urls[0] if urls else None,
                )
            )
        return out

    @staticmethod
    def _reference(
        sheet: str, kind: SheetKind, rows: list[tuple[int, tuple[Any, ...]]]
    ) -> ReferenceSheet:
        lines: list[str] = []
        header: list[str] | None = None
        for _, row in rows:
            cells = [collapse_whitespace(str(c)) for c in row if c is not None and str(c).strip()]
            if not cells:
                continue
            if kind == SheetKind.DICTIONARY and header is None:
                header = cells
                continue
            if header and len(cells) == len(header):
                lines.append(" | ".join(f"{h}: {v}" for h, v in zip(header, cells, strict=False)))
            else:
                lines.append(" | ".join(cells))
        return ReferenceSheet(kind=kind, sheet=sheet, rows=lines)

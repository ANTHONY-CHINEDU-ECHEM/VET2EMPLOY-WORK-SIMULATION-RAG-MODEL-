"""Infer role family, sector and collection date from workbook file names.

The research team names workbooks such as ``Cybersecurity_Analyst_Construction_Oct_22nd_2026``
or ``Healthcare_Data_Analytics_2026-09-29``. This module turns those names into normalised
metadata. A manifest override always wins over inference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from v2e_rag.domain.taxonomy import RoleFamily, Sector

UPLOAD_PREFIX_RE = re.compile(r"^[0-9a-f]{8}-\d+_", re.IGNORECASE)

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_ROLE_PATTERNS: list[tuple[re.Pattern[str], RoleFamily]] = [
    (re.compile(r"cyber\s*security|cybersecurity|infosec"), RoleFamily.CYBERSECURITY),
    (re.compile(r"data\s*engineer"), RoleFamily.DATA_ENGINEER),
    (re.compile(r"data\s*analy"), RoleFamily.DATA_ANALYST),
    (re.compile(r"software\s*(developer|engineer)|developer"), RoleFamily.SOFTWARE_DEVELOPER),
]

_SECTOR_PATTERNS: list[tuple[re.Pattern[str], Sector]] = [
    (re.compile(r"construction"), Sector.CONSTRUCTION),
    (re.compile(r"consulting|consultancy"), Sector.CONSULTING),
    (re.compile(r"education|educational"), Sector.EDUCATION),
    (re.compile(r"healthcare|health\s*care|\bnhs\b"), Sector.HEALTHCARE),
    (re.compile(r"^it\b|\binformation\s*technology\b"), Sector.INFORMATION_TECHNOLOGY),
]


@dataclass(frozen=True)
class FileMetadata:
    clean_name: str
    stem_words: str
    role_families: tuple[str, ...]
    sectors: tuple[str, ...]
    collected_on: date | None


def strip_upload_prefix(file_name: str) -> str:
    return UPLOAD_PREFIX_RE.sub("", file_name)


def _words(stem: str) -> str:
    return re.sub(r"[_\s]+", " ", stem).strip().lower()


def parse_collection_date(stem: str) -> date | None:
    iso = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", stem)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    tokens = [t for t in re.split(r"[_\s\-]+", stem.lower()) if t and t != "of"]
    year = month = day = None
    for index, token in enumerate(tokens):
        if re.fullmatch(r"20\d{2}", token):
            year = int(token)
        elif token in _MONTHS and month is None:
            month = _MONTHS[token]
            for neighbour in (index - 1, index + 1):
                if 0 <= neighbour < len(tokens):
                    match = re.fullmatch(r"(\d{1,2})(st|nd|rd|th)?", tokens[neighbour])
                    if match:
                        day = int(match.group(1))
                        break
    if year and month and day:
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def infer_file_metadata(file_name: str) -> FileMetadata:
    clean = strip_upload_prefix(file_name)
    stem = clean.rsplit(".", 1)[0]
    words = _words(stem)
    families = tuple(
        dict.fromkeys(rf.value for pattern, rf in _ROLE_PATTERNS if pattern.search(words))
    )
    sectors = tuple(
        dict.fromkeys(s.value for pattern, s in _SECTOR_PATTERNS if pattern.search(words))
    ) or (Sector.GENERAL.value,)
    return FileMetadata(
        clean_name=clean,
        stem_words=words,
        role_families=families,
        sectors=sectors,
        collected_on=parse_collection_date(stem),
    )

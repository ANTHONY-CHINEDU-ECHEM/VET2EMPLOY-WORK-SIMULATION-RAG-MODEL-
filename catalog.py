"""Typed loaders for the YAML files in ``config/``."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from v2e_rag.domain.taxonomy import Facet, RoleFamily, SourceType, TrackCode


@dataclass(frozen=True)
class TrackDefinition:
    code: TrackCode
    name: str
    role_family: RoleFamily
    file_code: str
    brief_heading_patterns: tuple[re.Pattern[str], ...]
    manager_title: str
    retrieval_keywords: tuple[str, ...]
    activity_count: int | None = None

    def matches_heading(self, heading: str) -> bool:
        return any(p.search(heading) for p in self.brief_heading_patterns)


@dataclass(frozen=True)
class DocumentRule:
    match: str
    title: str
    source_type: SourceType
    layout: str

    def applies_to(self, file_name: str) -> bool:
        return fnmatch.fnmatch(file_name.lower(), self.match.lower())


@dataclass(frozen=True)
class WorkbookOverride:
    match: str
    role_families: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    collected_on: date | None = None

    def applies_to(self, file_name: str) -> bool:
        return fnmatch.fnmatch(file_name.lower(), self.match.lower())


@dataclass(frozen=True)
class FacetRule:
    pattern: re.Pattern[str]
    facet: Facet


@dataclass(frozen=True)
class KnowledgeManifest:
    documents: tuple[DocumentRule, ...]
    workbook_match: str
    workbook_overrides: tuple[WorkbookOverride, ...]
    facet_rules: dict[SourceType, tuple[FacetRule, ...]]

    def document_rule_for(self, file_name: str) -> DocumentRule | None:
        return next((rule for rule in self.documents if rule.applies_to(file_name)), None)

    def is_workbook(self, file_name: str) -> bool:
        return fnmatch.fnmatch(file_name.lower(), self.workbook_match.lower())

    def override_for(self, file_name: str) -> WorkbookOverride | None:
        return next((o for o in self.workbook_overrides if o.applies_to(file_name)), None)

    def facet_for(self, source_type: SourceType, section_path: str | None) -> Facet:
        path = section_path or ""
        for rule in self.facet_rules.get(source_type, ()):
            if rule.pattern.search(path):
                return rule.facet
        return Facet.GENERAL


@dataclass(frozen=True)
class BannedTerm:
    pattern: re.Pattern[str]
    replacement: str
    reason: str
    allow_if_context: tuple[re.Pattern[str], ...] = ()
    raw: str = ""
    display: str = ""


@dataclass(frozen=True)
class BrandVoice:
    banned_terms: tuple[BannedTerm, ...]
    character_breaks: tuple[str, ...]

    @property
    def display_terms(self) -> list[str]:
        return [t.display for t in self.banned_terms]


@dataclass(frozen=True)
class Catalog:
    tracks: dict[TrackCode, TrackDefinition]
    manifest: KnowledgeManifest
    brand_voice: BrandVoice
    extra: dict[str, Any] = field(default_factory=dict)

    def track(self, code: TrackCode | str) -> TrackDefinition:
        return self.tracks[TrackCode(str(code).upper())]

    def track_for_heading(self, heading: str) -> TrackDefinition | None:
        return next((t for t in self.tracks.values() if t.matches_heading(heading)), None)


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return data


def _load_tracks(path: Path) -> dict[TrackCode, TrackDefinition]:
    data = _read_yaml(path).get("tracks", {})
    tracks: dict[TrackCode, TrackDefinition] = {}
    for code, spec in data.items():
        track_code = TrackCode(code)
        tracks[track_code] = TrackDefinition(
            code=track_code,
            name=spec["name"],
            role_family=RoleFamily(spec["role_family"]),
            file_code=spec.get("file_code", code),
            brief_heading_patterns=tuple(
                re.compile(p, re.IGNORECASE) for p in spec.get("brief_heading_patterns", [])
            ),
            manager_title=spec.get("manager_title", "Track Supervisor"),
            retrieval_keywords=tuple(spec.get("retrieval_keywords", [])),
            activity_count=int(spec["activity_count"]) if spec.get("activity_count") else None,
        )
    families = [t.role_family for t in tracks.values()]
    if len(families) != len(set(families)):
        raise ValueError(f"{path}: each track must map to a different role family")
    return tracks


def _load_manifest(path: Path) -> KnowledgeManifest:
    data = _read_yaml(path)
    documents = tuple(
        DocumentRule(
            match=d["match"],
            title=d["title"],
            source_type=SourceType(d["source_type"]),
            layout=d.get("layout", "report"),
        )
        for d in data.get("documents", [])
    )
    wb = data.get("workbooks", {}) or {}
    overrides = tuple(
        WorkbookOverride(
            match=o["match"],
            role_families=tuple(o.get("role_families", [])),
            sectors=tuple(o.get("sectors", [])),
            collected_on=o.get("collected_on"),
        )
        for o in (wb.get("overrides") or [])
    )
    facet_rules: dict[SourceType, tuple[FacetRule, ...]] = {}
    for source_type, rules in (data.get("facet_rules") or {}).items():
        facet_rules[SourceType(source_type)] = tuple(
            FacetRule(re.compile(r["pattern"], re.IGNORECASE), Facet(r["facet"])) for r in rules
        )
    return KnowledgeManifest(
        documents=documents,
        workbook_match=wb.get("match", "*.xlsx"),
        workbook_overrides=overrides,
        facet_rules=facet_rules,
    )


def _load_brand_voice(path: Path, brand_name: str) -> BrandVoice:
    data = _read_yaml(path)
    terms = []
    for spec in data.get("banned_terms", []):
        flags = 0 if spec.get("case_sensitive") else re.IGNORECASE
        terms.append(
            BannedTerm(
                pattern=re.compile(rf"\b(?:{spec['term']})\b", flags),
                replacement=str(spec.get("replacement", "")).replace("{brand_name}", brand_name),
                reason=spec.get("reason", ""),
                allow_if_context=tuple(
                    re.compile(c, re.IGNORECASE) for c in spec.get("allow_if_context", [])
                ),
                raw=spec["term"],
                display=spec.get("display", spec["term"]),
            )
        )
    return BrandVoice(
        banned_terms=tuple(terms),
        character_breaks=tuple(s.lower() for s in data.get("character_breaks", [])),
    )


@lru_cache(maxsize=4)
def load_catalog(config_dir: str | Path, brand_name: str = "Vet2Employ") -> Catalog:
    base = Path(config_dir)
    return Catalog(
        tracks=_load_tracks(base / "tracks.yaml"),
        manifest=_load_manifest(base / "knowledge_manifest.yaml"),
        brand_voice=_load_brand_voice(base / "brand_voice.yaml", brand_name),
    )

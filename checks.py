"""Deterministic verification checks.

These checks need no model call, run in milliseconds and catch the most damaging kinds of
hallucination: citations to evidence that does not exist, invented numbers, invented names,
invented tools and invented file names. They also enforce brand vocabulary and style.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from v2e_rag.domain.catalog import BannedTerm, BrandVoice
from v2e_rag.verification.claims import Claim

MAJOR = "major"
MINOR = "minor"


@dataclass
class Violation:
    claim_id: str
    check: str
    severity: str
    message: str
    details: list[str] = field(default_factory=list)

    def as_problem(self) -> str:
        extra = f" ({'; '.join(self.details)})" if self.details else ""
        return f"{self.claim_id}: {self.message}{extra}"


def _norm(text: str) -> str:
    text = (
        text.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    return re.sub(r"\s+", " ", text).lower()


# Citations -------------------------------------------------------------------------------


def check_citations(claims: list[Claim], known_ids: set[str]) -> list[Violation]:
    violations = []
    for claim in claims:
        if claim.kind == "sentence":
            continue
        unknown = [eid for eid in claim.evidence_ids if eid not in known_ids]
        if unknown:
            violations.append(
                Violation(
                    claim.claim_id, "citation", MAJOR, "cites evidence that does not exist", unknown
                )
            )
        if not [eid for eid in claim.evidence_ids if eid in known_ids]:
            violations.append(
                Violation(claim.claim_id, "citation", MAJOR, "has no valid evidence citation")
            )
    return violations


# Numbers -------------------------------------------------------------------------------

_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])[\u00a3$\u20ac]?\d+(?:[.,:]\d+)*(?:\s?%|\s?percent)?(?![A-Za-z0-9_])"
)
_WORD_NUMBERS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "fifteen": "15",
    "twenty": "20",
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
}


def _number_core(token: str) -> str:
    return re.sub(r"[\u00a3$\u20ac%\s]|percent", "", token).rstrip(".,")


def _numbers_in(text: str) -> set[str]:
    found = {_number_core(m.group(0)) for m in _NUMBER_RE.finditer(text)}
    lowered = text.lower()
    for word, digit in _WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b", lowered):
            found.add(digit)
    expanded = set(found)
    for value in found:
        expanded.add(value.replace(",", ""))
        if re.fullmatch(r"\d+\.0+", value):
            expanded.add(value.split(".")[0])
        if re.fullmatch(r"\d+", value):
            expanded.add(f"{value}.0")
    return expanded


def check_numbers(
    claims: list[Claim], evidence: dict[str, str], request_text: str
) -> list[Violation]:
    """Every number in a claim must appear in its cited evidence (or the request itself)."""
    violations = []
    request_numbers = _numbers_in(request_text)
    all_text = " ".join(evidence.values())
    all_numbers = _numbers_in(all_text)
    for claim in claims:
        numbers = {_number_core(m.group(0)) for m in _NUMBER_RE.finditer(claim.text)}
        if not numbers:
            continue
        if claim.kind == "sentence":
            allowed = all_numbers | request_numbers
        else:
            cited = " ".join(evidence[eid] for eid in claim.evidence_ids if eid in evidence)
            allowed = _numbers_in(cited) | request_numbers
        missing = sorted(
            n for n in numbers if n not in allowed and n.replace(",", "") not in allowed
        )
        if missing:
            violations.append(
                Violation(
                    claim.claim_id,
                    "number",
                    MAJOR,
                    "contains numbers not found in the cited evidence",
                    missing,
                )
            )
    return violations


# Named entities --------------------------------------------------------------------------

_SENTENCE_START_RE = re.compile(r"(?:^|[.!?:;]\s+|\n\s*|[\"(]\s*)$")

BASE_ALLOWLIST = {
    "i",
    "ok",
    "ai",
    "uk",
    "the",
    "a",
    "an",
    "and",
    "or",
    "if",
    "for",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "england",
    "united",
    "kingdom",
    "phexara",
    "limited",
    "ltd",
    "manager",
    "track",
    "supervisor",
    "programme",
    "office",
    "client",
    "persona",
    "peer",
    "reviewer",
    "candidate",
    "team",
    "verified",
    "profile",
    "technical",
    "quality",
    "problem",
    "solving",
    "communication",
    "delivery",
    "professionalism",
    "hi",
    "hello",
    "thanks",
    "thank",
    "regards",
    "best",
    "kind",
    "dear",
    "please",
    "week",
    "one",
    "two",
    "day",
    "activity",
    "deliverable",
    "deliverables",
    "schedule",
    "assigned",
    "due",
    "estimated",
    "effort",
}


def _capitalised_words(text: str) -> list[tuple[str, bool]]:
    """Return (word, at_sentence_start) for capitalised words in text."""
    words = []
    for match in re.finditer(r"\b[A-Z][A-Za-z0-9&'.\-]*", text):
        prefix = text[: match.start()]
        at_start = bool(_SENTENCE_START_RE.search(prefix)) or not prefix.strip()
        words.append((match.group(0).strip(".'"), at_start))
    return words


def check_entities(
    claims: list[Claim],
    evidence_text: str,
    request_text: str,
    extra_allowlist: set[str],
) -> list[Violation]:
    """Flag capitalised words (names, organisations, products) that appear nowhere in the
    evidence pack or the request. Sentence initial words are only checked when they also
    appear capitalised mid sentence in the output, which marks them as proper nouns."""
    haystack = _norm(evidence_text + " " + request_text)
    allow = {w.lower() for w in BASE_ALLOWLIST | extra_allowlist}
    proper_mid = {w for claim in claims for w, start in _capitalised_words(claim.text) if not start}
    violations = []
    for claim in claims:
        unknown: list[str] = []
        for word, at_start in _capitalised_words(claim.text):
            key = word.lower()
            if key in allow or len(key) < 3:
                continue
            if at_start and word not in proper_mid:
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", haystack):
                continue
            unknown.append(word)
        if unknown:
            violations.append(
                Violation(
                    claim.claim_id,
                    "entity",
                    MAJOR,
                    "names people, organisations or products that appear nowhere in the evidence",
                    sorted(set(unknown)),
                )
            )
    return violations


# Tools and file names -------------------------------------------------------------------

_TOOL_SPLIT = re.compile(r"\s*(?::|\bfor\b|\bto\b|\(|,|\bwhich\b|\bused\b)\s*", re.IGNORECASE)


def tool_name(text: str) -> str:
    head = _TOOL_SPLIT.split(text.strip(), maxsplit=1)[0]
    return head.strip(" .;")


def check_tools(
    claims: list[Claim], evidence: dict[str, str], threshold: int = 88
) -> list[Violation]:
    violations = []
    for claim in claims:
        if claim.field != "tools_and_technologies":
            continue
        name = tool_name(claim.text)
        if not name:
            continue
        cited = _norm(" ".join(evidence[eid] for eid in claim.evidence_ids if eid in evidence))
        alternatives = [a.strip() for a in re.split(r"\bor\b|/|\band\b", name) if a.strip()]
        if not any(fuzz.partial_ratio(_norm(alt), cited) >= threshold for alt in alternatives):
            violations.append(
                Violation(
                    claim.claim_id,
                    "tool",
                    MAJOR,
                    "names a tool that the cited evidence does not mention",
                    [name],
                )
            )
    return violations


_FILE_RE = re.compile(r"\bV2E_[A-Za-z0-9_\[\]\-]+(?:\.[A-Za-z0-9]{2,5})?")


def check_file_names(claims: list[Claim], evidence_text: str) -> list[Violation]:
    haystack = evidence_text.replace(" ", "")
    violations = []
    for claim in claims:
        for name in _FILE_RE.findall(claim.text):
            if name not in haystack:
                violations.append(
                    Violation(
                        claim.claim_id,
                        "file_name",
                        MAJOR,
                        "uses a file name the evidence does not specify",
                        [name],
                    )
                )
    return violations


# Style -----------------------------------------------------------------------------------

DASH_RE = re.compile(r"[\u2013\u2014]")


def check_style(
    claims: list[Claim], brand: BrandVoice, forbid_dashes: bool, allow_ai_disclosure: bool = False
) -> list[Violation]:
    violations = []
    for claim in claims:
        lowered = claim.text.lower()
        for term in brand.banned_terms:
            for match in term.pattern.finditer(claim.text):
                window = claim.text[max(0, match.start() - 40) : match.end() + 40]
                if any(ctx.search(window) for ctx in term.allow_if_context):
                    continue
                violations.append(
                    Violation(
                        claim.claim_id,
                        "style",
                        MINOR,
                        f"uses '{match.group(0)}': {term.reason}",
                        [term.display],
                    )
                )
        if not allow_ai_disclosure:
            for phrase in brand.character_breaks:
                if phrase in lowered:
                    violations.append(
                        Violation(
                            claim.claim_id, "character", MINOR, f"breaks character: '{phrase}'"
                        )
                    )
        if forbid_dashes and DASH_RE.search(claim.text):
            violations.append(
                Violation(claim.claim_id, "style", MINOR, "uses an em dash or en dash")
            )
    return violations


def normalise_dashes(text: str) -> str:
    text = re.sub(r"(\d)\s*[\u2013\u2014]\s*(\d)", r"\1 to \2", text)
    text = re.sub(r"\s*[\u2013\u2014]\s*", ", ", text)
    return re.sub(r",\s*,", ",", text)


def apply_brand_replacements(text: str, brand: BrandVoice) -> str:
    for term in brand.banned_terms:

        def substitute(match: re.Match[str], term: BannedTerm = term, source: str = text) -> str:
            window = source[max(0, match.start() - 40) : match.end() + 40]
            if any(ctx.search(window) for ctx in term.allow_if_context):
                return match.group(0)
            return term.replacement

        text = term.pattern.sub(substitute, text)
    return re.sub(r"\s{2,}", " ", text).strip()

"""Redaction of personal data before any text leaves the service for a model provider.

The Work Brief requires participants to treat names, emails, postcodes and account numbers
as personal data and never paste them into external tools. The service applies the same
standard to every free text field a candidate controls before it is forwarded to OpenRouter
or written to the audit trail.

Patterns are ordered from most to least specific so that, for example, an IBAN is labelled
as an IBAN rather than as a long number.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("SECRET", re.compile(r"(?i)\b(?:api[_-]?key|token|password|passwd|secret)\s*[:=]\s*\S+")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b")),
    ("CARD_OR_ACCOUNT", re.compile(r"\b(?:\d[ \-]?){12,18}\d\b")),
    (
        "NI_NUMBER",
        re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b", re.IGNORECASE),
    ),
    (
        "PHONE",
        re.compile(r"(?<!\w)(?:\+44\s?\(?0?\)?\s?|\(?0)\d{2,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4}\b"),
    ),
    ("SORT_CODE", re.compile(r"\b\d{2}-\d{2}-\d{2}\b")),
    ("POSTCODE", re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.IGNORECASE)),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return the redacted text and a count of redactions per category."""
    counts: dict[str, int] = {}
    for label, pattern in _PATTERNS:
        text, n = pattern.subn(f"[{label} REDACTED]", text)
        if n:
            counts[label] = counts.get(label, 0) + n
    return text, counts

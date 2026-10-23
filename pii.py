"""Redaction of personal data before any text leaves the service for a model provider.

The Work Brief requires participants to treat names, emails, postcodes and account numbers
as personal data and never paste them into external tools. The service applies the same
standard to candidate messages it forwards to OpenRouter.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("CARD_OR_ACCOUNT", re.compile(r"\b(?:\d[ \-]?){12,18}\d\b")),
    (
        "NI_NUMBER",
        re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b", re.IGNORECASE),
    ),
    ("PHONE", re.compile(r"(?:(?:\+44\s?|0)(?:\d\s?){9,10})\b")),
    ("SORT_CODE", re.compile(r"\b\d{2}-\d{2}-\d{2}\b")),
    ("POSTCODE", re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")),
    ("URL_TOKEN", re.compile(r"(?i)\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+")),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for label, pattern in _PATTERNS:
        text, n = pattern.subn(f"[{label} REDACTED]", text)
        if n:
            counts[label] = counts.get(label, 0) + n
    return text, counts

"""Tokenisation and BM25 scoring used by the in memory lexical channel.

The PostgreSQL backend uses native full text search; this module gives the in memory
backend equivalent behaviour so that offline development and tests exercise the same
hybrid retrieval logic.
"""

from __future__ import annotations

import math
import re
from collections import Counter

STOPWORDS = frozenset(
    [
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
        "also",
        "may",
        "must",
        "shall",
        "within",
        "without",
        "via",
        "per",
        "across",
        "using",
        "use",
        "used",
    ]
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.]*[a-z0-9+#]|[a-z0-9]")


def _light_stem(token: str) -> str:
    for suffix in ("ingly", "ings", "ing", "edly", "ies", "ied", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            if suffix in ("ies", "ied"):
                return token[: -len(suffix)] + "y"
            return token[: -len(suffix)]
    return token


def tokenize(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    return [_light_stem(t) for t in tokens if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]


class BM25Index:
    def __init__(self, k1: float = 1.4, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.doc_terms: dict[str, Counter[str]] = {}
        self.doc_len: dict[str, int] = {}
        self.df: Counter[str] = Counter()

    def add(self, doc_id: str, text: str) -> None:
        self.remove(doc_id)
        terms = Counter(tokenize(text))
        self.doc_terms[doc_id] = terms
        self.doc_len[doc_id] = sum(terms.values())
        self.df.update(terms.keys())

    def remove(self, doc_id: str) -> None:
        terms = self.doc_terms.pop(doc_id, None)
        if terms is None:
            return
        self.doc_len.pop(doc_id, None)
        for term in terms:
            self.df[term] -= 1
            if self.df[term] <= 0:
                del self.df[term]

    def score(self, query: str, candidate_ids: set[str] | None = None) -> dict[str, float]:
        q_terms = set(tokenize(query))
        if not q_terms or not self.doc_terms:
            return {}
        n_docs = len(self.doc_terms)
        avg_len = sum(self.doc_len.values()) / n_docs
        scores: dict[str, float] = {}
        ids = candidate_ids if candidate_ids is not None else self.doc_terms.keys()
        for doc_id in ids:
            terms = self.doc_terms.get(doc_id)
            if not terms:
                continue
            total = 0.0
            for term in q_terms:
                tf = terms.get(term, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n_docs - self.df[term] + 0.5) / (self.df[term] + 0.5))
                norm = (
                    tf
                    * (self.k1 + 1)
                    / (tf + self.k1 * (1 - self.b + self.b * self.doc_len[doc_id] / avg_len))
                )
                total += idf * norm
            if total > 0:
                scores[doc_id] = total
        return scores

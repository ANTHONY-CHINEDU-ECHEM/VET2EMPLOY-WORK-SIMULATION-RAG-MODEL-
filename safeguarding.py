"""Deterministic safeguarding screen for candidate messages.

The AI Manager prompt already asks the model to flag wellbeing, harassment, fairness and
similar concerns, but a model can miss a disclosure. This screen runs first, from rules in
``config/safeguarding.yaml`` that the Programme Office owns:

* ``flag`` categories force ``needs_human_review`` on the reply, whatever the model decided;
* ``intercept`` categories (crisis by default) skip the model entirely and return a fixed,
  human written message that signposts urgent support.

The screen only ever adds caution. It never suppresses a review the model requested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

Action = Literal["intercept", "flag"]

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})


@dataclass(frozen=True)
class SafeguardingCategory:
    name: str
    action: Action
    review_reason: str
    patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class SafeguardingPolicy:
    categories: tuple[SafeguardingCategory, ...]
    intercept_message: str

    def with_crisis_action(self, action: Action) -> SafeguardingPolicy:
        categories = tuple(
            SafeguardingCategory(c.name, action, c.review_reason, c.patterns)
            if c.name == "crisis"
            else c
            for c in self.categories
        )
        return SafeguardingPolicy(categories, self.intercept_message)


@dataclass
class ScreenResult:
    categories: list[str] = field(default_factory=list)
    action: Literal["none", "flag", "intercept"] = "none"
    review_reasons: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.categories)

    @property
    def review_reason(self) -> str | None:
        return " ".join(self.review_reasons) if self.review_reasons else None

    def to_dict(self) -> dict[str, Any]:
        return {"flagged": self.flagged, "categories": self.categories, "action": self.action}


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_APOSTROPHES)).lower()


def screen(text: str, policy: SafeguardingPolicy) -> ScreenResult:
    """Return every category whose patterns match. Intercept outranks flag."""
    haystack = normalise(text)
    result = ScreenResult()
    for category in policy.categories:
        if any(p.search(haystack) for p in category.patterns):
            result.categories.append(category.name)
            result.review_reasons.append(category.review_reason.strip())
            if category.action == "intercept":
                result.action = "intercept"
            elif result.action == "none":
                result.action = "flag"
    return result


def intercept_text(policy: SafeguardingPolicy, first_name: str | None) -> str:
    name = f", {first_name}" if first_name else ""
    return policy.intercept_message.replace("{name}", name).strip()


def _load(path: Path) -> SafeguardingPolicy:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    categories = []
    for name, spec in (data.get("categories") or {}).items():
        action = spec.get("action", "flag")
        if action not in ("intercept", "flag"):
            raise ValueError(f"{path}: category {name} has unknown action {action!r}")
        patterns = tuple(re.compile(p, re.IGNORECASE) for p in spec.get("patterns", []))
        if not patterns:
            raise ValueError(f"{path}: category {name} has no patterns")
        categories.append(
            SafeguardingCategory(
                name=str(name),
                action=action,
                review_reason=str(spec.get("review_reason", "")).strip(),
                patterns=patterns,
            )
        )
    message = str(data.get("intercept_message", "")).strip()
    if any(c.action == "intercept" for c in categories) and not message:
        raise ValueError(f"{path}: intercept_message is required when a category intercepts")
    return SafeguardingPolicy(tuple(categories), message)


@lru_cache(maxsize=4)
def load_policy(config_dir: str | Path) -> SafeguardingPolicy | None:
    """Load ``safeguarding.yaml`` from the config directory, or ``None`` when absent."""
    path = Path(config_dir) / "safeguarding.yaml"
    if not path.exists():
        return None
    return _load(path)

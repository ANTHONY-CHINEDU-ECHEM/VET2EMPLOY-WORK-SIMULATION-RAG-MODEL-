"""House style for the repository.

* No em dash or en dash characters anywhere in source, configuration or documentation.
* No hyphens in documentation prose. Code spans, code blocks, table separator rows, URLs and
  file paths are exempt because they are syntax, not prose.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".sql",
    ".txt",
    ".cfg",
    ".ini",
    ".example",
    "",
}
SKIP_DIRS = {
    ".git",
    "knowledge_base",
    "var",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "build",
    "dist",
}
DASHES = re.compile("[\u2013\u2014]")


def _text_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(
            part in SKIP_DIRS or part.endswith(".egg-info") for part in path.parts
        ):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in {"Dockerfile", "Makefile", ".env.example"}:
            files.append(path)
    return files


def test_no_em_or_en_dashes_anywhere():
    offenders = []
    for path in _text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if DASHES.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, offenders


def _prose_lines(markdown: str) -> list[tuple[int, str]]:
    lines = []
    in_block = False
    for number, line in enumerate(markdown.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_block = not in_block
            continue
        if in_block or re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?", stripped):
            continue
        if stripped.startswith("<!--"):
            continue
        prose = re.sub(r"`[^`]*`", "", line)  # inline code
        prose = re.sub(r"\]\([^)]*\)", "]", prose)  # link targets
        prose = re.sub(r"https?://\S+", "", prose)  # bare URLs
        prose = re.sub(r"<[^>]+>", "", prose)  # HTML tags
        lines.append((number, prose))
    return lines


def _markdown_files() -> list[Path]:
    """Every Markdown file in the repository: README, docs, ADRs, policies and templates."""
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.parts)
    )


def test_documentation_prose_has_no_hyphens():
    offenders = []
    docs = _markdown_files()
    assert ROOT / "README.md" in docs
    for path in docs:
        if not path.exists():
            continue
        for number, prose in _prose_lines(path.read_text(encoding="utf-8")):
            if "-" in prose:
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {prose.strip()[:80]}")
    assert not offenders, offenders

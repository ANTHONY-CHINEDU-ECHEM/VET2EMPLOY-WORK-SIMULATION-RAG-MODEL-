"""Layout aware parsing of PDF and Word documents into an ordered block stream.

PDF parsing uses pdfplumber so that:

* tables are extracted as tables (rows and cells) instead of interleaved text;
* headings are detected from font size and weight relative to the body text, which is
  far more reliable than guessing from line patterns;
* running headers, footers and page numbers are removed by detecting lines that repeat
  across a large share of pages.

Word documents are parsed in body order using paragraph styles for headings and with
tables linearised in place.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from v2e_rag.domain.taxonomy import collapse_whitespace

BlockKind = Literal["heading", "text", "table"]

BULLET_RE = re.compile(r"^\s*([\u2022\u25aa\u25cf\u25e6\u2023\u2043*\-]|\d{1,2}[.)]|[a-z][.)])\s+")
PAGE_NUMBER_RE = re.compile(r"^(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?$", re.IGNORECASE)


@dataclass
class Block:
    kind: BlockKind
    page: int | None
    text: str = ""
    level: int = 0
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class _Line:
    text: str
    top: float
    size: float
    bold: bool
    in_margin: bool = False


# Table helpers --------------------------------------------------------------------------


def join_cell_lines(cell: str | None) -> str:
    """Join the wrapped lines of a table cell.

    Wrapped file names such as ``V2E_DA_[Surname]_D`` + ``1_AnalysisPlan_v1.pdf`` are
    rejoined without a space; ordinary prose is joined with a space.
    """
    if not cell:
        return ""
    lines = [ln.strip() for ln in str(cell).splitlines() if ln.strip()]
    if not lines:
        return ""
    out = lines[0]
    for line in lines[1:]:
        last_token = out.split(" ")[-1]
        if "_" in last_token and not last_token.endswith((",", ".", ";", ":")):
            out += line
        else:
            out += " " + line
    return collapse_whitespace(out)


def linearise_table(rows: list[list[str]]) -> list[str]:
    """Convert a table into self describing lines that embed and cite well."""
    clean = [[join_cell_lines(c) for c in row] for row in rows if row and any(c for c in row)]
    if not clean:
        return []
    width = max(len(r) for r in clean)
    clean = [r + [""] * (width - len(r)) for r in clean]
    if width == 1:
        return [r[0] for r in clean if r[0]]
    header = clean[0]
    header_like = all(h and len(h) <= 60 for h in header) and len(clean) > 1
    if not header_like:
        return [" | ".join(c for c in r if c) for r in clean]
    lines = []
    for row in clean[1:]:
        parts = [f"{h}: {v}" for h, v in zip(header, row, strict=False) if v]
        if parts:
            lines.append(" | ".join(parts))
    return lines


def assemble_paragraphs(lines: list[str]) -> str:
    """Merge wrapped lines into paragraphs while keeping list items on their own lines."""
    paragraphs: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        starts_item = bool(BULLET_RE.match(line))
        if not paragraphs or starts_item or paragraphs[-1].endswith(":"):
            paragraphs.append(line)
        else:
            paragraphs[-1] = f"{paragraphs[-1]} {line}"
    return "\n".join(collapse_whitespace(p) for p in paragraphs)


# PDF -------------------------------------------------------------------------------------


# Running headers and footers live in the top and bottom margins of a page. Only lines in
# these zones are candidates for removal, so repeated labels inside the body (for example
# "Objective" or "You are done when" in every activity) are always kept.
MARGIN_SHARE = 0.09


def _is_boilerplate(line: _Line, signatures: set[str]) -> bool:
    return line.in_margin and _boilerplate_signature(line.text) in signatures


def _is_label(line: _Line) -> bool:
    text = line.text
    return (
        line.bold
        and len(text.split()) <= 8
        and not text.endswith((".", ",", ";", "!", "?"))
        and not BULLET_RE.match(text)
        and bool(re.search(r"[A-Za-z]{3,}", text))
    )


def _boilerplate_signature(text: str) -> str:
    return re.sub(r"\d+", "#", text.lower()).strip()


def _line_style(chars: list[dict[str, Any]]) -> tuple[float, bool]:
    sizes = Counter(round(float(c.get("size", 0.0)), 1) for c in chars)
    size = sizes.most_common(1)[0][0] if sizes else 0.0
    bold_chars = sum(1 for c in chars if "bold" in str(c.get("fontname", "")).lower())
    return size, bold_chars >= max(1, int(0.6 * len(chars)))


def _flush_text(pending: list[str], page_number: int, blocks: list[Block]) -> None:
    if pending:
        text = assemble_paragraphs(pending)
        if text:
            blocks.append(Block(kind="text", page=page_number, text=text))
        pending.clear()


def parse_pdf(path: Path, layout: str = "report") -> list[Block]:
    import pdfplumber

    pages: list[tuple[int, list[_Line], list[tuple[float, list[list[str]]]]]] = []
    size_weights: Counter[float] = Counter()

    with pdfplumber.open(str(path)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            tables = page.find_tables()
            bboxes = [t.bbox for t in tables]

            def outside_tables(
                obj: dict[str, Any], boxes: list[tuple[float, ...]] = bboxes
            ) -> bool:
                if obj.get("object_type") != "char":
                    return True
                cx = (obj["x0"] + obj["x1"]) / 2
                cy = (obj["top"] + obj["bottom"]) / 2
                return not any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes)

            view = page.filter(outside_tables) if bboxes else page
            lines: list[_Line] = []
            for raw in view.extract_text_lines(return_chars=True, strip=True):
                text = collapse_whitespace(raw.get("text", ""))
                chars = raw.get("chars", [])
                if not text or not chars:
                    continue
                size, bold = _line_style(chars)
                size_weights[size] += len(chars)
                top = float(raw.get("top", 0.0))
                bottom = float(raw.get("bottom", top))
                in_margin = top < page.height * MARGIN_SHARE or bottom > page.height * (
                    1 - MARGIN_SHARE
                )
                lines.append(_Line(text=text, top=top, size=size, bold=bold, in_margin=in_margin))
            table_blocks = []
            for table in tables:
                try:
                    rows = table.extract()
                except Exception:  # pragma: no cover, pdfplumber edge cases
                    continue
                table_blocks.append((float(table.bbox[1]), [[c or "" for c in r] for r in rows]))
            pages.append((page_number, lines, table_blocks))

    if not pages:
        return []

    body_size = size_weights.most_common(1)[0][0] if size_weights else 10.0

    # Running headers and footers repeat on many pages. Remove them.
    signature_pages: Counter[str] = Counter()
    for _, lines, _ in pages:
        for sig in {_boilerplate_signature(ln.text) for ln in lines if ln.in_margin}:
            signature_pages[sig] += 1
    threshold = max(3, int(0.3 * len(pages)))
    boilerplate = {sig for sig, count in signature_pages.items() if count >= threshold}

    def is_heading_candidate(line: _Line) -> bool:
        if line.size < body_size * 1.03 or not (line.bold or line.size >= body_size * 1.3):
            return False
        if len(line.text) > 140 or len(line.text.split()) > 18:
            return False
        return bool(re.search(r"[A-Za-z]{2,}", line.text))

    # Structural heading sizes are sizes used by at least three heading lines. Display
    # sizes on cover pages appear once or twice and are treated as top level headings.
    candidate_sizes: Counter[float] = Counter(
        ln.size
        for _, lines, _ in pages
        for ln in lines
        if is_heading_candidate(ln) and not _is_boilerplate(ln, boilerplate)
    )
    structural = sorted((s for s, n in candidate_sizes.items() if n >= 3), reverse=True)

    # Some documents (for example exported strategy portfolios) set headings in bold at the
    # body size. When size based headings are too sparse, fall back to weight based rules.
    use_weight_headings = sum(candidate_sizes.values()) < max(5, int(len(pages) * 0.2))

    def weight_heading_level(line: _Line) -> int:
        text = line.text
        if not line.bold or line.size < body_size * 0.95:
            return 0
        if len(text) > 80 or len(text.split()) > 10 or text.endswith((".", ",", ";")):
            return 0
        if not re.search(r"[A-Za-z]{2,}", text):
            return 0
        letters = [c for c in text if c.isalpha()]
        is_label = "\u00b7" in text or (
            letters and sum(c.isupper() for c in letters) / len(letters) > 0.8
        )
        return 1 if is_label else 2

    def heading_level(line: _Line) -> int:
        if use_weight_headings:
            return weight_heading_level(line)
        if not is_heading_candidate(line):
            return 0
        larger = sum(1 for s in structural if s > line.size)
        return min(larger + 1, 6)

    blocks: list[Block] = []
    for page_number, lines, tables in pages:
        events: list[tuple[float, str, Any]] = []
        for ln in lines:
            if _is_boilerplate(ln, boilerplate) or PAGE_NUMBER_RE.match(ln.text):
                continue
            events.append((ln.top, "line", ln))
        for top, rows in tables:
            events.append((top, "table", rows))
        events.sort(key=lambda e: e[0])

        pending: list[str] = []
        last_heading: Block | None = None

        for _, kind, payload in events:
            if kind == "table":
                _flush_text(pending, page_number, blocks)
                last_heading = None
                blocks.append(Block(kind="table", page=page_number, rows=payload))
                continue
            line: _Line = payload
            level = heading_level(line)
            if level:
                _flush_text(pending, page_number, blocks)
                if last_heading is not None and last_heading.level == level:
                    last_heading.text = f"{last_heading.text} {line.text}"  # wrapped heading
                    continue
                last_heading = Block(kind="heading", page=page_number, text=line.text, level=level)
                blocks.append(last_heading)
            elif _is_label(line):
                # Short bold labels ("Objective", "You are done when") start a new paragraph
                # and end with a colon so the text that follows reads as their content.
                last_heading = None
                _flush_text(pending, page_number, blocks)
                pending.append(line.text if line.text.endswith(":") else f"{line.text}:")
            else:
                last_heading = None
                pending.append(line.text)
        _flush_text(pending, page_number, blocks)
        if layout == "deck":
            blocks.append(
                Block(kind="heading", page=page_number, text="", level=-1)
            )  # page break marker
    return blocks


# DOCX ------------------------------------------------------------------------------------


def _iter_docx_body(document: Any) -> Iterator[Any]:
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(child, document)
        elif tag == "tbl":
            yield Table(child, document)


def _style_name(paragraph: Any) -> str:
    try:
        return paragraph.style.name if paragraph.style is not None else ""
    except Exception:
        return ""


def parse_docx(path: Path) -> list[Block]:
    import docx
    from docx.table import Table

    document = docx.Document(str(path))
    blocks: list[Block] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            blocks.append(Block(kind="text", page=None, text="\n".join(pending)))
            pending.clear()

    for element in _iter_docx_body(document):
        if isinstance(element, Table):
            rows = [[cell.text for cell in row.cells] for row in element.rows]
            # Deduplicate merged cells that python-docx repeats across a row.
            rows = [list(dict.fromkeys(r)) if len(set(r)) < len(r) else r for r in rows]
            if len(rows) == 1 and len(rows[0]) == 1:
                pending.append(assemble_paragraphs(rows[0][0].splitlines()))
            else:
                flush()
                blocks.append(Block(kind="table", page=None, rows=rows))
            continue
        text = collapse_whitespace(element.text or "")
        if not text:
            continue
        style = _style_name(element).lower()
        match = re.match(r"heading\s*(\d)", style)
        if match or style == "title":
            flush()
            level = int(match.group(1)) if match else 1
            blocks.append(Block(kind="heading", page=None, text=text, level=level))
        elif "list" in style:
            pending.append(f"\u2022 {text}")
        else:
            pending.append(text)
    flush()
    return blocks

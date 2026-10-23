"""Ingestion orchestrator.

For each file in the knowledge base directory the pipeline:

1. resolves how to read it (manifest rule for documents, inference for workbooks);
2. skips it when its content hash is unchanged since the last run (idempotent re-runs);
3. parses it into chunks and structured records;
4. embeds chunk text through the cached embedder;
5. atomically replaces the document in the store;
6. writes a run summary, including every data quality warning, for review.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from v2e_rag.domain.catalog import Catalog
from v2e_rag.domain.models import Chunk, RoleRecord, SourceDocument, sha256_hex
from v2e_rag.domain.taxonomy import SourceType
from v2e_rag.embeddings.base import Embedder, prepare_embedding_text
from v2e_rag.ingestion.chunking import DocumentChunker, RoleChunker, chunk_reference_sheet
from v2e_rag.ingestion.document_parser import parse_docx, parse_pdf
from v2e_rag.ingestion.excel_loader import WorkbookLoader
from v2e_rag.ingestion.filename_metadata import infer_file_metadata, strip_upload_prefix
from v2e_rag.observability.logging import get_logger
from v2e_rag.store.base import VectorStore

log = get_logger(__name__)

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".xlsx"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def document_id_for(file_name: str) -> str:
    stem = strip_upload_prefix(file_name).rsplit(".", 1)[0].lower()
    return "doc_" + re.sub(r"[^a-z0-9]+", "_", stem).strip("_")


@dataclass
class ParsedUnit:
    document: SourceDocument
    chunks: list[Chunk]
    records: list[RoleRecord] = field(default_factory=list)


@dataclass
class FileOutcome:
    file: str
    status: str
    documents: int = 0
    chunks: int = 0
    records: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0


@dataclass
class IngestionReport:
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    files: list[FileOutcome] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        return {
            "files": len(self.files),
            "ingested": sum(1 for f in self.files if f.status == "ingested"),
            "unchanged": sum(1 for f in self.files if f.status == "unchanged"),
            "failed": sum(1 for f in self.files if f.status == "failed"),
            "skipped": sum(1 for f in self.files if f.status == "skipped"),
            "chunks": sum(f.chunks for f in self.files),
            "records": sum(f.records for f in self.files),
            "warnings": sum(len(f.warnings) for f in self.files),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "totals": self.totals,
            "files": [f.__dict__ for f in self.files],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Ingestion report",
            "",
            f"Run `{self.run_id}` started {self.started_at.isoformat()}.",
            "",
            "| Measure | Value |",
            "| :--- | ---: |",
        ]
        lines += [f"| {k.capitalize()} | {v} |" for k, v in self.totals.items()]
        lines += [
            "",
            "## Files",
            "",
            "| File | Status | Chunks | Records | Warnings |",
            "| :--- | :--- | ---: | ---: | ---: |",
        ]
        lines += [
            f"| {f.file} | {f.status} | {f.chunks} | {f.records} | {len(f.warnings)} |"
            for f in self.files
        ]
        warnings = [(f.file, w) for f in self.files for w in f.warnings]
        if warnings:
            lines += ["", "## Data quality findings", ""]
            lines += [f"* {w}" for _, w in warnings]
        errors = [f for f in self.files if f.error]
        if errors:
            lines += ["", "## Errors", ""]
            lines += [f"* {f.file}: {f.error}" for f in errors]
        return "\n".join(lines) + "\n"


class IngestionPipeline:
    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        catalog: Catalog,
        chunk_max_tokens: int = 450,
        chunk_overlap_tokens: int = 60,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.catalog = catalog
        self.doc_chunker = DocumentChunker(catalog, chunk_max_tokens, chunk_overlap_tokens)
        self.role_chunker = RoleChunker(chunk_max_tokens)
        self.chunk_max_tokens = chunk_max_tokens

    # Parsing -----------------------------------------------------------------------------

    def parse_file(self, path: Path, content_hash: str) -> tuple[list[ParsedUnit], list[str]]:
        name = strip_upload_prefix(path.name)
        rule = self.catalog.manifest.document_rule_for(name)
        if rule is not None:
            document = SourceDocument(
                document_id=document_id_for(name),
                source_file=name,
                title=rule.title,
                source_type=rule.source_type,
                content_hash=content_hash,
                metadata={"layout": rule.layout},
            )
            if path.suffix.lower() == ".pdf":
                blocks = parse_pdf(path, rule.layout)
            elif path.suffix.lower() == ".docx":
                blocks = parse_docx(path)
            else:
                raise ValueError(f"Unsupported document type for {name}")
            chunks = self.doc_chunker.chunk(document, blocks)
            return [ParsedUnit(document, chunks)], []

        if self.catalog.manifest.is_workbook(name):
            return self._parse_workbook(path, name, content_hash)
        raise ValueError(f"No manifest rule matches {name}")

    def _parse_workbook(
        self, path: Path, name: str, content_hash: str
    ) -> tuple[list[ParsedUnit], list[str]]:
        meta = infer_file_metadata(name)
        override = self.catalog.manifest.override_for(name)
        families = (
            list(override.role_families)
            if override and override.role_families
            else list(meta.role_families)
        )
        sectors = list(override.sectors) if override and override.sectors else list(meta.sectors)
        collected_on = (
            override.collected_on if override and override.collected_on else meta.collected_on
        )
        warnings: list[str] = []
        if not families:
            warnings.append(
                f"{name}: role family could not be inferred from the file name; add a manifest override"
            )

        document = SourceDocument(
            document_id=document_id_for(name),
            source_file=name,
            title=f"Labour market research: {name.rsplit('.', 1)[0].replace('_', ' ')}",
            source_type=SourceType.JOB_MARKET,
            content_hash=content_hash,
            collected_on=collected_on,
            metadata={"role_families": families, "sectors": sectors},
        )
        parsed = WorkbookLoader(path, document.document_id, meta).load()
        warnings.extend(parsed.warnings)
        for record in parsed.records:
            record.role_families = families
            record.sectors = sectors
            record.collected_on = collected_on

        chunks: list[Chunk] = []
        for record in parsed.records:
            chunks.extend(self.role_chunker.chunk(document, record))
        for row in parsed.perspectives:
            chunks.append(
                self.role_chunker.chunk_perspective(
                    document, row, {"role_families": families, "sectors": sectors}
                )
            )
        units = [ParsedUnit(document, self._dedupe(chunks), parsed.records)]

        # Methodology and dictionary sheets repeat across workbooks; identical content
        # collapses to one reference document keyed by its content hash.
        for reference in parsed.references:
            ref_hash = sha256_hex(reference.text)
            ref_doc = SourceDocument(
                document_id=f"ref_{reference.kind.value}_{ref_hash[:12]}",
                source_file=name,
                title="Indeed rating methodology"
                if reference.kind.value == "methodology"
                else "Research data dictionary",
                source_type=SourceType.METHODOLOGY,
                content_hash=ref_hash,
                metadata={"sheet": reference.sheet},
            )
            units.append(
                ParsedUnit(
                    ref_doc, chunk_reference_sheet(ref_doc, reference, self.chunk_max_tokens)
                )
            )
        return units, warnings

    @staticmethod
    def _dedupe(chunks: list[Chunk]) -> list[Chunk]:
        seen: set[str] = set()
        unique = []
        for chunk in chunks:
            key = chunk.content_hash
            if key in seen:
                continue
            seen.add(key)
            unique.append(chunk)
        return unique

    # Orchestration -----------------------------------------------------------------------

    async def ingest_directory(self, directory: Path, force: bool = False) -> IngestionReport:
        report = IngestionReport(run_id=str(uuid.uuid4()), started_at=datetime.now(UTC))
        files = sorted(
            p for p in directory.iterdir() if p.is_file() and not p.name.startswith((".", "~$"))
        )
        seen_reference_docs: set[str] = set()
        for path in files:
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                report.files.append(
                    FileOutcome(
                        file=path.name, status="skipped", warnings=["Unsupported file type"]
                    )
                )
                continue
            report.files.append(
                await self.ingest_file(path, force=force, seen_reference_docs=seen_reference_docs)
            )
        report.finished_at = datetime.now(UTC)
        await self.store.record_ingestion_run(
            {
                "run_id": report.run_id,
                "started_at": report.started_at,
                "finished_at": report.finished_at,
                **report.to_dict(),
            }
        )
        log.info("ingestion_complete", **report.totals)
        return report

    async def ingest_file(
        self, path: Path, force: bool = False, seen_reference_docs: set[str] | None = None
    ) -> FileOutcome:
        started = time.perf_counter()
        name = strip_upload_prefix(path.name)
        outcome = FileOutcome(file=name, status="ingested")
        try:
            content_hash = file_sha256(path)
            existing = await self.store.get_document(document_id_for(name))
            if existing and existing.content_hash == content_hash and not force:
                outcome.status = "unchanged"
                return outcome
            units, warnings = self.parse_file(path, content_hash)
            outcome.warnings.extend(warnings)
            for unit in units:
                if (
                    seen_reference_docs is not None
                    and unit.document.source_type == SourceType.METHODOLOGY
                ):
                    if unit.document.document_id in seen_reference_docs:
                        continue
                    seen_reference_docs.add(unit.document.document_id)
                texts = [prepare_embedding_text(c.text) for c in unit.chunks]
                vectors = await self.embedder.embed_documents(texts) if texts else []
                await self.store.replace_document(
                    unit.document, unit.chunks, vectors, unit.records, self.embedder.model
                )
                outcome.documents += 1
                outcome.chunks += len(unit.chunks)
                outcome.records += len(unit.records)
            log.info("file_ingested", file=name, chunks=outcome.chunks, records=outcome.records)
        except Exception as exc:
            outcome.status = "failed"
            outcome.error = f"{type(exc).__name__}: {exc}"
            log.error("file_ingestion_failed", file=name, error=outcome.error)
        finally:
            outcome.seconds = round(time.perf_counter() - started, 2)
        return outcome

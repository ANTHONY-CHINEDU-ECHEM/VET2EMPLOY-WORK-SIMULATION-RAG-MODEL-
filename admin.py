"""Administrative endpoints for knowledge base operations. Require an admin API key.

Ingestion runs hold the store's ingestion lock (a PostgreSQL advisory lock on the pgvector
backend), so only one run can write to the knowledge base at a time across every replica
and the CLI. Run status is kept in the memory of the replica that started the run; the
durable record of every completed run is the ``ingestion_runs`` table.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status

from v2e_rag.api.schemas import (
    DocumentOut,
    ErrorResponse,
    IngestionOutcomeOut,
    IngestionRunOut,
)
from v2e_rag.api.security import require_admin_key
from v2e_rag.errors import IngestionLockedError
from v2e_rag.ingestion.pipeline import SUPPORTED_SUFFIXES
from v2e_rag.observability.logging import get_logger

router = APIRouter(
    prefix="/v1/admin",
    tags=["Administration"],
    dependencies=[Depends(require_admin_key)],
    responses={
        401: {"model": ErrorResponse, "description": "Missing admin key"},
        403: {"model": ErrorResponse, "description": "Admin privileges required"},
    },
)
log = get_logger(__name__)

# Magic numbers of the accepted formats. .docx and .xlsx are zip containers.
_SIGNATURES = {".pdf": (b"%PDF-",), ".docx": (b"PK\x03\x04",), ".xlsx": (b"PK\x03\x04",)}


def _write_atomically(directory: Path, name: str, suffix: str, content: bytes) -> Path:
    """Write to a temporary file in the same directory, then rename: a reader (or a crash)
    never sees a half written file in the knowledge base."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".upload.", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    except OSError:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target


def _track(request: Request, task: asyncio.Task[None]) -> None:
    tasks: set[asyncio.Task[None]] = request.app.state.background_tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(request: Request) -> list[dict[str, Any]]:
    documents = await request.app.state.container.store.list_documents()
    return [d.model_dump(mode="json") for d in documents]


@router.post(
    "/ingest",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestionRunOut,
    responses={409: {"model": ErrorResponse, "description": "A run is already in progress"}},
)
async def start_ingestion(
    request: Request, force: bool = False, prune: bool = False
) -> dict[str, Any]:
    """Ingest every supported file in the configured knowledge base directory in the
    background. Unchanged files are skipped unless ``force`` is true. With ``prune``,
    documents whose source file has been removed from the directory are deleted."""
    container = request.app.state.container
    runs: dict[str, dict[str, Any]] = request.app.state.ingestion_runs
    if any(run["status"] == "running" for run in runs.values()):
        raise HTTPException(status.HTTP_409_CONFLICT, "An ingestion run is already in progress")
    run_id = str(uuid.uuid4())
    runs[run_id] = {
        "run_id": run_id,
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
    }

    async def run() -> None:
        try:
            report = await container.ingestion_pipeline().ingest_directory(
                container.settings.knowledge_base_dir, force, prune
            )
            runs[run_id] = {"status": "completed", **report.to_dict(), "run_id": run_id}
        except IngestionLockedError as exc:
            runs[run_id] = {"run_id": run_id, "status": "conflict", "error": str(exc)}
        except Exception as exc:  # surfaced through the run status
            log.exception("ingestion_run_failed", run_id=run_id)
            runs[run_id] = {
                "run_id": run_id,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }

    _track(request, asyncio.create_task(run()))
    return runs[run_id]


@router.get(
    "/ingest/{run_id}",
    response_model=IngestionRunOut,
    responses={404: {"model": ErrorResponse, "description": "Unknown run id"}},
)
async def ingestion_status(run_id: str, request: Request) -> dict[str, Any]:
    run: dict[str, Any] | None = request.app.state.ingestion_runs.get(run_id)
    if run is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Unknown run id. Status is held by the replica that started the run; completed "
            "runs are recorded in the ingestion_runs table.",
        )
    return run


@router.post(
    "/documents",
    status_code=status.HTTP_201_CREATED,
    response_model=IngestionOutcomeOut,
    responses={
        400: {"model": ErrorResponse, "description": "Unsupported or malformed file"},
        409: {"model": ErrorResponse, "description": "An ingestion run is in progress"},
        413: {"model": ErrorResponse, "description": "File too large"},
        422: {"model": ErrorResponse, "description": "The file could not be ingested"},
    },
)
async def upload_document(request: Request, file: UploadFile) -> dict[str, Any]:
    """Upload a new or updated knowledge base file and ingest it immediately."""
    container = request.app.state.container
    limit: int = container.settings.max_upload_bytes
    name = Path(file.filename or "").name
    suffix = Path(name).suffix.lower()
    if not name or suffix not in SUPPORTED_SUFFIXES or name.startswith((".", "~$")):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Supported types: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"File exceeds {limit // (1024 * 1024)} MB",
        )
    if not content.startswith(_SIGNATURES[suffix]):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"The file content is not a valid {suffix} file"
        )
    try:
        target = await asyncio.to_thread(
            _write_atomically, container.settings.knowledge_base_dir, name, suffix, content
        )
    except OSError as exc:
        log.error("upload_write_failed", file=name, error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The knowledge base directory is not writable by the service.",
        ) from exc

    outcome = await container.ingestion_pipeline().ingest_single(target)
    if outcome.status == "failed":
        raise HTTPException(422, outcome.error or "Ingestion failed")
    log.info("document_uploaded", file=name, chunks=outcome.chunks)
    return dict(outcome.__dict__)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ErrorResponse, "description": "Unknown document"}},
)
async def delete_document(document_id: str, request: Request) -> None:
    """Remove a document with its chunks and records. Remove or replace its source file as
    well, otherwise the next directory ingestion will add it back."""
    deleted = await request.app.state.container.store.delete_document(document_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown document id")
    log.info("document_deleted", document_id=document_id)

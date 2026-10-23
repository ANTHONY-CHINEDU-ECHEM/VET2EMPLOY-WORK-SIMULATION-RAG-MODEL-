"""Administrative endpoints for knowledge base operations. Require an admin API key."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status

from v2e_rag.api.security import require_admin_key
from v2e_rag.ingestion.pipeline import SUPPORTED_SUFFIXES
from v2e_rag.observability.logging import get_logger

router = APIRouter(
    prefix="/v1/admin", tags=["Administration"], dependencies=[Depends(require_admin_key)]
)
log = get_logger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@router.get("/documents")
async def list_documents(request: Request) -> list[dict[str, Any]]:
    documents = await request.app.state.container.store.list_documents()
    return [d.model_dump(mode="json") for d in documents]


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def start_ingestion(request: Request, force: bool = False) -> dict[str, Any]:
    """Ingest every supported file in the configured knowledge base directory in the
    background. Unchanged files are skipped unless ``force`` is true."""
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
                container.settings.knowledge_base_dir, force
            )
            runs[run_id] = {"run_id": run_id, "status": "completed", **report.to_dict()}
        except Exception as exc:  # pragma: no cover, surfaced through the run status
            log.error("ingestion_run_failed", run_id=run_id, error=str(exc))
            runs[run_id] = {"run_id": run_id, "status": "failed", "error": str(exc)}

    request.app.state.background_tasks.add(asyncio.create_task(run()))
    return runs[run_id]


@router.get("/ingest/{run_id}")
async def ingestion_status(run_id: str, request: Request) -> dict[str, Any]:
    run = request.app.state.ingestion_runs.get(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown run id")
    return run


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(request: Request, file: UploadFile) -> dict[str, Any]:
    """Upload a new or updated knowledge base file and ingest it immediately."""
    container = request.app.state.container
    name = Path(file.filename or "").name
    if not name or Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Supported types: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File exceeds 50 MB")
    target_dir: Path = container.settings.knowledge_base_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    target.write_bytes(content)
    outcome = await container.ingestion_pipeline().ingest_file(target, force=True)
    if outcome.status == "failed":
        raise HTTPException(422, outcome.error or "Ingestion failed")
    return outcome.__dict__


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: str, request: Request) -> None:
    await request.app.state.container.store.delete_document(document_id)

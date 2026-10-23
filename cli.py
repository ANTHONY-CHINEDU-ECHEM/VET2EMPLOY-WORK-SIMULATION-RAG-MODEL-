"""Command line interface: ``v2e --help``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer

from v2e_rag.config import get_settings
from v2e_rag.container import build_container
from v2e_rag.observability.logging import configure_logging
from v2e_rag.store.memory_store import MemoryStore

app = typer.Typer(
    add_completion=False, help="Vet2Employ RAG service operations.", no_args_is_help=True
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _print(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str, ensure_ascii=False))


@app.command()
def migrate() -> None:
    """Apply database migrations (pgvector backend)."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if settings.vector_backend != "pgvector":
        typer.echo("Memory backend selected: no migrations to apply.")
        raise typer.Exit(0)
    from v2e_rag.store.migrations import apply_migrations

    applied = apply_migrations(
        settings.database_url, settings.embedding_dimensions, settings.migrations_dir
    )
    typer.echo(f"Applied migrations: {', '.join(applied) if applied else 'none (up to date)'}")


@app.command()
def ingest(
    path: Path | None = typer.Option(
        None, help="Directory to ingest. Defaults to V2E_KNOWLEDGE_BASE_DIR."
    ),
    force: bool = typer.Option(False, help="Re-ingest files even when unchanged."),
) -> None:
    """Parse, chunk, embed and store the knowledge base. Writes a data quality report."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    async def main() -> dict[str, Any]:
        container = build_container(settings)
        await container.start()
        try:
            report = await container.ingestion_pipeline().ingest_directory(
                path or settings.knowledge_base_dir, force
            )
            if isinstance(container.store, MemoryStore):
                container.store.save()
            settings.reports_dir.mkdir(parents=True, exist_ok=True)
            (settings.reports_dir / "ingestion_report.md").write_text(
                report.to_markdown(), encoding="utf-8"
            )
            (settings.reports_dir / "ingestion_report.json").write_text(
                json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8"
            )
            return report.totals
        finally:
            await container.stop()

    totals = _run(main())
    _print(totals)
    typer.echo(f"Report written to {settings.reports_dir / 'ingestion_report.md'}")
    if totals["failed"]:
        raise typer.Exit(1)


@app.command()
def stats() -> None:
    """Summarise what the knowledge base contains."""
    settings = get_settings()
    configure_logging("WARNING", settings.log_json)

    async def main() -> dict[str, Any]:
        container = build_container(settings)
        await container.start()
        try:
            return (await container.store.stats()).__dict__
        finally:
            await container.stop()

    _print(_run(main()))


@app.command()
def search(
    query: str,
    track: str | None = typer.Option(None, help="DA, SE, CY or DE"),
    role_family: str | None = typer.Option(None),
    limit: int = typer.Option(8),
) -> None:
    """Run hybrid retrieval and print the evidence with citations."""
    from v2e_rag.generation.requests import RetrieveRequest

    settings = get_settings()
    configure_logging("WARNING", settings.log_json)

    async def main() -> list[dict[str, Any]]:
        container = build_container(settings)
        await container.start()
        try:
            pack, _ = await container.service.retrieve(
                RetrieveRequest(query=query, track=track, role_family=role_family, limit=limit)
            )
            return [
                {
                    "id": i.evidence_id,
                    "score": i.score,
                    "citation": i.citation,
                    "text": i.text[:400],
                }
                for i in pack.items
            ]
        finally:
            await container.stop()

    _print(_run(main()))


@app.command("generate-task")
def generate_task(
    track: str | None = typer.Option(None, help="DA, SE, CY or DE"),
    role_family: str | None = typer.Option(None),
    activity: int | None = typer.Option(None, help="Activity number from the track brief"),
    task_type: str = typer.Option("core_activity"),
    job_level: str | None = typer.Option(None),
    sector: str | None = typer.Option(None),
    focus: str | None = typer.Option(None),
) -> None:
    """Generate one verified task end to end (requires OPENROUTER_API_KEY)."""
    from v2e_rag.generation.requests import TaskRequest

    settings = get_settings()
    configure_logging("WARNING", settings.log_json)

    async def main() -> dict[str, Any]:
        container = build_container(settings)
        await container.start()
        try:
            outcome = await container.service.generate_task(
                TaskRequest(
                    track=track,
                    role_family=role_family,
                    activity_number=activity,
                    task_type=task_type,
                    job_level=job_level,
                    sector=sector,
                    focus=focus,
                )
            )
            return {
                "task": outcome.output.model_dump(mode="json"),
                "verification": outcome.report.to_dict(),
                "evidence": [
                    {"id": i.evidence_id, "citation": i.citation} for i in outcome.evidence.items
                ],
                "audit_id": outcome.audit_id,
            }
        finally:
            await container.stop()

    _print(_run(main()))


@app.command("eval-retrieval")
def eval_retrieval(
    dataset: Path = typer.Option(Path("eval/retrieval_golden.yaml")),
    k: int = typer.Option(5),
) -> None:
    """Measure retrieval quality (hit rate, recall, MRR, nDCG) on the golden set."""
    from v2e_rag.evaluation.retrieval_eval import run_retrieval_eval

    settings = get_settings()
    configure_logging("WARNING", settings.log_json)

    async def main() -> dict[str, Any]:
        container = build_container(settings)
        await container.start()
        try:
            return await run_retrieval_eval(container, dataset, k)
        finally:
            await container.stop()

    result = _run(main())
    _print(result["summary"])
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    (settings.reports_dir / "retrieval_eval.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )


@app.command("eval-generation")
def eval_generation(dataset: Path = typer.Option(Path("eval/generation_scenarios.yaml"))) -> None:
    """Run generation scenarios end to end and report grounding metrics (uses OpenRouter)."""
    from v2e_rag.evaluation.generation_eval import run_generation_eval

    settings = get_settings()
    configure_logging("WARNING", settings.log_json)

    async def main() -> dict[str, Any]:
        container = build_container(settings)
        await container.start()
        try:
            return await run_generation_eval(container, dataset)
        finally:
            await container.stop()

    result = _run(main())
    _print(result["summary"])
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    (settings.reports_dir / "generation_eval.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0"),
    port: int = typer.Option(8000),
    workers: int = typer.Option(1),
) -> None:
    """Run the HTTP API."""
    import uvicorn

    uvicorn.run(
        "v2e_rag.api.app:app_factory",
        factory=True,
        host=host,
        port=port,
        workers=workers,
        proxy_headers=True,
    )


if __name__ == "__main__":
    app()

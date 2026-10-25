"""Command line interface: ``v2e --help``."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from v2e_rag import __version__
from v2e_rag.config import Settings, get_settings
from v2e_rag.container import build_container
from v2e_rag.domain.taxonomy import JobLevel, RoleFamily, Sector, TaskType, TrackCode
from v2e_rag.observability.logging import configure_logging
from v2e_rag.store.memory_store import MemoryStore

app = typer.Typer(
    add_completion=False, help="Vet2Employ RAG service operations.", no_args_is_help=True
)
audit_app = typer.Typer(help="Inspect and retain the generation audit trail.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _print(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str, ensure_ascii=False))


@app.command()
def version() -> None:
    """Print the service and prompt versions."""
    from v2e_rag.llm.prompts import PROMPT_VERSION

    _print({"version": __version__, "prompt_version": PROMPT_VERSION})


@app.command()
def migrate() -> None:
    """Apply database migrations (pgvector backend). Safe to run from several replicas."""
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
    prune: bool = typer.Option(
        False, help="Delete documents whose source file is no longer in the directory."
    ),
) -> None:
    """Parse, chunk, embed and store the knowledge base. Writes a data quality report."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    async def main() -> dict[str, Any]:
        container = build_container(settings)
        await container.start()
        try:
            report = await container.ingestion_pipeline().ingest_directory(
                path or settings.knowledge_base_dir, force, prune
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
            return {**report.totals, "pruned": len(report.pruned)}
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
            return dict((await container.store.stats()).__dict__)
        finally:
            await container.stop()

    _print(_run(main()))


@app.command()
def search(
    query: str,
    track: TrackCode | None = typer.Option(None, help="Work Brief track."),
    role_family: RoleFamily | None = typer.Option(None),
    limit: int = typer.Option(8, min=1, max=30),
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
    track: TrackCode | None = typer.Option(None, help="Work Brief track."),
    role_family: RoleFamily | None = typer.Option(None),
    activity: int | None = typer.Option(None, help="Activity number from the track brief"),
    task_type: TaskType = typer.Option(TaskType.CORE_ACTIVITY),
    job_level: JobLevel | None = typer.Option(None),
    sector: Sector | None = typer.Option(None),
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
    min_hit_rate: float | None = typer.Option(
        None, help="Exit with status 1 when hit rate at k falls below this value."
    ),
    min_mrr: float | None = typer.Option(
        None, help="Exit with status 1 when mean reciprocal rank falls below this value."
    ),
) -> None:
    """Measure retrieval quality (hit rate, MRR, precision, nDCG) on the golden set."""
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
    summary = result["summary"]
    _print(summary)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    (settings.reports_dir / "retrieval_eval.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    failures = []
    if min_hit_rate is not None and summary[f"hit_rate@{k}"] < min_hit_rate:
        failures.append(f"hit_rate@{k} {summary[f'hit_rate@{k}']} < {min_hit_rate}")
    if min_mrr is not None and summary["mrr"] < min_mrr:
        failures.append(f"mrr {summary['mrr']} < {min_mrr}")
    if failures:
        typer.echo("Retrieval quality gate failed: " + "; ".join(failures), err=True)
        raise typer.Exit(1)


@app.command("eval-generation")
def eval_generation(
    dataset: Path = typer.Option(Path("eval/generation_scenarios.yaml")),
    min_as_expected: float | None = typer.Option(
        None, help="Exit with status 1 when the share of scenarios behaving as expected is lower."
    ),
) -> None:
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
    rate = result["summary"]["as_expected_rate"]
    if min_as_expected is not None and rate < min_as_expected:
        typer.echo(f"Generation gate failed: as_expected_rate {rate} < {min_as_expected}", err=True)
        raise typer.Exit(1)


@app.command()
def openapi(
    output: Path = typer.Option(Path("docs/openapi.json"), help="Where to write the schema."),
    check: bool = typer.Option(
        False, help="Do not write; exit with status 1 if the file differs from the code."
    ),
) -> None:
    """Export the OpenAPI schema so the platform can generate a typed client."""
    from v2e_rag.api.app import create_app

    settings = Settings(_env_file=None, environment="test", log_level="WARNING")  # type: ignore[call-arg]
    schema = create_app(settings).openapi()
    text = json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if check:
        current = output.read_text(encoding="utf-8") if output.exists() else ""
        if current != text:
            typer.echo(f"{output} is out of date. Run `v2e openapi` and commit it.", err=True)
            raise typer.Exit(1)
        typer.echo(f"{output} is up to date.")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    typer.echo(f"OpenAPI schema written to {output}")


@app.command()
def doctor(
    live: bool = typer.Option(
        False, help="Also embed one short string through OpenRouter to prove the key works."
    ),
) -> None:
    """Check configuration, database, migrations, knowledge base and model access.

    Prints one line per check and exits with status 1 if any check fails. Run it after
    every deployment and before handing an environment to someone else.
    """
    from v2e_rag.domain.catalog import load_catalog
    from v2e_rag.safeguarding import load_policy

    settings = get_settings()
    configure_logging("WARNING", settings.log_json)
    results: list[tuple[str, str, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, "PASS" if ok else "FAIL", detail))

    problems = settings.production_problems()
    if not problems:
        check("settings safe for production", True, "no problems")
    elif settings.environment == "production":
        check("settings safe for production", False, "; ".join(problems))
    else:
        # Outside production these are expected; show them so nothing surprises a deploy.
        results.append(
            (
                "settings safe for production",
                "WARN",
                "would block production: " + "; ".join(problems),
            )
        )
    try:
        catalog = load_catalog(str(settings.config_dir), settings.brand_name)
        check("config: tracks, manifest, brand voice", True, f"{len(catalog.tracks)} tracks")
    except Exception as exc:
        check("config: tracks, manifest, brand voice", False, str(exc))
    try:
        policy = load_policy(str(settings.config_dir))
        check(
            "config: safeguarding",
            policy is not None or not settings.safeguarding_enabled,
            f"{len(policy.categories)} categories" if policy else "safeguarding.yaml missing",
        )
    except Exception as exc:
        check("config: safeguarding", False, str(exc))

    kb = settings.knowledge_base_dir
    files = [p for p in kb.glob("*") if p.suffix.lower() in {".pdf", ".docx", ".xlsx"}]
    check("knowledge base directory", kb.is_dir() and bool(files), f"{len(files)} files in {kb}")
    check("OpenRouter key present", settings.openrouter_configured)

    if settings.vector_backend == "pgvector":
        from v2e_rag.store.migrations import pending_migrations

        try:
            pending = pending_migrations(settings.database_url, settings.migrations_dir)
            check(
                "database migrations",
                not pending,
                f"pending: {pending}" if pending else "up to date",
            )
        except Exception as exc:
            check("database reachable", False, type(exc).__name__)

    async def store_checks() -> None:
        container = build_container(settings)
        try:
            await container.start()
            stats = await container.store.stats()
            check("knowledge base loaded", stats.chunks > 0, f"{stats.chunks} chunks")
            check(
                "embedding model matches stored vectors",
                stats.embedding_model in (None, container.embedder.model),
                f"stored {stats.embedding_model}, configured {container.embedder.model}",
            )
            if live and settings.embedding_provider == "openrouter":
                vector = await container.embedder.embed_query("Vet2Employ readiness check")
                check("OpenRouter embeddings reachable", len(vector) > 0, f"{len(vector)} dims")
        except Exception as exc:
            check("service container", False, f"{type(exc).__name__}: {exc}"[:200])
        finally:
            await container.stop()

    _run(store_checks())
    width = max(len(name) for name, _, _ in results)
    for name, state, detail in results:
        typer.echo(f"{state}  {name.ljust(width)}  {detail}")
    if any(state == "FAIL" for _, state, _ in results):
        raise typer.Exit(1)


@audit_app.command("show")
def audit_show(audit_id: str) -> None:
    """Print one audit record: request, evidence, response and verification."""
    settings = get_settings()
    configure_logging("WARNING", settings.log_json)

    async def main() -> dict[str, Any] | None:
        container = build_container(settings)
        await container.start()
        try:
            record: dict[str, Any] | None = await container.store.get_audit(audit_id)
            return record
        finally:
            await container.stop()

    record = _run(main())
    if record is None:
        typer.echo(f"No audit record {audit_id}", err=True)
        raise typer.Exit(1)
    _print(record)


@audit_app.command("purge")
def audit_purge(
    older_than_days: int | None = typer.Option(
        None, min=1, help="Defaults to V2E_AUDIT_RETENTION_DAYS."
    ),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation."),
) -> None:
    """Delete audit records older than the retention period."""
    settings = get_settings()
    configure_logging("WARNING", settings.log_json)
    days = older_than_days or settings.audit_retention_days
    if not days:
        typer.echo("Pass --older-than-days or set V2E_AUDIT_RETENTION_DAYS.", err=True)
        raise typer.Exit(2)
    before = datetime.now(UTC) - timedelta(days=days)
    if not yes and sys.stdin.isatty():
        typer.confirm(f"Delete audit records created before {before.isoformat()}?", abort=True)

    async def main() -> int:
        container = build_container(settings)
        await container.start()
        try:
            deleted: int = await container.store.purge_audit(before)
            return deleted
        finally:
            await container.stop()

    deleted = _run(main())
    _print({"deleted": deleted, "before": before.isoformat()})


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
        timeout_graceful_shutdown=30,
        # Requests are logged as structured JSON by the service middleware.
        access_log=False,
    )


if __name__ == "__main__":
    app()

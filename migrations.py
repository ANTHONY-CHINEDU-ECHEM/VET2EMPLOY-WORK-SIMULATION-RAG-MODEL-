"""Minimal, transparent SQL migration runner.

Migrations are plain numbered ``.sql`` files in ``migrations/`` so that a database
administrator can review or apply them by hand (for example on AlloyDB). The runner records
applied versions in ``schema_migrations`` and substitutes ``{{EMBEDDING_DIM}}``.

Several replicas may start at the same moment and each run ``v2e migrate`` from the
container entrypoint. A session level advisory lock serialises them, so exactly one
applies each migration and the rest find the schema up to date.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from v2e_rag.observability.logging import get_logger

log = get_logger(__name__)

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"
MIGRATION_LOCK_KEY = 0x7632_6500_0000_0002


class EmbeddingDimensionMismatch(RuntimeError):
    pass


class MigrationsNotFound(RuntimeError):
    pass


def _migration_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.sql") if p.name[:4].isdigit())


def resolve_migrations_dir(directory: Path | None) -> Path:
    """Use the configured directory, falling back to the repository copy. Fail loudly when
    neither holds migrations: a silent "up to date" against an empty schema hides a broken
    deployment until the first request."""
    for candidate in (directory, DEFAULT_MIGRATIONS_DIR):
        if candidate is not None and candidate.is_dir() and _migration_files(candidate):
            return candidate
    raise MigrationsNotFound(
        f"No migration files found in {directory} or {DEFAULT_MIGRATIONS_DIR}. "
        "Set V2E_MIGRATIONS_DIR to the folder that contains 0001_initial_schema.sql."
    )


def pending_migrations(database_url: str, directory: Path | None = None) -> list[str]:
    """Versions present on disk but not yet applied (used by ``v2e doctor``)."""
    directory = resolve_migrations_dir(directory)
    with psycopg.connect(database_url, autocommit=True) as conn:
        exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()
        done: set[str] = set()
        if exists and exists[0]:
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    return [p.stem for p in _migration_files(directory) if p.stem not in done]


def apply_migrations(
    database_url: str, embedding_dim: int, directory: Path | None = None
) -> list[str]:
    directory = resolve_migrations_dir(directory)
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for path in _migration_files(directory):
                version = path.stem
                if version in done:
                    continue
                sql = path.read_text(encoding="utf-8").replace(
                    "{{EMBEDDING_DIM}}", str(int(embedding_dim))
                )
                with conn.transaction():
                    conn.execute(sql.encode("utf-8"))
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                applied.append(version)
                log.info("migration_applied", version=version)
            verify_embedding_dimension(conn, embedding_dim)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
    return applied


def verify_embedding_dimension(conn: psycopg.Connection, expected: int) -> None:
    row = conn.execute(
        "SELECT a.atttypmod FROM pg_attribute a JOIN pg_class c ON a.attrelid = c.oid "
        "WHERE c.relname = 'kb_chunks' AND a.attname = 'embedding'"
    ).fetchone()
    if row is None:
        return
    actual = int(row[0])
    if actual > 0 and actual != expected:
        raise EmbeddingDimensionMismatch(
            f"kb_chunks.embedding has {actual} dimensions but V2E_EMBEDDING_DIMENSIONS is {expected}. "
            "Use the matching embedding model or create a new database and re-ingest."
        )

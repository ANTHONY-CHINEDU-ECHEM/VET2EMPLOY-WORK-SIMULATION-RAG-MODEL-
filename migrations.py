"""Minimal, transparent SQL migration runner.

Migrations are plain numbered ``.sql`` files in ``migrations/`` so that a database
administrator can review or apply them by hand (for example on AlloyDB). The runner records
applied versions in ``schema_migrations`` and substitutes ``{{EMBEDDING_DIM}}``.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from v2e_rag.observability.logging import get_logger

log = get_logger(__name__)

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


class EmbeddingDimensionMismatch(RuntimeError):
    pass


def _migration_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.sql") if p.name[:4].isdigit())


def apply_migrations(
    database_url: str, embedding_dim: int, directory: Path | None = None
) -> list[str]:
    directory = directory or DEFAULT_MIGRATIONS_DIR
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
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
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
            log.info("migration_applied", version=version)
        verify_embedding_dimension(conn, embedding_dim)
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

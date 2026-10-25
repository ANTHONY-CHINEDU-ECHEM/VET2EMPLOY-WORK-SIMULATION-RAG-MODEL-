#!/bin/sh
# Container entrypoint.
#   serve      apply migrations (pgvector backend) then start the API
#   ingest     apply migrations (pgvector backend) then ingest the knowledge base
#   anything   passed straight to the v2e command line (doctor, stats, search, audit, ...)
#
# Migrations take a PostgreSQL advisory lock, so several replicas starting together are
# safe. Set V2E_SKIP_MIGRATIONS=true where migrations run as a separate release job.
set -eu

case "${1:-serve}" in
    serve|ingest)
        if [ "${V2E_VECTOR_BACKEND:-pgvector}" = "pgvector" ] && [ "${V2E_SKIP_MIGRATIONS:-false}" != "true" ]; then
            v2e migrate
        fi
        ;;
esac

if [ "${1:-serve}" = "serve" ]; then
    exec v2e serve --host 0.0.0.0 --port "${V2E_PORT:-8000}" --workers "${V2E_WORKERS:-2}"
fi

exec v2e "$@"

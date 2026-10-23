#!/bin/sh
# Container entrypoint.
#   serve      apply migrations (pgvector backend) then start the API
#   ingest     apply migrations (pgvector backend) then ingest the knowledge base
#   anything   passed straight to the v2e command line (stats, search, eval-retrieval, ...)
set -e

case "$1" in
    serve|ingest)
        if [ "${V2E_VECTOR_BACKEND:-pgvector}" = "pgvector" ]; then
            v2e migrate
        fi
        ;;
esac

if [ "$1" = "serve" ]; then
    exec v2e serve --host 0.0.0.0 --port "${V2E_PORT:-8000}" --workers "${V2E_WORKERS:-2}"
fi

exec v2e "$@"

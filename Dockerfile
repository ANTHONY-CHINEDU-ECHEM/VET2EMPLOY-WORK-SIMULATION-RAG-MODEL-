# syntax=docker/dockerfile:1.7
# Vet2Employ RAG service image. Multi stage: build wheels against the lock file, then a
# slim non root runtime. Dependency versions come from requirements/constraints.txt so
# every build of the same commit installs exactly the same packages.

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements/constraints.txt ./requirements/constraints.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --wheel-dir /wheels -c requirements/constraints.txt .

FROM python:${PYTHON_VERSION}-slim AS runtime
ARG VERSION=dev
LABEL org.opencontainers.image.title="vet2employ_rag" \
      org.opencontainers.image.description="Grounded RAG service for the Vet2Employ AI Manager" \
      org.opencontainers.image.vendor="Phexara Limited" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    V2E_CONFIG_DIR=/app/config \
    V2E_MIGRATIONS_DIR=/app/migrations \
    V2E_KNOWLEDGE_BASE_DIR=/app/knowledge_base/raw \
    V2E_REPORTS_DIR=/app/var/reports \
    V2E_AUDIT_LOG_PATH=/app/var/audit/generation_audit.jsonl \
    V2E_MEMORY_SNAPSHOT_PATH=/app/var/memory_store
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/*.whl && rm -rf /wheels
COPY config ./config
COPY migrations ./migrations
COPY eval ./eval
COPY scripts ./scripts
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/var/reports /app/var/audit /app/var/memory_store /app/knowledge_base/raw \
    && chown -R app:app /app/var /app/knowledge_base
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).status == 200 else 1)"
STOPSIGNAL SIGTERM
ENTRYPOINT ["entrypoint.sh"]
CMD ["serve"]

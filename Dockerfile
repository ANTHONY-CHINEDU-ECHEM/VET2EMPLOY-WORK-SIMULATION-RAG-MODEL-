# syntax=docker/dockerfile:1.7
# Vet2Employ RAG service image. Multi stage: build wheels, then a slim non root runtime.

FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --wheel-dir /wheels .

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    V2E_CONFIG_DIR=/app/config \
    V2E_MIGRATIONS_DIR=/app/migrations \
    V2E_KNOWLEDGE_BASE_DIR=/app/knowledge_base/raw \
    V2E_REPORTS_DIR=/app/var/reports
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install /wheels/*.whl && rm -rf /wheels
COPY config ./config
COPY migrations ./migrations
COPY eval ./eval
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/var/reports /app/var/audit /app/knowledge_base/raw \
    && chown -R app:app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).status == 200 else 1)"
ENTRYPOINT ["entrypoint.sh"]
CMD ["serve"]

# Developer shortcuts. Run `make help` for the list.

PYTHON ?= python3
TEST_DB ?= postgresql://v2e:v2e@localhost:5432/v2e_test
BASE_URL ?= http://localhost:8000

.PHONY: help install install-locked hooks lint format typecheck check test test-integration \
        test-all coverage audit lock openapi db-up migrate ingest serve doctor smoke \
        eval-retrieval eval-generation docker-build up down clean

help:
	@grep -E '^[a-z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

install: ## Install the package with development tools
	$(PYTHON) -m pip install -e ".[dev]"

install-locked: ## Install with the exact versions CI uses
	$(PYTHON) -m pip install -c requirements/constraints-dev.txt -e ".[dev]"

hooks: ## Install the pre commit hooks
	pre-commit install

lint: ## Static checks
	ruff check src tests scripts
	ruff format --check src tests scripts

format: ## Apply formatting
	ruff format src tests scripts
	ruff check --fix src tests scripts

typecheck: ## Type checks
	mypy src

check: lint typecheck test ## Everything a pull request must pass locally (no database)
	v2e openapi --check

test: ## Unit tests and repository style checks
	pytest tests/unit tests/test_repository_style.py -q

test-integration: ## PostgreSQL and real knowledge base tests (needs TEST_DB)
	V2E_TEST_DATABASE_URL=$(TEST_DB) pytest tests/integration -q

test-all: test test-integration ## Everything

coverage: ## Unit and PostgreSQL tests with a coverage report (needs TEST_DB)
	V2E_TEST_DATABASE_URL=$(TEST_DB) pytest tests/unit tests/test_repository_style.py \
		tests/integration/test_pgvector_store.py --cov --cov-report=term --cov-report=html -q

audit: ## Known vulnerability scan of the pinned dependencies
	pip-audit -r requirements/constraints.txt --progress-spinner off

lock: ## Regenerate requirements/ from pyproject.toml (needs uv)
	uv pip compile pyproject.toml --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
		-o requirements/constraints.txt --no-header --quiet
	uv pip compile pyproject.toml --extra dev --python-version 3.12 \
		--python-platform x86_64-manylinux_2_28 -o requirements/constraints-dev.txt --no-header --quiet
	@$(PYTHON) scripts/lock_headers.py

openapi: ## Regenerate docs/openapi.json from the code
	v2e openapi

db-up: ## Start PostgreSQL with pgvector
	docker compose up -d db

migrate: ## Apply database migrations
	v2e migrate

ingest: ## Ingest the knowledge base
	v2e ingest

serve: ## Run the API locally
	v2e serve --port 8000

doctor: ## Check configuration, database, migrations and knowledge base
	v2e doctor

smoke: ## Smoke test a running service (BASE_URL, optional API_KEY)
	$(PYTHON) scripts/smoke_test.py --base-url $(BASE_URL) $(if $(API_KEY),--api-key $(API_KEY),--offline)

eval-retrieval: ## Retrieval quality on the golden set, gated
	v2e eval-retrieval --min-hit-rate 0.9 --min-mrr 0.6

eval-generation: ## End to end generation scenarios (uses OpenRouter), gated
	v2e eval-generation --min-as-expected 0.9

docker-build: ## Build the container image
	docker build --build-arg VERSION=$$(git rev-parse --short HEAD 2>/dev/null || echo dev) \
		-t vet2employ_rag:latest .

up: ## Start database and API, then ingest
	docker compose up -d db api
	docker compose run --rm ingest

down: ## Stop the stack
	docker compose down

clean: ## Remove caches and local reports
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov coverage.xml junit.xml

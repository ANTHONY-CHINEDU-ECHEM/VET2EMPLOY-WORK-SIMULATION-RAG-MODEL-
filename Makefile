# Developer shortcuts. Run `make help` for the list.

PYTHON ?= python3
TEST_DB ?= postgresql://v2e:v2e@localhost:5432/v2e_test

.PHONY: help install lint format typecheck test test-integration test-all db-up migrate ingest serve \
        eval-retrieval eval-generation docker-build up down

help:
	@grep -E '^[a-z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

install: ## Install the package with development tools
	$(PYTHON) -m pip install -e ".[dev]"

lint: ## Static checks
	ruff check src tests
	ruff format --check src tests

format: ## Apply formatting
	ruff format src tests
	ruff check --fix src tests

typecheck: ## Type checks
	mypy src

test: ## Unit tests and repository style checks
	pytest tests/unit tests/test_repository_style.py -q

test-integration: ## PostgreSQL and real knowledge base tests (needs TEST_DB)
	V2E_TEST_DATABASE_URL=$(TEST_DB) pytest tests/integration -q

test-all: test test-integration ## Everything

db-up: ## Start PostgreSQL with pgvector
	docker compose up -d db

migrate: ## Apply database migrations
	v2e migrate

ingest: ## Ingest the knowledge base
	v2e ingest

serve: ## Run the API locally
	v2e serve --port 8000

eval-retrieval: ## Retrieval quality on the golden set
	v2e eval-retrieval

eval-generation: ## End to end generation scenarios (uses OpenRouter)
	v2e eval-generation

docker-build: ## Build the container image
	docker build -t vet2employ_rag:latest .

up: ## Start database and API, then ingest
	docker compose up -d db api
	docker compose run --rm ingest

down: ## Stop the stack
	docker compose down

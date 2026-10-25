# Cloud Control — developer tasks.
# Every target is a one-liner around a tool, so CI and humans run the same thing.

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
PORT ?= 8000

.DEFAULT_GOAL := help
.PHONY: help venv install run dev test cov lint format format-check typecheck check docker-build docker-run clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

install: venv ## Install runtime + dev dependencies
	$(BIN)/pip install -r requirements-dev.txt

run: ## Run the server (production-ish, no reload)
	$(BIN)/uvicorn app.main:app --host 0.0.0.0 --port $(PORT)

dev: ## Run the server with auto-reload
	$(BIN)/uvicorn app.main:app --reload --host 0.0.0.0 --port $(PORT)

test: ## Run the test suite
	$(BIN)/pytest

cov: ## Run tests with a coverage report
	$(BIN)/pytest --cov=app --cov-report=term-missing --cov-report=html

lint: ## Lint with ruff
	$(BIN)/ruff check app tests

format: ## Format with ruff
	$(BIN)/ruff format app tests
	$(BIN)/ruff check app tests --fix

format-check: ## Verify formatting without changing files
	$(BIN)/ruff format --check app tests

typecheck: ## Static type check
	$(BIN)/mypy app

check: lint format-check typecheck test ## Everything CI runs

docker-build: ## Build the container image
	docker build -t cloud-control-api:1.0.0 .

docker-run: ## Run the container
	docker run --rm -p $(PORT):8000 -e DASH_BOOTSTRAP_DASHBOARD=true cloud-control-api:1.0.0

clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage build dist *.egg-info

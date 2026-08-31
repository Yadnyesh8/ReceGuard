# Common tasks. Everything here is what CI runs, so a green `make check` means
# a green pipeline.

PY ?= .venv/bin/python
UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help setup lint format types test test-fast test-docker gates check build image clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Create the venv and install everything
	$(UV) venv
	VIRTUAL_ENV=.venv $(UV) pip install -e ".[dev,harness,mcp]"
	$(PY) -m pre_commit install || true

lint: ## Lint
	$(PY) -m ruff check raceguard tests

format: ## Format and autofix
	$(PY) -m ruff check raceguard tests --fix
	$(PY) -m ruff format raceguard tests

types: ## Type check
	$(PY) -m mypy raceguard

test-fast: ## Tests that need no Docker
	$(PY) -m pytest tests -q -m "not docker"

test-docker: ## The full pipeline against a real PostgreSQL
	$(PY) -m pytest tests -q -m docker

test: ## Everything
	$(PY) -m pytest tests -q

gates: ## The scanner quality gates from section 17.2
	$(PY) tests/eval_corpus.py

check: lint types test gates ## Everything CI runs

build: ## Build the wheel and sdist, and prove the wheel installs
	$(UV) build
	$(UV) venv -q /tmp/raceguard-verify
	VIRTUAL_ENV=/tmp/raceguard-verify $(UV) pip install -q dist/*.whl
	/tmp/raceguard-verify/bin/raceguard version

image: ## Build the tool's own container image
	docker build -t raceguard:local .
	docker run --rm raceguard:local version

clean: ## Remove build output, caches, and orphaned containers
	rm -rf dist build .coverage coverage.xml htmlcov .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	$(PY) -m raceguard.cli clean --runs || true

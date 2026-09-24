# Development entry points. Every target works on a clean clone.
PY := .venv/bin/python
UV := uv

.PHONY: install lint fmt typecheck test check serve dev build-ui gen-client changelog-draft backup reset chrome clean help

install:            ## Create .venv and install the package with dev extras (from uv.lock)
	$(UV) sync --all-extras

lint:               ## Ruff (lint + format check)
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:                ## Ruff format in place
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

typecheck:          ## mypy --strict
	$(PY) -m mypy

test:               ## Offline test suite
	$(PY) -m pytest -q

check: lint typecheck test   ## Everything CI runs on the Python side

serve:              ## Run the backend (serves the built frontend if present)
	$(PY) -m netkeeper.cli serve

dev:                ## Backend with reload; run `pnpm dev` in frontend/ alongside
	$(PY) -m netkeeper.cli serve --reload

build-ui:           ## Production frontend build into frontend/dist
	cd frontend && pnpm install --frozen-lockfile && pnpm build

gen-client:         ## Export the OpenAPI schema and regenerate the TypeScript client
	$(PY) -m netkeeper.cli openapi export --out frontend/openapi.json
	cd frontend && pnpm gen

changelog-draft:    ## Preview the unreleased changelog assembled from changelog.d/
	$(PY) -m towncrier build --draft --version unreleased

backup:             ## Snapshot the database into the data directory's backups/
	$(PY) -m netkeeper.cli backup

reset:              ## Archive the database, then delete it so `make serve` starts clean
	scripts/reset-data.sh

chrome:             ## Start the netkeeper Chrome profile with its debugging port (by hand)
	scripts/chrome.sh

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache frontend/dist

help:               ## This list
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

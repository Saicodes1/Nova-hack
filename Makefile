.PHONY: setup test test-live dev flight lint check
# Use the project venv when it exists, so targets work without activating it.
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

setup:
	test -x .venv/bin/python || python3 -m venv .venv
	.venv/bin/python -m pip install -e ".[dev]"
	npm --prefix web ci
test:
	$(PYTHON) -m pytest -q
	npm --prefix web run typecheck
test-live:
	$(PYTHON) -m pytest -q -m live
dev:
	npm --prefix web run build
	$(PYTHON) -m uvicorn ground.main:app --port 8000
flight:
	$(PYTHON) -m uvicorn node.flight_app:app --port 8100
lint:
	$(PYTHON) -m ruff check .
	npm --prefix web run typecheck
check:
	$(PYTHON) -m pytest -q
	$(PYTHON) -m ruff check .
	npm --prefix web run build

.PHONY: setup test test-live dev flight lint
PYTHON ?= python
setup:
	$(PYTHON) -m pip install -e ".[dev]"
	cd web && npm ci
test:
	$(PYTHON) -m pytest -q
	cd web && npm run typecheck
test-live:
	$(PYTHON) -m pytest -q -m live
dev:
	cd web && npm run build
	$(PYTHON) -m uvicorn ground.main:app --port 8000
flight:
	$(PYTHON) -m uvicorn node.flight_app:app --port 8100
lint:
	$(PYTHON) -m ruff check .
	cd web && npm run typecheck

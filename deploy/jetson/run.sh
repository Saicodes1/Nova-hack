#!/usr/bin/env bash
# Start the flight node on port 8100, reachable from the ground station laptop.
set -euo pipefail
cd "$(dirname "$0")/../.."
export VERDICT_MODE="${VERDICT_MODE:-ollama}"
exec .venv/bin/python -m uvicorn node.flight_app:app --host 0.0.0.0 --port 8100

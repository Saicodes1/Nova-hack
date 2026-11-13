#!/usr/bin/env bash
# Stand-in for the Jetson flight node, on any machine.
#
# Runs the same node code on port 8100 with simulated power and temperature
# (reported as "power_source":"simulated"). Analyses are real: same engine,
# same imagery. The verdict comes from a local Ollama model when one is
# running here, otherwise from the template sentence.
#
#   deploy/sim/run.sh            # then point the ground station at it:
#   NODE_MODE=remote NODE_URL=http://127.0.0.1:8100
set -euo pipefail
cd "$(dirname "$0")/../.."
export NODE_SIM=jetson
if [ -z "${VERDICT_MODE:-}" ]; then
  if curl -fsS -m 2 "${OLLAMA_URL:-http://127.0.0.1:11434}/api/version" >/dev/null 2>&1; then
    export VERDICT_MODE=ollama
  else
    export VERDICT_MODE=template
  fi
fi
PYTHON=python
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python
echo "Simulated flight node on ${HOST:-127.0.0.1}:${PORT:-8100} (verdict: $VERDICT_MODE)"
exec "$PYTHON" -m uvicorn node.flight_app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8100}"

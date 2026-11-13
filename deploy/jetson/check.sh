#!/usr/bin/env bash
# Smoke test against a running flight node. Usage: check.sh [http://host:8100]
set -euo pipefail
cd "$(dirname "$0")/../.."
NODE="${1:-http://127.0.0.1:8100}"
echo "== health";    curl -fsS "$NODE/health";    echo
echo "== telemetry"; curl -fsS "$NODE/telemetry"; echo
echo "== offline analysis (synthetic imagery, no internet needed)"
.venv/bin/python scripts/node_check.py "$NODE" --demo dubai_flood
echo "== live analysis (needs internet on the node)"
.venv/bin/python scripts/node_check.py "$NODE"

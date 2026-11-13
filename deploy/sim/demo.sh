#!/usr/bin/env bash
# The whole demo on one machine, with no Jetson.
#
# Starts, in order: the Ollama model server (if it is not already running),
# the simulated flight node, and the ground station pointed at that node.
# The analyses and the model-written verdict are real. Power and temperature
# are simulated and are labelled "simulated" on screen.
#
#   deploy/sim/demo.sh                 # ground station on 8000, node on 8100
#   GROUND_PORT=8010 NODE_PORT=8102 deploy/sim/demo.sh
#
# Needs Ollama (https://ollama.com/download) with the model pulled once:
#   ollama pull llama3.2:3b
set -euo pipefail
cd "$(dirname "$0")/../.."
MODEL="${OLLAMA_MODEL:-llama3.2:3b}"
NODE_PORT="${NODE_PORT:-8100}"
GROUND_PORT="${GROUND_PORT:-8000}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
PYTHON=python
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python
OLLAMA="$(command -v ollama || true)"
[ -z "$OLLAMA" ] && [ -x "$HOME/.local/ollama/bin/ollama" ] && OLLAMA="$HOME/.local/ollama/bin/ollama"
mkdir -p var

if ! curl -fsS -m 2 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
  [ -n "$OLLAMA" ] || { echo "Ollama is not installed. Without it the verdict uses the template sentence."; }
  if [ -n "$OLLAMA" ]; then
    echo "Starting the model server…"
    nohup "$OLLAMA" serve > var/ollama.log 2>&1 &
    for _ in $(seq 1 20); do curl -fsS -m 1 "$OLLAMA_URL/api/version" >/dev/null 2>&1 && break; sleep 1; done
  fi
fi
if [ -n "$OLLAMA" ] && ! "$OLLAMA" list 2>/dev/null | grep -q "^$MODEL"; then
  echo "Downloading $MODEL (about 2 GB, once)…"
  "$OLLAMA" pull "$MODEL"
fi

echo "Starting the simulated flight node on port $NODE_PORT…"
PORT="$NODE_PORT" OLLAMA_URL="$OLLAMA_URL" OLLAMA_MODEL="$MODEL" nohup deploy/sim/run.sh > var/sim-node.log 2>&1 &
node=$!
trap 'kill $node 2>/dev/null' EXIT
for _ in $(seq 1 120); do
  state="$(curl -fsS -m 2 "http://127.0.0.1:$NODE_PORT/health" 2>/dev/null || true)"
  case "$state" in
    *'"verdict_writer":"template"'*) echo "Node ready (template verdict; no model server found)."; break ;;
    *'"verdict_model_ready":true'*) echo "Node ready, model loaded."; break ;;
  esac
  sleep 2
done

echo "Ground station: http://127.0.0.1:$GROUND_PORT"
NODE_MODE=remote NODE_URL="http://127.0.0.1:$NODE_PORT" \
  "$PYTHON" -m uvicorn ground.main:app --host 127.0.0.1 --port "$GROUND_PORT"

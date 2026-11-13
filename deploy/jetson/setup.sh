#!/usr/bin/env bash
# One-time setup of the flight node on a Jetson Orin Nano (JetPack 6, Ubuntu 22.04).
# Needs Python 3.11+. Uses the system Python when it is new enough, otherwise uv.
set -euo pipefail
cd "$(dirname "$0")/../.."

if python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' && python3 -m venv .venv 2>/dev/null; then
  # The system Python is new enough (JetPack 7 ships 3.12).
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -e ".[dev]"
else
  # Older JetPack (Python 3.10) or no python3-venv: let uv provide Python 3.12.
  if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
  rm -rf .venv  # a failed "python3 -m venv" can leave a partial folder
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -e ".[dev]"
fi

if ! command -v ollama >/dev/null; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
ollama pull "${OLLAMA_MODEL:-llama3.2:3b}"

[ -f .env ] || cp .env.example .env
grep -q '^VERDICT_MODE=ollama' .env || sed -i 's/^VERDICT_MODE=.*/VERDICT_MODE=ollama/' .env
echo "Setup complete. Start the node with deploy/jetson/run.sh"

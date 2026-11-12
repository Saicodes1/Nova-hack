#!/bin/bash
# Starts the ground station on http://127.0.0.1:8000 and writes logs to var/logs/.
cd "$(dirname "$0")"
mkdir -p var/logs
lsof -ti tcp:8000 | xargs kill 2>/dev/null
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
( npm --prefix web run build ) > var/logs/build.log 2>&1
echo "build exit $?" >> var/logs/build.log
.venv/bin/python -m uvicorn ground.main:app --host 127.0.0.1 --port 8000 > var/logs/server.log 2>&1

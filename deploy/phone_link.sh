#!/usr/bin/env bash
# Open a public address for the ground station so phones can reach it, write
# it to .env as PUBLIC_URL, and restart the ground station so the QR code
# shows it. The address changes every time this runs. Stop it with Ctrl+C.
# Needs cloudflared: https://github.com/cloudflare/cloudflared/releases
set -euo pipefail
cd "$(dirname "$0")/.."
port="${1:-8000}"
log="$(mktemp)"
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:${port}" >"$log" 2>&1 &
tunnel=$!
trap 'kill $tunnel 2>/dev/null' EXIT
for _ in $(seq 1 40); do
  url="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$log" | head -1 || true)"
  [ -n "$url" ] && break
  sleep 1
done
[ -n "${url:-}" ] || { echo "No tunnel address. See $log"; exit 1; }
if grep -q '^PUBLIC_URL=' .env 2>/dev/null; then
  sed -i "s#^PUBLIC_URL=.*#PUBLIC_URL=${url}#" .env
else
  echo "PUBLIC_URL=${url}" >> .env
fi
pid="$(ss -ltnp 2>/dev/null | grep ":${port} " | grep -o 'pid=[0-9]*' | cut -d= -f2 | head -1 || true)"
[ -n "$pid" ] && kill "$pid" && sleep 1
nohup .venv/bin/uvicorn ground.main:app --host 127.0.0.1 --port "$port" > var/ground.log 2>&1 &
echo "Phones:  ${url}/?ask"
echo "Screen:  http://127.0.0.1:${port}  (press Phone QR)"
wait $tunnel

#!/usr/bin/env bash
# Log node power, temperature and memory once a second until killed.
# Usage: scripts/sample_power.sh out.csv [http://127.0.0.1:8100]
# Columns: epoch, power_w, temp_c, node_rss_mb, system_used_mb, state
set -u
OUT="$1"
NODE="${2:-http://127.0.0.1:8100}"
echo "epoch,power_w,temp_c,node_rss_mb,system_used_mb,state" > "$OUT"
while true; do
  t=$(curl -fsS -m 2 "$NODE/telemetry" 2>/dev/null) || { sleep 1; continue; }
  used=$(free -m | awk '/^Mem:/ {print $3}')
  echo "$t" | python3 -c "
import json, sys, time
t = json.load(sys.stdin)
print(int(time.time()), t['power_w'], t['temp_c'], t['mem_used_mb'], $used, t['state'], sep=',')
" >> "$OUT"
  sleep 1
done

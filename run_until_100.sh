#!/bin/bash
# Register until accounts.json reaches TARGET. Resilient: recomputes need each batch,
# restarts bot if it exits early (crash / server flakiness). Run detached under screen.
cd /home/hermes/garapan/anu-regis/edel-regis
TARGET=100
count_accts() { python3 -c "import json;print(len(json.load(open('accounts.json'))))" 2>/dev/null || echo 0; }

echo "[start] $(date) target=$TARGET"
while true; do
  count=$(count_accts)
  if [ "$count" -ge "$TARGET" ]; then
    echo "[TARGET REACHED] $count/$TARGET at $(date)"
    break
  fi
  need=$((TARGET - count))
  echo "=== batch $(date): have $count, need $need more ==="
  PYTHONUNBUFFERED=1 python3 edel_bot.py "$need"
  echo "--- batch ended, sleeping 5s ---"
  sleep 5
done
echo "[ALL DONE] $(count_accts) accounts at $(date)"

#!/bin/bash
# Launched every minute by the com.intraday.liveloop LaunchAgent. One cycle per
# run, so a sleeping laptop simply misses cycles instead of leaving a dead
# process behind; outside US market hours it writes a heartbeat and exits.
set -euo pipefail

HOME_DIR="${INTRADAY_HOME:-$HOME/.intraday}"
LOG_DIR="$HOME_DIR/logs"
LOG="$LOG_DIR/liveloop.log"
MAX_BYTES=$((5 * 1024 * 1024))
KEEP=3

mkdir -p "$LOG_DIR"

rotate() {
  local size
  size=$(wc -c <"$LOG" 2>/dev/null || echo 0)
  if [ "$size" -lt "$MAX_BYTES" ]; then
    return
  fi
  local i
  for ((i = KEEP - 1; i >= 1; i--)); do
    [ -f "$LOG.$i" ] && mv -f "$LOG.$i" "$LOG.$((i + 1))"
  done
  mv -f "$LOG" "$LOG.1"
}

rotate
exec >>"$LOG" 2>&1

cd "$HOME_DIR/app"
exec "$HOME_DIR/venv/bin/python" scripts/live_loop.py --once --exit-when-closed

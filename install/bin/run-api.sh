#!/bin/bash
# Launched by the com.intraday.dashboard LaunchAgent at login and whenever it
# crashes. Rotates its own log first, so an always-on service can never fill
# the disk.
set -euo pipefail

HOME_DIR="${INTRADAY_HOME:-$HOME/.intraday}"
LOG_DIR="$HOME_DIR/logs"
LOG="$LOG_DIR/dashboard.log"
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

echo "--- $(date '+%Y-%m-%d %H:%M:%S') starting dashboard ---"
cd "$HOME_DIR/app"
exec "$HOME_DIR/venv/bin/python" -m uvicorn intraday.web.app:app \
  --host 127.0.0.1 --port "${INTRADAY_PORT:-8000}"

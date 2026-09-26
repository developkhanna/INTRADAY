#!/bin/bash
# Double-click to open the dashboard. If the background service is not running
# for some reason, this nudges macOS to start it and then waits for it.
set -euo pipefail

PORT="${INTRADAY_PORT:-8000}"
AGENT="$HOME/Library/LaunchAgents/com.intraday.dashboard.plist"

if ! curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
  printf 'Starting the dashboard…\n'
  launchctl kickstart "gui/$UID/com.intraday.dashboard" >/dev/null 2>&1 \
    || launchctl load -w "$AGENT" >/dev/null 2>&1 \
    || true
  for _ in $(seq 1 30); do
    curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1 && break
    sleep 1
  done
fi

if curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
  open "http://localhost:$PORT/"
  exit 0
fi

printf '\nThe dashboard is not answering on port %s.\n' "$PORT"
printf 'Run "Install Intraday.command" again, or look at ~/.intraday/logs/dashboard.log.\n\n'
exit 1

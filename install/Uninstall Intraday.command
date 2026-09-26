#!/bin/bash
# Double-click to stop the Intraday dashboard and remove it from login.
# Your data in ~/.intraday is kept unless you say otherwise at the prompt.
set -euo pipefail

INTRADAY_HOME="$HOME/.intraday"
AGENT_DIR="$HOME/Library/LaunchAgents"

printf '\n============================================\n'
printf '  Removing the Intraday background services\n'
printf '============================================\n\n'

for label in com.intraday.dashboard com.intraday.liveloop; do
  launchctl bootout "gui/$UID/$label" >/dev/null 2>&1 || true
  launchctl unload "$AGENT_DIR/$label.plist" >/dev/null 2>&1 || true
  rm -f "$AGENT_DIR/$label.plist"
  printf '   Stopped and unregistered %s\n' "$label"
done

printf '\nThe dashboard will no longer start when you log in.\n'
printf '\nYour data folder is %s. It holds every downloaded price bar and the\n' "$INTRADAY_HOME"
printf 'whole record of past predictions. Deleting it cannot be undone.\n\n'
read -r -p 'Delete that folder too? Type DELETE to confirm, or press Return to keep it: ' answer

if [ "$answer" = "DELETE" ]; then
  rm -rf "$INTRADAY_HOME"
  printf '\nDeleted %s.\n' "$INTRADAY_HOME"
else
  printf '\nKept %s. Re-running the installer will pick it straight back up.\n' "$INTRADAY_HOME"
fi

printf '\nYou can close this window now.\n\n'

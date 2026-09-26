#!/bin/bash
# Double-click installer for the Intraday dashboard on macOS.
#
# Installs Python if it is missing, copies the app into ~/.intraday/app, makes
# a private virtual environment, registers two background services with macOS
# (the dashboard and the once-a-minute live loop), and opens the dashboard.
#
# Safe to run as many times as you like: every step checks first and skips
# work that is already done.
set -euo pipefail

INTRADAY_HOME="$HOME/.intraday"
APP_DIR="$INTRADAY_HOME/app"
VENV="$INTRADAY_HOME/venv"
LOG_DIR="$INTRADAY_HOME/logs"
AGENT_DIR="$HOME/Library/LaunchAgents"
REPO_URL="${INTRADAY_REPO_URL:-https://github.com/developkhanna/INTRADAY.git}"
REPO_TARBALL="${INTRADAY_REPO_TARBALL:-https://codeload.github.com/developkhanna/INTRADAY/tar.gz/refs/heads/main}"
PORT="${INTRADAY_PORT:-8000}"
PYTHON_PKG_URL="https://www.python.org/ftp/python/3.12.7/python-3.12.7-macos11.pkg"
MIN_MINOR=10

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
fail() {
  printf '\n\033[31m%s\033[0m\n' "$*"
  printf 'Nothing was broken. You can close this window and send a screenshot of it.\n'
  exit 1
}

printf '\n============================================\n'
printf '  Intraday dashboard — one-time installation\n'
printf '============================================\n'
info "This window is just showing you progress. You can close it when it says so."

# --- 1. Python ---------------------------------------------------------------

python_ok() {
  local candidate="$1"
  [ -x "$candidate" ] || command -v "$candidate" >/dev/null 2>&1 || return 1
  "$candidate" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, $MIN_MINOR) else 1)" \
    >/dev/null 2>&1
}

find_python() {
  local candidate
  for candidate in \
    /opt/homebrew/bin/python3 /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
    python3; do
    if python_ok "$candidate"; then
      command -v "$candidate" 2>/dev/null || echo "$candidate"
      return 0
    fi
  done
  return 1
}

install_python() {
  if command -v brew >/dev/null 2>&1; then
    info "Installing Python with Homebrew. This takes a few minutes."
    brew install python@3.12 >/dev/null || fail "Homebrew could not install Python."
    return
  fi
  info "Downloading the official Python installer from python.org (about 60 MB)."
  local pkg="/tmp/intraday-python.pkg"
  curl -fsSL "$PYTHON_PKG_URL" -o "$pkg" || fail "Could not download Python."
  printf '\n'
  info "macOS needs your Mac login password to install Python."
  info "Type it below — nothing is shown while you type, that is normal."
  sudo installer -pkg "$pkg" -target / >/dev/null || fail "The Python installer did not finish."
  rm -f "$pkg"
}

say "Step 1 of 5 — Checking Python…"
if PYTHON="$(find_python)"; then
  info "Found $("$PYTHON" --version 2>&1)."
else
  info "Python 3.$MIN_MINOR or newer is not installed yet."
  install_python
  PYTHON="$(find_python)" || fail "Python still is not available after installing it."
  info "Now using $("$PYTHON" --version 2>&1)."
fi

# --- 2. The app itself --------------------------------------------------------

say "Step 2 of 5 — Getting the latest version of the app…"
mkdir -p "$INTRADAY_HOME" "$LOG_DIR"

if command -v git >/dev/null 2>&1; then
  if [ -d "$APP_DIR/.git" ]; then
    info "Updating the copy in $APP_DIR."
    git -C "$APP_DIR" fetch --quiet origin && git -C "$APP_DIR" reset --quiet --hard origin/main
  else
    info "Downloading into $APP_DIR."
    rm -rf "$APP_DIR"
    git clone --quiet "$REPO_URL" "$APP_DIR" || fail "Could not download the app."
  fi
else
  # No git (no Xcode command line tools): a plain tarball works just as well.
  info "Downloading the app as an archive."
  tmp="$(mktemp -d)"
  curl -fsSL "$REPO_TARBALL" -o "$tmp/app.tar.gz" || fail "Could not download the app."
  tar -xzf "$tmp/app.tar.gz" -C "$tmp"
  rm -rf "$APP_DIR"
  mkdir -p "$APP_DIR"
  cp -R "$tmp"/INTRADAY-*/. "$APP_DIR"/
  rm -rf "$tmp"
fi

# --- 3. Private Python environment -------------------------------------------

say "Step 3 of 5 — Installing the app's own Python packages…"
info "A few hundred megabytes of maths libraries. Two to five minutes."
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV" || fail "Could not create the Python environment."
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install --quiet -e "$APP_DIR" \
  || fail "Could not install the app's packages."
info "Done."

# --- 4. Background services ---------------------------------------------------

say "Step 4 of 5 — Making it start by itself every time you log in…"
mkdir -p "$AGENT_DIR"
chmod +x "$APP_DIR/install/bin/run-api.sh" "$APP_DIR/install/bin/run-live-loop.sh"

write_agent() {
  local label="$1" script="$2" extra="$3"
  cat >"$AGENT_DIR/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$script</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>INTRADAY_HOME</key><string>$INTRADAY_HOME</string>
    <key>INTRADAY_PORT</key><string>$PORT</string>
  </dict>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>StandardOutPath</key><string>$LOG_DIR/$label.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/$label.err.log</string>
  <key>ProcessType</key><string>Background</string>
$extra
</dict>
</plist>
PLIST
}

# The dashboard: always on, restarted if it ever exits.
write_agent "com.intraday.dashboard" "$APP_DIR/install/bin/run-api.sh" \
  "  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>"

# The live loop: one short run a minute. launchd restarts the timer after
# sleep, and each run exits immediately when the US market is closed.
write_agent "com.intraday.liveloop" "$APP_DIR/install/bin/run-live-loop.sh" \
  "  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>60</integer>
  <key>KeepAlive</key><false/>"

load_agent() {
  local label="$1"
  launchctl bootout "gui/$UID/$label" >/dev/null 2>&1 || true
  if ! launchctl bootstrap "gui/$UID" "$AGENT_DIR/$label.plist" >/dev/null 2>&1; then
    # Older macOS releases only understand the load/unload spelling.
    launchctl unload "$AGENT_DIR/$label.plist" >/dev/null 2>&1 || true
    launchctl load -w "$AGENT_DIR/$label.plist" >/dev/null 2>&1 \
      || fail "macOS refused to register the background service $label."
  fi
}

load_agent "com.intraday.dashboard"
load_agent "com.intraday.liveloop"
info "Registered. Logs go to $LOG_DIR and are trimmed automatically."

# --- 5. Open it ---------------------------------------------------------------

say "Step 5 of 5 — Starting the dashboard…"
ready=""
for _ in $(seq 1 60); do
  if curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
    ready="yes"
    break
  fi
  sleep 1
done

if [ -z "$ready" ]; then
  fail "The dashboard did not answer on port $PORT. Check $LOG_DIR/dashboard.log."
fi

open "http://localhost:$PORT/" >/dev/null 2>&1 || true

cat <<DONE

============================================
  Installed.
============================================

A browser tab should have opened at http://localhost:$PORT

  • If it asks for Alpaca keys, follow the three steps on that page. That is the
    only thing the app needs from you.
  • After you save the keys it starts downloading three years of market history
    in the background. That takes roughly 40 minutes on a normal connection, and
    the page shows you how far along it is. You can close the laptop; it picks up
    where it left off.
  • From now on the dashboard starts on its own every time you log in. Just open
    http://localhost:$PORT — bookmark it, or use "Open Intraday" in this folder.

You can close this window now.

DONE

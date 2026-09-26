"""Keep the Mac awake during US market hours, under an explicit toggle.

A sleeping laptop records no predictions. macOS ships `caffeinate`, which
holds a "do not idle-sleep" assertion for as long as it runs, so the engine
runs one while the US session is open and the owner has left the switch on.

The switch is the owner's, not ours: turning it off kills the process on the
spot rather than at the next login, and the state is persisted so the choice
survives a restart. On anything that is not macOS the feature reports itself
as unavailable instead of pretending.
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
import subprocess
import threading

from intraday.config import DATA_DIR, ensure_dirs
from intraday.health import market_is_open

logger = logging.getLogger(__name__)

PREFS_FILE = DATA_DIR / "preferences.json"
CHECK_SECONDS = 30

LABEL = (
    "Keep this Mac awake during market hours so predictions keep recording "
    "(17:30–00:00 Abu Dhabi, 09:30–16:00 New York)"
)


def supported() -> bool:
    return platform.system() == "Darwin" and shutil.which("caffeinate") is not None


def load_preferences() -> dict:
    if not PREFS_FILE.exists():
        return {}
    try:
        return json.loads(PREFS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_preferences(values: dict) -> dict:
    ensure_dirs()
    merged = {**load_preferences(), **values}
    PREFS_FILE.write_text(json.dumps(merged, indent=2))
    return merged


def keep_awake_enabled() -> bool:
    """Default on: the common case is wanting the engine to keep running."""
    return bool(load_preferences().get("keep_awake", True))


class AwakeKeeper:
    """Owns at most one `caffeinate` child process."""

    def __init__(self, command: list[str] | None = None):
        # -i: no idle sleep. Not -d: the screen may still turn off.
        self._command = command or ["caffeinate", "-i"]
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start_process(self) -> bool:
        with self._lock:
            if self.running():
                return True
            try:
                self._process = subprocess.Popen(  # noqa: S603
                    self._command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, ValueError) as exc:
                logger.warning("could not start caffeinate: %s", exc)
                self._process = None
                return False
            return True

    def stop_process(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def reconcile(self) -> bool:
        """Run caffeinate exactly when it is wanted. Returns whether it is running."""
        wanted = supported() and keep_awake_enabled() and market_is_open()
        if wanted:
            self.start_process()
        else:
            self.stop_process()
        return self.running()

    def set_enabled(self, enabled: bool) -> dict:
        """Toggle. Turning it off stops the process immediately."""
        save_preferences({"keep_awake": bool(enabled)})
        self.reconcile()
        return self.status()

    def status(self) -> dict:
        enabled = keep_awake_enabled()
        is_supported = supported()
        if not is_supported:
            text = "Only available on a Mac. This machine cannot be kept awake by the app."
        elif not enabled:
            text = "Off. If the Mac sleeps during market hours, nothing is recorded."
        elif self.running():
            text = "On, and holding this Mac awake right now (the market is open)."
        else:
            text = "On. It will hold the Mac awake when the US market opens."
        return {
            "enabled": enabled,
            "supported": is_supported,
            "active": self.running(),
            "label": LABEL,
            "text": text,
        }

    def supervise(self) -> None:
        """Background loop: pick up market open/close without a restart."""
        if self._thread and self._thread.is_alive():
            return

        def loop() -> None:
            while not self._stop.wait(CHECK_SECONDS):
                try:
                    self.reconcile()
                except Exception:  # never take the API down
                    logger.exception("caffeinate supervisor failed")

        self.reconcile()
        self._thread = threading.Thread(target=loop, name="intraday-awake", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self.stop_process()


_keeper: AwakeKeeper | None = None


def keeper() -> AwakeKeeper:
    global _keeper
    if _keeper is None:
        _keeper = AwakeKeeper()
    return _keeper

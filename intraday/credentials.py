"""Alpaca credential storage for a machine where nobody edits shell profiles.

Load order is environment variables first, then `~/.intraday/credentials.json`
written by the setup page. The file is created with mode 0600 and is never
logged, echoed back to the browser, or committed.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from intraday.config import DATA_DIR

CREDENTIALS_FILE = DATA_DIR / "credentials.json"


@dataclass(frozen=True)
class Credentials:
    key_id: str
    secret_key: str
    source: str  # "environment" or "file"


def load_credentials(path: Path | None = None) -> Credentials | None:
    """Environment first, then the file the setup page writes. None if absent."""
    key_id = os.environ.get("ALPACA_API_KEY_ID", "").strip()
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if key_id and secret_key:
        return Credentials(key_id=key_id, secret_key=secret_key, source="environment")

    path = path or CREDENTIALS_FILE
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    key_id = str(payload.get("key_id", "")).strip()
    secret_key = str(payload.get("secret_key", "")).strip()
    if not key_id or not secret_key:
        return None
    return Credentials(key_id=key_id, secret_key=secret_key, source="file")


def save_credentials(key_id: str, secret_key: str, path: Path | None = None) -> Path:
    """Write the keys readable only by the owner."""
    path = path or CREDENTIALS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"key_id": key_id.strip(), "secret_key": secret_key.strip()}, indent=2)
    # Create with 0600 before any bytes land on disk, so the secret is never
    # world-readable, not even for the instant between write and chmod.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(payload)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def credentials_present(path: Path | None = None) -> bool:
    return load_credentials(path) is not None


def verify_credentials(key_id: str, secret_key: str) -> tuple[bool, str]:
    """Ask Alpaca whether these keys work. Returns (ok, message), never the secret."""
    from intraday.config import AlpacaConfig
    from intraday.data.alpaca import AlpacaClient

    client = AlpacaClient(AlpacaConfig(key_id=key_id.strip(), secret_key=secret_key.strip()))
    try:
        account = client.account()
    except Exception as exc:  # network failure, 401, 403 — all the same to the user
        detail = str(exc)
        if "401" in detail or "403" in detail:
            return False, "Alpaca rejected those keys. Check you copied both in full."
        return False, f"Could not reach Alpaca to check the keys ({type(exc).__name__})."
    status = str(account.get("status", "")).upper()
    if status and status not in ("ACTIVE", "PAPER_ONLY"):
        return True, f"Keys work. Alpaca reports the account status as {status}."
    return True, "Keys work. Alpaca accepted them."


def mask(key_id: str) -> str:
    """A key id fragment safe to display: never the secret, never the full id."""
    key_id = key_id.strip()
    if len(key_id) <= 4:
        return "••••"
    return f"{key_id[:4]}{'•' * 6}{key_id[-2:]}"

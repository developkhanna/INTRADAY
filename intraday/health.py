"""Plain-English system status for the dashboard and `/healthz`.

Answers the five questions the owner actually has: is the app running, are my
keys in place, is the live loop ticking, how fresh is the data, and did we
record anything today. Anything stale says so out loud — silently showing old
numbers is the failure mode this exists to prevent.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from intraday.config import DATA_DIR, MARKET_TZ, SESSION_CLOSE_MIN, SESSION_OPEN_MIN
from intraday.credentials import load_credentials, mask

HEARTBEAT_FILE = DATA_DIR / "live_loop.json"

# Bars arrive every minute during the session; the free SIP feed is embargoed
# ~15 minutes, so anything older than half an hour is genuinely behind.
STALE_BARS_MINUTES = 30
STALE_TICK_MINUTES = 5


def record_live_tick(status: str, predictions: int = 0, path: Path | None = None) -> None:
    """Called by the live loop each cycle so the dashboard can prove it is alive."""
    path = path or HEARTBEAT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": status,
        "predictions": predictions,
    }
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2))
    temp.replace(path)


def read_live_tick(path: Path | None = None) -> dict | None:
    path = path or HEARTBEAT_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def market_is_open(now: dt.datetime | None = None) -> bool:
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return SESSION_OPEN_MIN <= minutes < SESSION_CLOSE_MIN


def _minutes_since(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        moment = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - moment).total_seconds() / 60


def _ago(minutes: float | None) -> str:
    if minutes is None:
        return "never"
    if minutes < 2:
        return "just now"
    if minutes < 90:
        return f"{int(minutes)} minutes ago"
    hours = minutes / 60
    if hours < 36:
        return f"{hours:.0f} hours ago"
    return f"{hours / 24:.0f} days ago"


def snapshot(last_bar: dt.datetime | None = None, predictions_today: int | None = None) -> dict:
    """One dictionary describing the whole system, each item in plain English."""
    credentials = load_credentials()
    tick = read_live_tick()
    tick_minutes = _minutes_since(tick.get("at")) if tick else None
    bar_minutes = _minutes_since(last_bar.isoformat()) if last_bar is not None else None
    open_now = market_is_open()

    items = [
        {
            "name": "Dashboard",
            "ok": True,
            "text": "Running. This page is being served by the app on your Mac.",
        },
        {
            "name": "Alpaca keys",
            "ok": credentials is not None,
            "text": (
                f"Saved ({mask(credentials.key_id)}, from the {credentials.source})."
                if credentials
                else "Missing. Open the setup page and paste your Alpaca keys."
            ),
        },
        {
            "name": "Live loop",
            "ok": (tick_minutes is not None and tick_minutes <= STALE_TICK_MINUTES)
            or not open_now,
            "text": (
                f"Last checked the market {_ago(tick_minutes)}"
                + (f" ({tick.get('status')})." if tick else ".")
                if tick
                else "Has not run yet."
            ),
        },
        {
            "name": "Market data",
            "ok": bar_minutes is None or not open_now or bar_minutes <= STALE_BARS_MINUTES,
            "text": (
                f"Newest stored 1-minute bar is from {_ago(bar_minutes)}."
                if bar_minutes is not None
                else "No bars downloaded yet."
            ),
        },
        {
            "name": "Predictions today",
            "ok": True,
            "text": (
                f"{predictions_today} recorded so far today."
                if predictions_today is not None
                else "Not counted yet."
            ),
        },
    ]

    stale = open_now and bar_minutes is not None and bar_minutes > STALE_BARS_MINUTES
    warnings = []
    if stale:
        warnings.append(
            "The market is open but the newest data on this Mac is "
            f"{_ago(bar_minutes)}. Your laptop was probably asleep — the numbers "
            "below are old until it catches up."
        )
    if credentials is None:
        warnings.append("No Alpaca keys yet, so nothing new can be downloaded.")
    if open_now and tick_minutes is not None and tick_minutes > STALE_TICK_MINUTES:
        warnings.append(
            f"The live loop last ran {_ago(tick_minutes)}, during market hours. "
            "It should run every minute."
        )

    return {
        "items": items,
        "warnings": warnings,
        "market_open": open_now,
        "stale": stale,
        "credentials_ok": credentials is not None,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

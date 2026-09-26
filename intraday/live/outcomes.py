"""Attach realized outcomes to recorded predictions."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from intraday.data.store import BarStore
from intraday.live.ledger import Ledger

logger = logging.getLogger(__name__)


def attach_outcomes(
    ledger: Ledger | None = None,
    store: BarStore | None = None,
    now: pd.Timestamp | None = None,
) -> int:
    """Score every prediction whose horizon has passed, using stored bars."""
    ledger = ledger or Ledger()
    store = store or BarStore()
    now = now or pd.Timestamp.utcnow().tz_localize("UTC")

    pending = ledger.pending(now)
    if pending.empty:
        return 0

    rows = []
    for symbol, group in pending.groupby("symbol"):
        start = group["bar_timestamp"].min().date()
        bars = store.read([symbol], start=start)
        if bars.empty:
            continue
        bars = bars.sort_values("timestamp").reset_index(drop=True)
        timestamps = bars["timestamp"].to_numpy()

        for row in group.itertuples():
            entry_position = np.searchsorted(timestamps, np.datetime64(row.bar_timestamp))
            if entry_position >= len(bars):
                continue
            window = bars.iloc[entry_position + 1 : entry_position + 1 + row.horizon_minutes]
            if len(window) < row.horizon_minutes:
                continue  # not enough bars yet (halt, or session ended)

            entry = float(bars["close"].iloc[entry_position])
            high = float(window["high"].max())
            low = float(window["low"].min())
            realized_return = float(window["close"].iloc[-1]) / entry - 1

            target_before_stop = None
            if pd.notna(row.target_pct) and pd.notna(row.stop_pct):
                target_before_stop = _first_touch(
                    window, entry * (1 + row.target_pct), entry * (1 - row.stop_pct)
                )

            rows.append(
                {
                    "prediction_id": row.prediction_id,
                    "resolved_at": now,
                    "realized_return": realized_return,
                    "realized_mfe": high / entry - 1,
                    "realized_mae": low / entry - 1,
                    "target_before_stop": target_before_stop,
                    "minutes_to_resolution": int(row.horizon_minutes),
                }
            )

    if not rows:
        return 0
    written = ledger.attach_outcome(pd.DataFrame(rows))
    logger.info("attached %d outcomes", written)
    return written


def _first_touch(window: pd.DataFrame, target: float, stop: float) -> int:
    """1 if the target is touched first, 0 if the stop is (ties count as a stop)."""
    for bar in window.itertuples():
        if bar.low <= stop:
            return 0
        if bar.high >= target:
            return 1
    return 0

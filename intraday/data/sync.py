"""Incremental download of 1-minute bars into the local store."""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from intraday.config import AlpacaConfig, Settings, ensure_dirs
from intraday.data.alpaca import AlpacaClient
from intraday.data.store import BarStore

logger = logging.getLogger(__name__)


def sync_history(
    settings: Settings | None = None,
    years: float = 3.0,
    client: AlpacaClient | None = None,
    store: BarStore | None = None,
    symbols: list[str] | None = None,
    chunk_days: int = 30,
) -> pd.DataFrame:
    """Download missing history for every symbol, oldest gap first.

    Resumable: each symbol starts from the last bar already stored, so an
    interrupted run costs nothing but the current chunk.
    """
    ensure_dirs()
    settings = settings or Settings()
    client = client or AlpacaClient(AlpacaConfig.from_env())
    store = store or BarStore()
    symbols = symbols or list(settings.all_symbols())

    end = client.latest_close_cutoff(client.config.historical_feed)
    default_start = end - dt.timedelta(days=int(365 * years))

    for symbol in symbols:
        for window_start, window_end in _missing_windows(store, symbol, default_start, end):
            logger.info(
                "%s: fetching %s -> %s", symbol, window_start.date(), window_end.date()
            )
            cursor = window_start
            total = 0
            while cursor < window_end:
                chunk_end = min(cursor + dt.timedelta(days=chunk_days), window_end)
                frame = client.bars([symbol], cursor, chunk_end)
                if not frame.empty:
                    total += store.write(frame)
                cursor = chunk_end
            logger.info("%s: stored, %d rows touched", symbol, total)

    return store.coverage()


def _missing_windows(
    store: BarStore, symbol: str, start: dt.datetime, end: dt.datetime
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Windows to download: the backfill before stored data, and the tail after it."""
    span = store.span(symbol)
    if span is None:
        return [(start, end)]
    first, last = (ts.to_pydatetime() for ts in span)
    windows = []
    if start < first:
        windows.append((start, first))
    tail_start = last + dt.timedelta(minutes=1)
    if tail_start < end:
        windows.append((tail_start, end))
    return windows

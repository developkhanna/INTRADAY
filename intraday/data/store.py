"""Local bar storage: one parquet file per symbol and month."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd

from intraday.config import BARS_DIR, MARKET_TZ, SESSION_CLOSE_MIN, SESSION_OPEN_MIN

logger = logging.getLogger(__name__)

SCHEMA = [
    "symbol", "timestamp", "open", "high", "low", "close",
    "volume", "trade_count", "vwap_bar",
]


class BarStore:
    def __init__(self, root: Path = BARS_DIR, timeframe: str = "1Min"):
        self.root = Path(root) / timeframe
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str, period: pd.Period) -> Path:
        directory = self.root / symbol
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{period.year:04d}-{period.month:02d}.parquet"

    def write(self, bars: pd.DataFrame) -> int:
        """Merge bars into the store, de-duplicating on (symbol, timestamp)."""
        if bars.empty:
            return 0
        bars = bars.copy()
        bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
        written = 0
        bars["_period"] = (
            bars["timestamp"].dt.tz_convert("UTC").dt.tz_localize(None).dt.to_period("M")
        )
        for (symbol, period), group in bars.groupby(["symbol", "_period"], sort=False):
            path = self._path(symbol, period)
            group = group.drop(columns="_period")
            if path.exists():
                existing = pd.read_parquet(path)
                group = pd.concat([existing, group], ignore_index=True)
            group = (
                group.drop_duplicates(subset=["symbol", "timestamp"], keep="last")
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
            # Write through a temp file: an interrupted run must not leave a
            # half-written parquet behind, which would poison every later read.
            temp = path.with_suffix(".parquet.tmp")
            group.to_parquet(temp, index=False)
            temp.replace(path)
            written += len(group)
        return written

    def symbols(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def read(
        self,
        symbols: list[str] | None = None,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> pd.DataFrame:
        symbols = symbols or self.symbols()
        start_ts = _as_utc(start)
        end_ts = _as_utc(end)
        frames = []
        for symbol in symbols:
            directory = self.root / symbol
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.parquet")):
                if start is not None or end is not None:
                    period = pd.Period(path.stem, freq="M")
                    if start_ts is not None and period.end_time.tz_localize("UTC") < start_ts:
                        continue
                    if end_ts is not None and period.start_time.tz_localize("UTC") > end_ts:
                        continue
                frames.append(pd.read_parquet(path))
        if not frames:
            return pd.DataFrame(columns=SCHEMA)
        out = pd.concat(frames, ignore_index=True)
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
        if start_ts is not None:
            out = out[out["timestamp"] >= start_ts]
        if end_ts is not None:
            out = out[out["timestamp"] <= end_ts]
        return out.sort_values(["symbol", "timestamp"]).reset_index(drop=True)

    def coverage(self) -> pd.DataFrame:
        rows = []
        for symbol in self.symbols():
            frame = self.read([symbol])
            if frame.empty:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "bars": len(frame),
                    "first": frame["timestamp"].min(),
                    "last": frame["timestamp"].max(),
                    "sessions": frame["timestamp"].dt.tz_convert(MARKET_TZ).dt.date.nunique(),
                }
            )
        return pd.DataFrame(rows)

    def span(self, symbol: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        """Earliest and latest stored bar for a symbol, or None if empty."""
        directory = self.root / symbol
        if not directory.exists():
            return None
        files = sorted(directory.glob("*.parquet"))
        if not files:
            return None
        first = pd.read_parquet(files[0], columns=["timestamp"])["timestamp"].min()
        last = pd.read_parquet(files[-1], columns=["timestamp"])["timestamp"].max()
        return pd.Timestamp(first), pd.Timestamp(last)

    def last_timestamp(self, symbol: str) -> pd.Timestamp | None:
        span = self.span(symbol)
        return span[1] if span else None


def _as_utc(value: dt.datetime | dt.date | None) -> pd.Timestamp | None:
    """Accept dates or naive/aware datetimes; always compare in UTC."""
    if value is None:
        return None
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def regular_hours_only(bars: pd.DataFrame) -> pd.DataFrame:
    """Keep 09:30-15:59 ET bars. Extended hours are thin and behave differently."""
    if bars.empty:
        return bars
    local = bars["timestamp"].dt.tz_convert(MARKET_TZ)
    minute_of_day = local.dt.hour * 60 + local.dt.minute
    mask = (minute_of_day >= SESSION_OPEN_MIN) & (minute_of_day < SESSION_CLOSE_MIN)
    return bars[mask].reset_index(drop=True)

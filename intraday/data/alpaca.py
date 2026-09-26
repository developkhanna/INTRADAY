"""Alpaca market-data client.

Only the endpoints this project needs: paged historical bars, latest quotes and
the trading calendar. Everything returns tidy pandas frames in UTC.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Iterable, Iterator

import pandas as pd
import requests

from intraday.config import AlpacaConfig

logger = logging.getLogger(__name__)

BAR_COLUMNS = {
    "t": "timestamp",
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "v": "volume",
    "n": "trade_count",
    "vw": "vwap_bar",
}

MAX_SYMBOLS_PER_REQUEST = 50
PAGE_LIMIT = 10000


class AlpacaError(RuntimeError):
    pass


class AlpacaClient:
    def __init__(self, config: AlpacaConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "APCA-API-KEY-ID": config.key_id,
                "APCA-API-SECRET-KEY": config.secret_key,
            }
        )

    def _get(self, url: str, params: dict, max_retries: int = 5) -> dict:
        delay = 1.0
        for _attempt in range(max_retries):
            response = self.session.get(url, params=params, timeout=60)
            if response.status_code == 429:
                logger.warning("rate limited, sleeping %.1fs", delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if response.status_code >= 500:
                logger.warning("server error %s, retrying", response.status_code)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if not response.ok:
                raise AlpacaError(f"{response.status_code} {response.text[:300]}")
            return response.json()
        raise AlpacaError(f"gave up after {max_retries} attempts: {url}")

    def account(self) -> dict:
        return self._get(f"{self.config.trading_url}/v2/account", {})

    def latest_close_cutoff(self, feed: str) -> dt.datetime:
        """Newest timestamp this feed is allowed to return."""
        now = dt.datetime.now(dt.timezone.utc)
        if feed == "sip":
            return now - dt.timedelta(minutes=self.config.sip_delay_minutes)
        return now

    def iter_bars(
        self,
        symbols: Iterable[str],
        start: dt.datetime,
        end: dt.datetime,
        timeframe: str = "1Min",
        feed: str | None = None,
        adjustment: str = "split",
    ) -> Iterator[pd.DataFrame]:
        """Yield one frame per API page for the given symbols and window."""
        feed = feed or self.config.historical_feed
        symbols = list(symbols)
        cutoff = self.latest_close_cutoff(feed)
        if end > cutoff:
            end = cutoff
        if start >= end:
            return

        for i in range(0, len(symbols), MAX_SYMBOLS_PER_REQUEST):
            chunk = symbols[i : i + MAX_SYMBOLS_PER_REQUEST]
            page_token: str | None = None
            while True:
                params = {
                    "symbols": ",".join(chunk),
                    "timeframe": timeframe,
                    "start": _iso(start),
                    "end": _iso(end),
                    "limit": PAGE_LIMIT,
                    "feed": feed,
                    "adjustment": adjustment,
                    "sort": "asc",
                }
                if page_token:
                    params["page_token"] = page_token
                payload = self._get(f"{self.config.data_url}/v2/stocks/bars", params)
                frame = _bars_payload_to_frame(payload.get("bars") or {})
                if not frame.empty:
                    yield frame
                page_token = payload.get("next_page_token")
                if not page_token:
                    break

    def bars(
        self,
        symbols: Iterable[str],
        start: dt.datetime,
        end: dt.datetime,
        **kwargs,
    ) -> pd.DataFrame:
        frames = list(self.iter_bars(symbols, start, end, **kwargs))
        if not frames:
            return _empty_bars()
        out = pd.concat(frames, ignore_index=True)
        return out.sort_values(["symbol", "timestamp"]).reset_index(drop=True)

    def latest_quotes(self, symbols: Iterable[str], feed: str | None = None) -> pd.DataFrame:
        feed = feed or self.config.realtime_feed
        payload = self._get(
            f"{self.config.data_url}/v2/stocks/quotes/latest",
            {"symbols": ",".join(symbols), "feed": feed},
        )
        rows = []
        for symbol, quote in (payload.get("quotes") or {}).items():
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": pd.Timestamp(quote["t"]).tz_convert("UTC"),
                    "bid": quote.get("bp"),
                    "ask": quote.get("ap"),
                    "bid_size": quote.get("bs"),
                    "ask_size": quote.get("as"),
                }
            )
        return pd.DataFrame(rows)

    def calendar(self, start: dt.date, end: dt.date) -> pd.DataFrame:
        payload = self._get(
            f"{self.config.trading_url}/v2/calendar",
            {"start": start.isoformat(), "end": end.isoformat()},
        )
        return pd.DataFrame(payload)


def _iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_bars() -> pd.DataFrame:
    columns = ["symbol", "timestamp", *BAR_COLUMNS.values()]
    ordered = [c for c in dict.fromkeys(columns) if c != "timestamp"] 
    return pd.DataFrame(columns=["symbol", "timestamp", *[c for c in ordered if c != "symbol"]])


def _bars_payload_to_frame(bars_by_symbol: dict[str, list[dict]]) -> pd.DataFrame:
    frames = []
    for symbol, bars in bars_by_symbol.items():
        if not bars:
            continue
        frame = pd.DataFrame(bars).rename(columns=BAR_COLUMNS)
        frame["symbol"] = symbol
        frames.append(frame)
    if not frames:
        return _empty_bars()
    out = pd.concat(frames, ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    keep = [
        "symbol", "timestamp", "open", "high", "low", "close",
        "volume", "trade_count", "vwap_bar",
    ]
    return out[[c for c in keep if c in out.columns]]

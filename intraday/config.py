"""Central configuration: paths, universe, horizons, trading session."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")

DATA_DIR = Path(os.environ.get("INTRADAY_DATA_DIR", Path.home() / ".intraday"))
BARS_DIR = DATA_DIR / "bars"
DATASET_DIR = DATA_DIR / "datasets"
MODEL_DIR = DATA_DIR / "models"
LEDGER_DB = DATA_DIR / "ledger.duckdb"

# Regular trading hours, minutes from midnight in MARKET_TZ.
SESSION_OPEN_MIN = 9 * 60 + 30
SESSION_CLOSE_MIN = 16 * 60

# Prediction horizons in minutes.
HORIZONS = (5, 10, 15, 30, 60)

# Benchmarks are always fetched: they feed relative-strength features.
BENCHMARKS = ("SPY", "QQQ")

# Sector ETFs, and the sector each tradable symbol is mapped to.
SECTOR_ETFS = ("XLK", "XLY", "XLF", "XLE", "XLV", "XLI", "XLC", "XLP")

DEFAULT_SECTOR_MAP: dict[str, str] = {
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AMD": "XLK", "AVGO": "XLK",
    "MU": "XLK", "TSM": "XLK", "ARM": "XLK", "LRCX": "XLK", "AMAT": "XLK",
    "SMCI": "XLK", "PLTR": "XLK", "CRM": "XLK", "ORCL": "XLK", "INTC": "XLK",
    "AMZN": "XLY", "TSLA": "XLY", "SHOP": "XLY", "ABNB": "XLY",
    "META": "XLC", "GOOGL": "XLC", "NFLX": "XLC",
    "COIN": "XLF", "HOOD": "XLF", "JPM": "XLF",
    "NBIS": "XLK", "UBER": "XLY",
}

DEFAULT_UNIVERSE: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "GOOGL", "TSLA",
    "AVGO", "MU", "TSM", "SHOP", "NBIS", "ARM", "LRCX", "AMAT",
    "PLTR", "COIN", "SMCI", "NFLX",
)


@dataclass(frozen=True)
class AlpacaConfig:
    """Credentials and endpoints for Alpaca market data."""

    key_id: str
    secret_key: str
    data_url: str = "https://data.alpaca.markets"
    trading_url: str = "https://paper-api.alpaca.markets"
    # SIP is the full consolidated tape. Free accounts may query it only up to
    # 15 minutes ago; IEX is unrestricted but covers ~2% of volume.
    historical_feed: str = "sip"
    realtime_feed: str = "iex"
    # Free-tier SIP embargo, with a safety margin.
    sip_delay_minutes: int = 16

    @classmethod
    def from_env(cls) -> AlpacaConfig:
        key_id = os.environ.get("ALPACA_API_KEY_ID", "")
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
        if not key_id or not secret_key:
            raise RuntimeError(
                "ALPACA_API_KEY_ID and ALPACA_SECRET_KEY must be set in the environment."
            )
        return cls(key_id=key_id, secret_key=secret_key)


@dataclass
class Settings:
    """Everything the pipeline needs that a user might reasonably change."""

    universe: tuple[str, ...] = DEFAULT_UNIVERSE
    sector_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SECTOR_MAP))
    horizons: tuple[int, ...] = HORIZONS

    def support_symbols(self) -> tuple[str, ...]:
        sectors = {self.sector_map.get(sym) for sym in self.universe}
        sectors.discard(None)
        return tuple(sorted(set(BENCHMARKS) | sectors))  # type: ignore[arg-type]

    def all_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.universe) | set(self.support_symbols())))


WATCHLIST_FILE = DATA_DIR / "watchlist.json"


def load_watchlist() -> tuple[str, ...]:
    """The editable universe. Falls back to the default list."""
    if WATCHLIST_FILE.exists():
        symbols = json.loads(WATCHLIST_FILE.read_text())
        if symbols:
            return tuple(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
    return DEFAULT_UNIVERSE


def save_watchlist(symbols: list[str]) -> tuple[str, ...]:
    """Persist the universe. New symbols are picked up by the next data sync."""
    ensure_dirs()
    cleaned = tuple(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
    WATCHLIST_FILE.write_text(json.dumps(list(cleaned), indent=2))
    return cleaned


def settings_from_watchlist() -> Settings:
    return Settings(universe=load_watchlist())


def ensure_dirs() -> None:
    for path in (DATA_DIR, BARS_DIR, DATASET_DIR, MODEL_DIR):
        path.mkdir(parents=True, exist_ok=True)

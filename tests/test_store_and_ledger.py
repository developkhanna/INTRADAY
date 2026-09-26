from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from intraday.data.store import BarStore, regular_hours_only
from intraday.live.ledger import Ledger


def _bars(symbol: str, start: str, minutes: int) -> pd.DataFrame:
    timestamps = pd.date_range(start, periods=minutes, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "symbol": symbol,
            "timestamp": timestamps,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000.0,
            "trade_count": 10.0,
            "vwap_bar": 100.2,
        }
    )


@pytest.fixture()
def store(tmp_path: Path) -> BarStore:
    return BarStore(root=tmp_path)


def test_write_is_idempotent(store: BarStore) -> None:
    bars = _bars("AAPL", "2025-02-03 14:30", 60)
    store.write(bars)
    store.write(bars)
    assert len(store.read(["AAPL"])) == 60


def test_read_respects_the_date_window(store: BarStore) -> None:
    store.write(_bars("AAPL", "2025-02-03 14:30", 60))
    store.write(_bars("AAPL", "2025-03-03 14:30", 60))
    assert len(store.read(["AAPL"], start=dt.date(2025, 3, 1))) == 60
    assert len(store.read(["AAPL"])) == 120


def test_span_reports_first_and_last_bar(store: BarStore) -> None:
    store.write(_bars("AAPL", "2025-02-03 14:30", 60))
    first, last = store.span("AAPL")
    assert first < last
    assert store.span("MSFT") is None


def test_regular_hours_filter_drops_extended_session() -> None:
    bars = _bars("AAPL", "2025-02-03 09:00", 24 * 60)  # a full UTC day
    filtered = regular_hours_only(bars)
    local = filtered["timestamp"].dt.tz_convert("America/New_York")
    assert len(filtered) == 390
    assert local.dt.hour.min() == 9 and local.dt.hour.max() == 15


def test_ledger_records_then_scores(tmp_path: Path) -> None:
    ledger = Ledger(path=tmp_path / "ledger.duckdb")
    now = pd.Timestamp("2025-02-03 15:00", tz="UTC")
    prediction = pd.DataFrame(
        [
            {
                "prediction_id": "p1",
                "created_at": now,
                "bar_timestamp": now,
                "symbol": "AAPL",
                "horizon_minutes": 15,
                "model_version": "tbs_t1_s0p5_15m",
                "model_status": "UNVALIDATED",
                "probability": 0.42,
                "action": "NO TRADE",
                "features": {"ret_5m": 0.001},
            }
        ]
    )
    assert ledger.record(prediction) == 1

    assert len(ledger.pending(now + pd.Timedelta(minutes=20))) == 1
    assert len(ledger.pending(now + pd.Timedelta(minutes=5))) == 0

    ledger.attach_outcome(
        pd.DataFrame(
            [
                {
                    "prediction_id": "p1",
                    "resolved_at": now + pd.Timedelta(minutes=15),
                    "realized_return": 0.004,
                    "realized_mfe": 0.006,
                    "realized_mae": -0.001,
                    "target_before_stop": 1,
                    "minutes_to_resolution": 15,
                }
            ]
        )
    )
    assert ledger.pending(now + pd.Timedelta(minutes=20)).empty
    scored = ledger.scored()
    assert len(scored) == 1 and scored["target_before_stop"].iloc[0] == 1

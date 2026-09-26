"""Leakage tests.

The single most common way a backtest lies is by letting a feature peek at the
future. These tests try hard to catch that.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from intraday.config import MARKET_TZ, Settings
from intraday.features.engineer import build_features, feature_columns
from intraday.labels.builder import Barrier, build_labels


def _session_bars(symbol: str, day: dt.date, closes: np.ndarray) -> pd.DataFrame:
    start = pd.Timestamp(
        dt.datetime.combine(day, dt.time(9, 30)), tz=MARKET_TZ
    ).tz_convert("UTC")
    timestamps = [start + pd.Timedelta(minutes=i) for i in range(len(closes))]
    return pd.DataFrame(
        {
            "symbol": symbol,
            "timestamp": timestamps,
            "open": closes,
            "high": closes * 1.001,
            "low": closes * 0.999,
            "close": closes,
            "volume": np.full(len(closes), 10_000.0),
            "trade_count": np.full(len(closes), 100.0),
            "vwap_bar": closes,
        }
    )


def _synthetic_bars(
    symbols: tuple[str, ...], sessions: int = 6, minutes: int = 200
) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    frames = []
    day = dt.date(2025, 1, 6)
    for _ in range(sessions):
        for symbol in symbols:
            steps = rng.normal(0, 0.0008, minutes)
            closes = 100 * np.exp(np.cumsum(steps))
            frames.append(_session_bars(symbol, day, closes))
        day += dt.timedelta(days=1)
    combined = pd.concat(frames, ignore_index=True)
    return combined.sort_values(["symbol", "timestamp"]).reset_index(drop=True)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings(universe=("AAPL", "MSFT"))


@pytest.fixture(scope="module")
def bars(settings: Settings) -> pd.DataFrame:
    return _synthetic_bars(tuple(settings.all_symbols()))


def test_features_ignore_the_future(bars: pd.DataFrame, settings: Settings) -> None:
    """Truncating the data after T must not change any feature value at T.

    If a feature is computed with a centred window or a negative shift, the
    truncated run differs and this fails.
    """
    full = build_features(bars, settings=settings)
    cutoff = full["timestamp"].quantile(0.6)
    truncated = build_features(bars[bars["timestamp"] <= cutoff], settings=settings)

    columns = [c for c in feature_columns(full) if c in truncated.columns]
    left = full[full["timestamp"] <= cutoff].set_index(["symbol", "timestamp"])[columns]
    right = truncated.set_index(["symbol", "timestamp"])[columns]
    common = left.index.intersection(right.index)

    assert len(common) > 100
    pd.testing.assert_frame_equal(
        left.loc[common].sort_index(),
        right.loc[common].sort_index(),
        check_exact=False,
        rtol=1e-9,
    )


def test_labels_need_future_bars(bars: pd.DataFrame) -> None:
    """The last minutes of a session have no future left, so labels must be NaN."""
    subset = bars[bars["symbol"] == "AAPL"].reset_index(drop=True)
    atr = pd.Series(subset["close"] * 0.002, index=subset.index)
    labels = build_labels(subset, atr=atr, barriers=(Barrier(1.0, 0.5),), barrier_horizons=(15,))

    tail = labels.tail(4)
    assert tail["fwd_ret_5m"].isna().all()
    assert tail["tbs_t1_s0p5_15m"].isna().all()
    assert labels["fwd_ret_5m"].notna().sum() > 0


def test_forward_return_matches_manual_arithmetic(bars: pd.DataFrame) -> None:
    subset = bars[bars["symbol"] == "AAPL"].reset_index(drop=True)
    labels = build_labels(subset, atr=None, horizons=(5,), barrier_horizons=())
    i = 20
    expected = subset["close"].iloc[i + 5] / subset["close"].iloc[i] - 1
    assert labels["fwd_ret_5m"].iloc[i] == pytest.approx(expected)


def test_labels_do_not_cross_the_overnight_gap(bars: pd.DataFrame) -> None:
    subset = bars[bars["symbol"] == "AAPL"].reset_index(drop=True)
    labels = build_labels(subset, atr=None, horizons=(5,), barrier_horizons=())
    local = subset["timestamp"].dt.tz_convert(MARKET_TZ)
    last_of_session = local.dt.date != local.dt.date.shift(-1)
    assert labels.loc[last_of_session.to_numpy(), "fwd_ret_5m"].isna().all()


def test_mfe_is_above_and_mae_below_the_entry(bars: pd.DataFrame) -> None:
    subset = bars[bars["symbol"] == "AAPL"].reset_index(drop=True)
    labels = build_labels(subset, atr=None, horizons=(15,), barrier_horizons=())
    valid = labels["mfe_15m"].notna()
    assert (labels.loc[valid, "mfe_15m"] >= labels.loc[valid, "mae_15m"]).all()
    assert (labels.loc[valid, "mfe_15m"] >= labels.loc[valid, "fwd_ret_15m"] - 1e-12).all()
    assert (labels.loc[valid, "mae_15m"] <= labels.loc[valid, "fwd_ret_15m"] + 1e-12).all()


def test_target_before_stop_on_a_hand_built_path() -> None:
    """A path that rises 1 ATR before it ever falls must be labelled 1, and vice versa."""
    up_closes = np.array([100.0, 100.5, 101.0, 101.5, 102.0, 102.0, 102.0])
    down_closes = np.array([100.0, 99.5, 99.0, 98.5, 98.0, 98.0, 98.0])
    day = dt.date(2025, 3, 3)

    for closes, expected in ((up_closes, 1.0), (down_closes, 0.0)):
        frame = _session_bars("AAPL", day, closes)
        frame["high"] = closes
        frame["low"] = closes
        atr = pd.Series(1.0, index=frame.index)
        labels = build_labels(
            frame, atr=atr, horizons=(5,), barriers=(Barrier(1.0, 1.0),), barrier_horizons=(5,)
        )
        assert labels["tbs_t1_s1_5m"].iloc[0] == expected

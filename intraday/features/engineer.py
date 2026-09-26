"""Point-in-time feature engineering.

Every column produced here must be computable from bars with timestamp <= T.
The rule is enforced by construction (no negative shifts, no centred windows,
no forward fills across time) and checked by tests/test_leakage.py.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from intraday.config import MARKET_TZ, SESSION_OPEN_MIN, Settings
from intraday.data.store import regular_hours_only

RETURN_LOOKBACKS = (1, 3, 5, 10, 15, 30)
OPENING_RANGE_MINUTES = 15
RVOL_SESSIONS = 20

FEATURE_PREFIXES = (
    "ret_", "dist_", "vwap_", "rvol", "vol_", "rsi_", "atr", "realized_vol",
    "day_range_pos", "or_", "gap_", "spread_", "minutes_since_open", "rs_",
    "bench_", "sector_", "trades_", "close_", "session_",
)


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100 - 100 / (1 + rs)


def _true_range(frame: pd.DataFrame) -> pd.Series:
    prev_close = frame["close"].shift(1)
    ranges = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def _corwin_schultz_spread(high: pd.Series, low: pd.Series) -> pd.Series:
    """High-low spread estimator (Corwin & Schultz 2012), as a fraction of price.

    Uses the current and previous bar only, so it is point-in-time safe.
    """
    h1, l1 = high.shift(1), low.shift(1)
    beta = np.log(high / low) ** 2 + np.log(h1 / l1) ** 2
    hi2 = pd.concat([high, h1], axis=1).max(axis=1)
    lo2 = pd.concat([low, l1], axis=1).min(axis=1)
    gamma = np.log(hi2 / lo2) ** 2
    denom = 3 - 2 * np.sqrt(2)
    alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / denom - np.sqrt(gamma / denom)
    alpha = alpha.clip(lower=0.0)
    spread = 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha))
    return spread.replace([np.inf, -np.inf], np.nan)


def _session_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Features for one symbol, one session. `frame` is sorted by timestamp."""
    out = pd.DataFrame(index=frame.index)
    close = frame["close"]

    out["close_price"] = close
    for lookback in RETURN_LOOKBACKS:
        out[f"ret_{lookback}m"] = close.pct_change(lookback)

    typical = (frame["high"] + frame["low"] + frame["close"]) / 3
    cum_volume = frame["volume"].cumsum()
    cum_dollar = (typical * frame["volume"]).cumsum()
    vwap = cum_dollar / cum_volume.replace(0, np.nan)
    out["vwap"] = vwap
    out["dist_vwap"] = close / vwap - 1
    out["vwap_slope_5m"] = vwap.pct_change(5)

    out["vol_1m"] = frame["volume"]
    out["vol_5m"] = frame["volume"].rolling(5, min_periods=1).sum()
    out["vol_15m"] = frame["volume"].rolling(15, min_periods=1).sum()
    out["volume_accel"] = (out["vol_5m"] / 5) / (out["vol_15m"] / 15).replace(0, np.nan)
    out["trades_1m"] = frame.get("trade_count", pd.Series(np.nan, index=frame.index))

    out["rsi_14"] = _rsi(close, 14)
    out["rsi_60"] = _rsi(close, 60)

    true_range = _true_range(frame)
    atr = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    out["atr"] = atr
    out["atr_pct"] = atr / close
    out["realized_vol_30m"] = close.pct_change().rolling(30, min_periods=10).std()

    day_high = frame["high"].cummax()
    day_low = frame["low"].cummin()
    span = (day_high - day_low).replace(0, np.nan)
    out["day_range_pos"] = (close - day_low) / span
    out["dist_day_high"] = close / day_high - 1
    out["dist_day_low"] = close / day_low - 1

    minutes = np.arange(len(frame))
    opening = frame.iloc[:OPENING_RANGE_MINUTES]
    or_high = opening["high"].max()
    or_low = opening["low"].min()
    established = minutes >= OPENING_RANGE_MINUTES
    out["dist_or_high"] = np.where(established, close / or_high - 1, np.nan)
    out["dist_or_low"] = np.where(established, close / or_low - 1, np.nan)
    out["or_width_pct"] = np.where(established, (or_high - or_low) / or_low, np.nan)

    out["spread_est"] = _corwin_schultz_spread(frame["high"], frame["low"])
    out["spread_hl_pct"] = (frame["high"] - frame["low"]) / close

    return out


def _symbol_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Features for one symbol across all its sessions."""
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    local = frame["timestamp"].dt.tz_convert(MARKET_TZ)
    frame = frame.assign(
        session=local.dt.date,
        minutes_since_open=local.dt.hour * 60 + local.dt.minute - SESSION_OPEN_MIN,
    )

    parts = [
        _session_features(group)
        for _, group in frame.groupby("session", sort=True)
    ]
    features = pd.concat(parts).sort_index()
    features["minutes_since_open"] = frame["minutes_since_open"].to_numpy()
    features["symbol"] = frame["symbol"].to_numpy()
    features["timestamp"] = frame["timestamp"].to_numpy()
    features["session"] = frame["session"].to_numpy()

    # Overnight gap: today's first price versus the previous session's close.
    session_close = frame.groupby("session")["close"].last()
    session_open = frame.groupby("session")["open"].first()
    gap = (session_open / session_close.shift(1) - 1).rename("gap_open_pct")
    features = features.merge(gap, left_on="session", right_index=True, how="left")

    # Time-adjusted relative volume: today's volume for this minute-of-day
    # against the median for the same minute over the previous sessions.
    pivot = features.pivot_table(
        index="session", columns="minutes_since_open", values="vol_1m", aggfunc="last"
    )
    baseline = pivot.shift(1).rolling(RVOL_SESSIONS, min_periods=3).median()
    baseline_long = baseline.stack().rename("vol_baseline").reset_index()
    features = features.merge(
        baseline_long, on=["session", "minutes_since_open"], how="left"
    )
    features["rvol"] = features["vol_1m"] / features["vol_baseline"].replace(0, np.nan)
    features = features.drop(columns="vol_baseline")

    return features


def _benchmark_returns(bars: pd.DataFrame, symbols: tuple[str, ...]) -> pd.DataFrame:
    """Wide frame of benchmark returns indexed by timestamp."""
    frames = []
    for symbol in symbols:
        sub = bars[bars["symbol"] == symbol].sort_values("timestamp")
        if sub.empty:
            continue
        local = sub["timestamp"].dt.tz_convert(MARKET_TZ)
        session = local.dt.date
        block = pd.DataFrame({"timestamp": sub["timestamp"].to_numpy()})
        grouped = sub.groupby(session)["close"]
        for lookback in (5, 15, 30):
            block[f"{symbol}_ret_{lookback}m"] = grouped.pct_change(lookback).to_numpy()
        frames.append(block.set_index("timestamp"))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1)


def build_features(bars: pd.DataFrame, settings: Settings | None = None) -> pd.DataFrame:
    """Build the point-in-time feature matrix for the tradable universe."""
    settings = settings or Settings()
    bars = regular_hours_only(bars)
    if bars.empty:
        return pd.DataFrame()

    benchmarks = _benchmark_returns(bars, settings.support_symbols())

    rows = []
    for symbol in settings.universe:
        sub = bars[bars["symbol"] == symbol]
        if sub.empty:
            continue
        rows.append(_symbol_features(sub))
    if not rows:
        return pd.DataFrame()

    features = pd.concat(rows, ignore_index=True)

    if not benchmarks.empty:
        features = features.merge(
            benchmarks, left_on="timestamp", right_index=True, how="left"
        )
        for bench in ("SPY", "QQQ"):
            for lookback in (5, 15, 30):
                column = f"{bench}_ret_{lookback}m"
                if column in features:
                    features[f"bench_{bench.lower()}_ret_{lookback}m"] = features[column]
                    features[f"rs_{bench.lower()}_{lookback}m"] = (
                        features[f"ret_{lookback}m"] - features[column]
                    )

        sector = features["symbol"].map(settings.sector_map)
        for lookback in (5, 15, 30):
            values = np.full(len(features), np.nan)
            for etf in set(sector.dropna()):
                column = f"{etf}_ret_{lookback}m"
                if column in features:
                    mask = (sector == etf).to_numpy()
                    values[mask] = features.loc[mask, column].to_numpy()
            features[f"sector_ret_{lookback}m"] = values
            features[f"rs_sector_{lookback}m"] = features[f"ret_{lookback}m"] - values

        features = features.drop(
            columns=[
                c for c in features.columns
                if c.endswith("m") and c.split("_")[0] in set(settings.support_symbols())
            ]
        )

    return features.sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def feature_columns(frame: pd.DataFrame) -> list[str]:
    excluded = {"symbol", "timestamp", "session", "close_price", "vwap", "atr"}
    return [
        column
        for column in frame.columns
        if column not in excluded
        and not column.startswith(("label_", "fwd_", "mfe_", "mae_", "tbs_"))
        and pd.api.types.is_numeric_dtype(frame[column])
    ]

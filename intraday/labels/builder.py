"""Forward-looking labels.

These are the answers the model is trained to predict. They are computed from
bars strictly after T and must never be joined into the feature matrix except
as targets. Labels stay inside one trading session: a horizon that would run
past the close is left as NaN rather than jumping the overnight gap.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from intraday.config import HORIZONS, MARKET_TZ


@dataclass(frozen=True)
class Barrier:
    """Target and stop, expressed as multiples of ATR."""

    target_atr: float
    stop_atr: float

    @property
    def name(self) -> str:
        return f"t{self.target_atr:g}_s{self.stop_atr:g}".replace(".", "p")


DEFAULT_BARRIERS = (
    Barrier(0.5, 0.3),
    Barrier(0.75, 0.5),
    Barrier(1.0, 0.5),
    Barrier(1.5, 0.75),
)

BARRIER_HORIZONS = (15, 30, 60)


def _session_ids(timestamps: pd.Series) -> np.ndarray:
    dates = timestamps.dt.tz_convert(MARKET_TZ).dt.date.to_numpy()
    _, ids = np.unique(dates, return_inverse=True)
    return ids


def _shift_forward(values: np.ndarray, k: int) -> np.ndarray:
    """values[t + k], padded with NaN at the tail."""
    out = np.full(values.shape, np.nan)
    if k < len(values):
        out[: len(values) - k] = values[k:]
    return out


def _same_session(session_ids: np.ndarray, k: int) -> np.ndarray:
    shifted = _shift_forward(session_ids.astype(float), k)
    return shifted == session_ids


def build_labels(
    bars: pd.DataFrame,
    atr: pd.Series | None = None,
    horizons: tuple[int, ...] = HORIZONS,
    barriers: tuple[Barrier, ...] = DEFAULT_BARRIERS,
    barrier_horizons: tuple[int, ...] = BARRIER_HORIZONS,
) -> pd.DataFrame:
    """Return one row per input bar with forward returns, MFE/MAE and barrier outcomes.

    `bars` must be sorted by (symbol, timestamp) and restricted to regular hours.
    `atr` is the point-in-time ATR aligned to `bars`; without it, barrier labels
    are skipped.
    """
    frames = []
    for _symbol, group in bars.groupby("symbol", sort=False):
        index = group.index
        sub_atr = atr.loc[index] if atr is not None else None
        frames.append(
            _labels_for_symbol(
                group.reset_index(drop=True), sub_atr, horizons, barriers, barrier_horizons
            ).set_index(index)
        )
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).sort_index()


def _labels_for_symbol(
    group: pd.DataFrame,
    atr: pd.Series | None,
    horizons: tuple[int, ...],
    barriers: tuple[Barrier, ...],
    barrier_horizons: tuple[int, ...],
) -> pd.DataFrame:
    close = group["close"].to_numpy(dtype=float)
    high = group["high"].to_numpy(dtype=float)
    low = group["low"].to_numpy(dtype=float)
    sessions = _session_ids(group["timestamp"])

    out = pd.DataFrame(index=group.index)
    max_horizon = max([*horizons, *barrier_horizons])

    running_high = np.full(len(close), -np.inf)
    running_low = np.full(len(close), np.inf)
    valid = np.ones(len(close), dtype=bool)

    for k in range(1, max_horizon + 1):
        in_session = _same_session(sessions, k)
        valid &= in_session
        running_high = np.where(valid, np.fmax(running_high, _shift_forward(high, k)), np.nan)
        running_low = np.where(valid, np.fmin(running_low, _shift_forward(low, k)), np.nan)

        if k in horizons:
            future_close = np.where(valid, _shift_forward(close, k), np.nan)
            out[f"fwd_ret_{k}m"] = future_close / close - 1
            out[f"mfe_{k}m"] = running_high / close - 1
            out[f"mae_{k}m"] = running_low / close - 1
            out[f"label_up_{k}m"] = (out[f"fwd_ret_{k}m"] > 0).astype(float)
            out.loc[out[f"fwd_ret_{k}m"].isna(), f"label_up_{k}m"] = np.nan

    if atr is not None:
        atr_values = atr.to_numpy(dtype=float)
        for barrier in barriers:
            for horizon in barrier_horizons:
                out[f"tbs_{barrier.name}_{horizon}m"] = _target_before_stop(
                    close, high, low, sessions, atr_values, barrier, horizon
                )

    return out


def _target_before_stop(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    sessions: np.ndarray,
    atr: np.ndarray,
    barrier: Barrier,
    horizon: int,
) -> np.ndarray:
    """1 if the target is touched before the stop within the horizon, else 0.

    NaN when the ATR is unknown or the horizon runs past the session close.
    Touching both inside the same minute counts as a stop: the conservative
    assumption, since a 1-minute bar does not reveal the order of the ticks.
    """
    up = close + barrier.target_atr * atr
    down = close - barrier.stop_atr * atr

    result = np.full(len(close), np.nan)
    undecided = np.isfinite(atr) & (atr > 0)
    outcome = np.zeros(len(close))

    for k in range(1, horizon + 1):
        in_session = _same_session(sessions, k)
        undecided &= in_session
        if not undecided.any():
            break
        future_high = _shift_forward(high, k)
        future_low = _shift_forward(low, k)
        hit_stop = undecided & (future_low <= down)
        hit_target = undecided & (future_high >= up) & ~hit_stop
        outcome[hit_target] = 1.0
        result[hit_target] = 1.0
        result[hit_stop] = 0.0
        undecided &= ~(hit_target | hit_stop)

    # Still undecided at the horizon: neither barrier was touched.
    expired = undecided
    result[expired] = 0.0

    # Rows whose horizon left the session are unusable.
    reaches_close = _same_session(sessions, horizon)
    result[~reaches_close] = np.nan
    result[~np.isfinite(atr) | (atr <= 0)] = np.nan
    return result


def label_columns(frame: pd.DataFrame) -> list[str]:
    return [
        column
        for column in frame.columns
        if column.startswith(("fwd_ret_", "mfe_", "mae_", "label_", "tbs_"))
    ]

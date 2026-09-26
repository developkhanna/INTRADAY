"""Walk-forward splitting with purge and embargo.

Random shuffling on time series is the fastest way to fool yourself: the model
sees tomorrow while training and looks brilliant. Here every test block sits
strictly after its training block, and the minutes whose labels overlap the
boundary are dropped (purge + embargo) so a label computed from future bars
cannot appear in training.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Split:
    name: str
    train: np.ndarray
    test: np.ndarray
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def walk_forward_splits(
    timestamps: pd.Series,
    n_splits: int = 6,
    label_horizon_minutes: int = 60,
    embargo_minutes: int = 60,
    min_train_fraction: float = 0.3,
) -> list[Split]:
    """Expanding-window splits over calendar days."""
    ordered = pd.Series(pd.to_datetime(timestamps.to_numpy(), utc=True))
    days = np.array(sorted(ordered.dt.date.unique()))
    if len(days) < n_splits + 2:
        raise ValueError(f"need at least {n_splits + 2} sessions, got {len(days)}")

    first_test = max(int(len(days) * min_train_fraction), 1)
    boundaries = np.linspace(first_test, len(days), n_splits + 1).astype(int)

    gap = pd.Timedelta(minutes=label_horizon_minutes + embargo_minutes)
    splits = []
    positions = np.arange(len(ordered))
    for i in range(n_splits):
        test_days = days[boundaries[i] : boundaries[i + 1]]
        if len(test_days) == 0:
            continue
        test_start = pd.Timestamp(test_days[0]).tz_localize("UTC")
        test_end = pd.Timestamp(test_days[-1]).tz_localize("UTC") + pd.Timedelta(days=1)
        train_mask = ordered < (test_start - gap)
        test_mask = (ordered >= test_start) & (ordered < test_end)
        if train_mask.sum() == 0 or test_mask.sum() == 0:
            continue
        splits.append(
            Split(
                name=f"fold{i + 1}",
                train=positions[train_mask.to_numpy()],
                test=positions[test_mask.to_numpy()],
                train_end=ordered[train_mask].max(),
                test_start=test_start,
                test_end=test_end,
            )
        )
    return splits

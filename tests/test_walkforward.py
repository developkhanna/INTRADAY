from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday.models.walkforward import walk_forward_splits


def _timestamps(days: int = 30, per_day: int = 390) -> pd.Series:
    stamps = []
    day = pd.Timestamp("2025-01-06", tz="UTC")
    for _ in range(days):
        open_time = day + pd.Timedelta(minutes=14 * 60 + 30)
        stamps.extend(open_time + pd.to_timedelta(np.arange(per_day), "m"))
        day += pd.Timedelta(days=1)
    return pd.Series(stamps)


def test_train_always_precedes_test() -> None:
    timestamps = _timestamps()
    for split in walk_forward_splits(timestamps, n_splits=4):
        assert timestamps.iloc[split.train].max() < timestamps.iloc[split.test].min()
        assert not set(split.train) & set(split.test)


def test_purge_gap_keeps_overlapping_labels_out_of_training() -> None:
    timestamps = _timestamps()
    horizon, embargo = 60, 60
    for split in walk_forward_splits(
        timestamps, n_splits=3, label_horizon_minutes=horizon, embargo_minutes=embargo
    ):
        gap = split.test_start - timestamps.iloc[split.train].max()
        assert gap >= pd.Timedelta(minutes=horizon + embargo)


def test_training_window_expands() -> None:
    splits = walk_forward_splits(_timestamps(), n_splits=4)
    sizes = [len(s.train) for s in splits]
    assert sizes == sorted(sizes)


def test_too_little_history_is_refused() -> None:
    with pytest.raises(ValueError):
        walk_forward_splits(_timestamps(days=3), n_splits=6)

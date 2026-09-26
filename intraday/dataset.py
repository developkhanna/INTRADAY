"""Assemble the training dataset: point-in-time features + forward labels."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd

from intraday.config import DATASET_DIR, Settings, ensure_dirs
from intraday.data.store import BarStore, regular_hours_only
from intraday.features.engineer import build_features
from intraday.labels.builder import build_labels

logger = logging.getLogger(__name__)


def build_dataset(
    settings: Settings | None = None,
    store: BarStore | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    out_path: Path | None = None,
) -> pd.DataFrame:
    """Build and optionally persist the modelling table.

    Features come from bars at or before T; labels from bars strictly after T.
    They are computed separately and joined on (symbol, timestamp) so the two
    halves can never blur into each other.
    """
    ensure_dirs()
    settings = settings or Settings()
    store = store or BarStore()

    symbols = [s for s in settings.all_symbols() if s in set(store.symbols())]
    missing = sorted(set(settings.all_symbols()) - set(symbols))
    if missing:
        logger.warning("no stored bars for: %s", ", ".join(missing))

    bars = store.read(symbols, start=start, end=end)
    if bars.empty:
        raise RuntimeError("no bars in the local store; run scripts/sync_history.py first")

    features = build_features(bars, settings=settings)
    if features.empty:
        raise RuntimeError("feature build produced no rows")

    label_bars = (
        regular_hours_only(bars[bars["symbol"].isin(settings.universe)])
        .merge(features[["symbol", "timestamp", "atr"]], on=["symbol", "timestamp"], how="inner")
        .sort_values(["symbol", "timestamp"])
        .reset_index(drop=True)
    )
    labels = build_labels(label_bars, atr=label_bars["atr"])
    labels[["symbol", "timestamp"]] = label_bars[["symbol", "timestamp"]]

    dataset = features.merge(labels, on=["symbol", "timestamp"], how="inner")

    if out_path is None:
        out_path = DATASET_DIR / "dataset.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_parquet(out_path, index=False)
    logger.info("dataset: %d rows, %d columns -> %s", len(dataset), dataset.shape[1], out_path)
    return dataset

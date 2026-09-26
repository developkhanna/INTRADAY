"""Model training and honest walk-forward evaluation.

Every number reported here comes from data the model never saw during
training. Baselines are trained on the same folds, because a model that cannot
beat "the base rate" or "yesterday's momentum" is not a model.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, log_loss, mean_absolute_error, roc_auc_score

from intraday.config import MODEL_DIR, ensure_dirs
from intraday.features.engineer import feature_columns
from intraday.models.walkforward import walk_forward_splits

logger = logging.getLogger(__name__)

MIN_AUC = 0.52
MIN_FOLDS_BEATING_BASELINE = 0.6

LGB_PARAMS = dict(
    objective="binary",
    learning_rate=0.03,
    num_leaves=63,
    min_child_samples=200,
    subsample=0.8,
    subsample_freq=1,
    colsample_bytree=0.7,
    reg_lambda=5.0,
    n_estimators=400,
    verbose=-1,
)


@dataclass
class FoldResult:
    fold: str
    test_start: str
    test_end: str
    n_train: int
    n_test: int
    base_rate: float
    auc: float
    brier: float
    brier_baseline: float
    log_loss: float
    calibration_error: float

    @property
    def beats_baseline(self) -> bool:
        return self.auc > MIN_AUC and self.brier < self.brier_baseline


@dataclass
class ModelReport:
    target: str
    n_features: int
    folds: list[FoldResult] = field(default_factory=list)

    @property
    def mean_auc(self) -> float:
        return float(np.mean([f.auc for f in self.folds])) if self.folds else float("nan")

    @property
    def status(self) -> str:
        """VALIDATED only if the model beats the baseline out of sample, repeatedly."""
        if not self.folds:
            return "UNVALIDATED"
        share = np.mean([f.beats_baseline for f in self.folds])
        if share >= MIN_FOLDS_BEATING_BASELINE and self.mean_auc > MIN_AUC:
            return "VALIDATED"
        return "UNVALIDATED"

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "n_features": self.n_features,
            "status": self.status,
            "mean_auc": self.mean_auc,
            "folds": [asdict(f) for f in self.folds],
        }


def expected_calibration_error(y_true: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    """How far predicted probabilities sit from observed frequencies."""
    edges = np.linspace(0, 1, bins + 1)
    index = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    error = 0.0
    for b in range(bins):
        mask = index == b
        if not mask.any():
            continue
        error += mask.mean() * abs(p[mask].mean() - y_true[mask].mean())
    return float(error)


def _fit_calibrated(
    x_train: pd.DataFrame, y_train: np.ndarray, calibration_fraction: float = 0.2
) -> tuple[object, IsotonicRegression]:
    """Fit LightGBM on the earlier part of the fold, calibrate on the later part."""
    import lightgbm as lgb

    cut = int(len(x_train) * (1 - calibration_fraction))
    fit_x, fit_y = x_train.iloc[:cut], y_train[:cut]
    cal_x, cal_y = x_train.iloc[cut:], y_train[cut:]

    model = lgb.LGBMClassifier(**LGB_PARAMS)
    model.fit(fit_x, fit_y)

    raw = model.predict_proba(cal_x)[:, 1]
    calibrator = IsotonicRegression(out_of_bounds="clip")
    calibrator.fit(raw, cal_y)
    return model, calibrator


def evaluate_target(
    dataset: pd.DataFrame,
    target: str,
    features: Sequence[str] | None = None,
    n_splits: int = 6,
    label_horizon_minutes: int = 60,
) -> ModelReport:
    """Walk-forward evaluation of one binary target."""
    features = list(features or feature_columns(dataset))
    frame = dataset.dropna(subset=[target]).reset_index(drop=True)
    frame = frame[frame[features].notna().mean(axis=1) > 0.8].reset_index(drop=True)

    x = frame[features]
    y = frame[target].to_numpy(dtype=float)
    report = ModelReport(target=target, n_features=len(features))

    for split in walk_forward_splits(
        frame["timestamp"], n_splits=n_splits, label_horizon_minutes=label_horizon_minutes
    ):
        model, calibrator = _fit_calibrated(x.iloc[split.train], y[split.train])
        raw = model.predict_proba(x.iloc[split.test])[:, 1]
        probability = calibrator.predict(raw)

        y_test = y[split.test]
        base_rate = float(y[split.train].mean())
        if len(np.unique(y_test)) < 2:
            continue
        report.folds.append(
            FoldResult(
                fold=split.name,
                test_start=str(split.test_start.date()),
                test_end=str(split.test_end.date()),
                n_train=len(split.train),
                n_test=len(split.test),
                base_rate=float(y_test.mean()),
                auc=float(roc_auc_score(y_test, probability)),
                brier=float(brier_score_loss(y_test, probability)),
                brier_baseline=float(
                    brier_score_loss(y_test, np.full(len(y_test), base_rate))
                ),
                log_loss=float(log_loss(y_test, np.clip(probability, 1e-6, 1 - 1e-6))),
                calibration_error=expected_calibration_error(y_test, probability),
            )
        )
        logger.info("%s %s auc=%.4f", target, split.name, report.folds[-1].auc)

    return report


def evaluate_regression_target(
    dataset: pd.DataFrame,
    target: str,
    features: Sequence[str] | None = None,
    n_splits: int = 6,
) -> dict:
    """Walk-forward evaluation of a continuous target (MFE, MAE, forward return)."""
    import lightgbm as lgb

    features = list(features or feature_columns(dataset))
    frame = dataset.dropna(subset=[target]).reset_index(drop=True)
    x, y = frame[features], frame[target].to_numpy(dtype=float)

    folds = []
    for split in walk_forward_splits(frame["timestamp"], n_splits=n_splits):
        params = {**LGB_PARAMS, "objective": "regression"}
        model = lgb.LGBMRegressor(**params)
        model.fit(x.iloc[split.train], y[split.train])
        prediction = model.predict(x.iloc[split.test])
        y_test = y[split.test]
        naive = np.full(len(y_test), y[split.train].mean())
        folds.append(
            {
                "fold": split.name,
                "test_start": str(split.test_start.date()),
                "mae": float(mean_absolute_error(y_test, prediction)),
                "mae_baseline": float(mean_absolute_error(y_test, naive)),
                "n_test": int(len(y_test)),
            }
        )
    beats = [f["mae"] < f["mae_baseline"] for f in folds]
    return {
        "target": target,
        "folds": folds,
        "status": (
            "VALIDATED"
            if folds and np.mean(beats) >= MIN_FOLDS_BEATING_BASELINE
            else "UNVALIDATED"
        ),
    }


def train_production_model(
    dataset: pd.DataFrame, target: str, features: Sequence[str] | None = None
) -> dict:
    """Refit on all available history for live use, after validation has run."""
    ensure_dirs()
    features = list(features or feature_columns(dataset))
    frame = dataset.dropna(subset=[target]).reset_index(drop=True)
    model, calibrator = _fit_calibrated(frame[features], frame[target].to_numpy(dtype=float))

    import joblib

    path = MODEL_DIR / f"{target}.joblib"
    joblib.dump({"model": model, "calibrator": calibrator, "features": features}, path)
    return {"target": target, "path": str(path), "n_rows": len(frame)}


def write_report(reports: list[dict], path: Path | None = None) -> Path:
    ensure_dirs()
    path = path or MODEL_DIR / "validation_report.json"
    path.write_text(json.dumps(reports, indent=2))
    return path

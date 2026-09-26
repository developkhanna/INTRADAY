"""Walk-forward validation, then (only for validated targets) a production refit."""

from __future__ import annotations

import argparse
import json
import logging

import pandas as pd

from intraday.config import DATASET_DIR
from intraday.models.train import (
    evaluate_regression_target,
    evaluate_target,
    train_production_model,
    write_report,
)

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(DATASET_DIR / "dataset.parquet"))
    parser.add_argument("--targets", nargs="*", default=None, help="binary target columns")
    parser.add_argument("--splits", type=int, default=6)
    parser.add_argument("--fit-production", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dataset = pd.read_parquet(args.dataset)

    targets = args.targets or [c for c in dataset.columns if c.startswith("tbs_")]
    reports = []
    for target in targets:
        report = evaluate_target(dataset, target, n_splits=args.splits)
        reports.append(report.to_dict())
        logger.info("%s: %s mean_auc=%.4f", target, report.status, report.mean_auc)
        if args.fit_production and report.status == "VALIDATED":
            train_production_model(dataset, target)

    for target in [c for c in dataset.columns if c.startswith(("mfe_", "mae_", "fwd_ret_"))]:
        reports.append(evaluate_regression_target(dataset, target, n_splits=args.splits))

    path = write_report(reports)
    print(json.dumps([{k: r[k] for k in ("target", "status")} for r in reports], indent=2))
    print(f"report -> {path}")


if __name__ == "__main__":
    main()

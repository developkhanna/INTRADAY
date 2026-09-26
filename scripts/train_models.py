"""Walk-forward validation, then a production refit.

A refit model is only allowed to speak, not to trade: the live loop reads each
target's status out of the validation report, so an UNVALIDATED target can be
recorded in the ledger every minute while still being refused a BUY. Fitting
those models is what makes the record-before-the-outcome evidence possible.
"""

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
    parser.add_argument(
        "--fit-unvalidated",
        action="store_true",
        help="refit every target, not only validated ones (status is unchanged)",
    )
    parser.add_argument(
        "--fit-only",
        action="store_true",
        help="skip evaluation and refit from the existing validation report",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dataset = pd.read_parquet(args.dataset)

    targets = args.targets or [c for c in dataset.columns if c.startswith("tbs_")]

    if args.fit_only:
        for target in targets:
            logger.info("refitting %s on all history", target)
            print(json.dumps(train_production_model(dataset, target)))
        return

    reports = []
    for target in targets:
        report = evaluate_target(dataset, target, n_splits=args.splits)
        reports.append(report.to_dict())
        logger.info("%s: %s mean_auc=%.4f", target, report.status, report.mean_auc)
        if args.fit_production and (args.fit_unvalidated or report.status == "VALIDATED"):
            train_production_model(dataset, target)

    for target in [c for c in dataset.columns if c.startswith(("mfe_", "mae_", "fwd_ret_"))]:
        reports.append(evaluate_regression_target(dataset, target, n_splits=args.splits))

    path = write_report(reports)
    print(json.dumps([{k: r[k] for k in ("target", "status")} for r in reports], indent=2))
    print(f"report -> {path}")


if __name__ == "__main__":
    main()

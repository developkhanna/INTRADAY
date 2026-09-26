"""Build the modelling table from locally stored bars."""

from __future__ import annotations

import argparse
import datetime as dt
import logging

from intraday.config import settings_from_watchlist
from intraday.dataset import build_dataset


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dataset = build_dataset(settings=settings_from_watchlist(), start=args.start, end=args.end)
    print(f"{len(dataset):,} rows x {dataset.shape[1]} columns")


if __name__ == "__main__":
    main()

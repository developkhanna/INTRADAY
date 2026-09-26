"""Download historical 1-minute bars for the whole universe."""

from __future__ import annotations

import argparse
import logging

from intraday.data.sync import sync_history


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=float, default=3.0)
    parser.add_argument("--symbols", nargs="*", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    coverage = sync_history(years=args.years, symbols=args.symbols)
    print(coverage.to_string(index=False))


if __name__ == "__main__":
    main()

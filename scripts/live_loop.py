"""Run the live prediction loop during the US session.

Predictions are recorded before the outcome is known; outcomes are attached
once each horizon has elapsed. Nothing here places an order.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import time

import pandas as pd

from intraday.config import MARKET_TZ, SESSION_CLOSE_MIN, SESSION_OPEN_MIN, settings_from_watchlist
from intraday.health import record_live_tick
from intraday.live.outcomes import attach_outcomes
from intraday.live.predictor import run_once

logger = logging.getLogger(__name__)


def market_is_open(now: dt.datetime | None = None) -> bool:
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return SESSION_OPEN_MIN <= minutes < SESSION_CLOSE_MIN


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", nargs="*", default=["tbs_t1_s0p5_15m", "tbs_t1_s0p5_30m"])
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--exit-when-closed",
        action="store_true",
        help="stop as soon as the session is over; the scheduler starts it again",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = settings_from_watchlist()

    while True:
        if market_is_open():
            try:
                frame = run_once(args.targets, settings=settings)
                logger.info("recorded %d predictions", len(frame))
                attach_outcomes(now=pd.Timestamp.now(tz="UTC"))
                record_live_tick("recording predictions", predictions=len(frame))
            except Exception:  # keep the loop alive across transient API errors
                logger.exception("prediction cycle failed")
                record_live_tick("last cycle failed, will retry")
        else:
            logger.info("market closed")
            record_live_tick("market closed")
            if args.exit_when_closed:
                return
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

"""The arithmetic in the briefing pack, checked against hand-computed numbers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

from intraday.config import MARKET_TZ
from intraday.positions import Position
from intraday.risk import RiskProfile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "premarket_brief.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("premarket_brief", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


brief = _load_module()


def _minutes(day: str, prices: list[float], start: str = "04:00") -> pd.DataFrame:
    index = pd.date_range(f"{day} {start}", periods=len(prices), freq="1min", tz=MARKET_TZ)
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p * 1.001 for p in prices],
            "low": [p * 0.999 for p in prices],
            "close": prices,
            "volume": [1_000.0] * len(prices),
        },
        index=index,
    )


def test_daily_stats_uses_the_last_close_and_20_day_normals():
    index = pd.date_range("2026-08-01", periods=30, freq="B", tz=MARKET_TZ)
    daily = pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1_000_000.0,
        },
        index=index,
    )
    stats = brief.daily_stats(daily)
    assert stats["prior_close"] == 100.0
    assert stats["avg_volume_20d"] == 1_000_000.0
    assert stats["avg_day_range_pct"] == pytest.approx(0.02)
    assert stats["off_20d_high_pct"] == pytest.approx(100 / 101 - 1)


def test_premarket_stats_measures_the_gap_against_the_prior_close():
    minutes = _minutes("2026-09-28", [100.0, 101.0, 102.0])
    stats = brief.premarket_stats(minutes, pd.Timestamp("2026-09-28").date(), 100.0)
    assert stats["premarket_last"] == 102.0
    assert stats["gap_pct"] == pytest.approx(0.02)
    assert stats["premarket_bars"] == 3


def test_premarket_stats_ignores_regular_hours_bars():
    minutes = _minutes("2026-09-28", [100.0] * 5, start="09:30")
    assert brief.premarket_stats(minutes, pd.Timestamp("2026-09-28").date(), 100.0) == {}


def test_candidate_levels_size_stop_and_target_are_consistent():
    risk = RiskProfile()
    levels = brief.candidate_levels(
        {"premarket_last": 100.0, "minute_range_pct": 0.005, "avg_volume_20d": 1_000.0,
         "premarket_volume": 100.0},
        risk,
    )
    assert levels["shares_for_position_size"] == 10  # $1,000 at $100
    assert levels["working_stop_pct"] == pytest.approx(0.005)
    assert levels["working_stop_price"] == pytest.approx(99.5)
    assert levels["target_2r_price"] == pytest.approx(101.0)
    assert levels["hard_exit_price"] == pytest.approx(80.0)
    assert levels["relative_volume_premarket"] == pytest.approx(0.1)


def test_candidate_levels_clamps_a_stop_that_noise_would_trigger():
    levels = brief.candidate_levels(
        {"prior_close": 50.0, "minute_range_pct": 0.00001}, RiskProfile()
    )
    assert levels["working_stop_pct"] == pytest.approx(RiskProfile().min_working_stop_pct)


def test_positions_are_ranked_by_room_left_to_the_hard_exit():
    rows = {
        "SAFE": {"premarket_last": 100.0},
        "NEAR": {"premarket_last": 82.0},
    }
    positions = [Position("SAFE", 10, 100.0), Position("NEAR", 10, 100.0)]
    enriched = brief.enrich_positions(rows, positions, RiskProfile())
    assert [row["symbol"] for row in enriched] == ["NEAR", "SAFE"]
    assert enriched[0]["hard_exit_price"] == pytest.approx(80.0)
    assert enriched[0]["room_to_hard_exit_pct"] == pytest.approx(0.025)
    assert enriched[1]["unrealized_dollars"] == pytest.approx(0.0)

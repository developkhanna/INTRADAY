from __future__ import annotations

import pytest

from intraday.decide import CostModel, breakeven_probability, decide, expected_value, position_size


def test_breakeven_requires_more_than_the_naive_odds() -> None:
    """Costs push the required win rate above the pure payoff ratio."""
    naive = breakeven_probability(0.01, 0.005, cost_pct=0.0)
    with_costs = breakeven_probability(0.01, 0.005, cost_pct=0.001)
    assert naive == pytest.approx(1 / 3)
    assert with_costs > naive


def test_unvalidated_model_never_produces_a_buy() -> None:
    idea = decide("AAPL", 15, probability=0.95, target_pct=0.01, stop_pct=0.005,
                  cost_pct=0.0002, model_status="UNVALIDATED")
    assert idea.action == "NO TRADE"
    assert "coin flip" in idea.eli5


def test_thin_liquidity_blocks_the_trade() -> None:
    idea = decide("XYZ", 15, probability=0.9, target_pct=0.01, stop_pct=0.005,
                  cost_pct=0.004, model_status="VALIDATED", liquidity_ok=False)
    assert idea.action == "NO TRADE"


def test_positive_edge_produces_a_buy_and_negative_edge_does_not() -> None:
    good = decide("AAPL", 15, 0.60, 0.01, 0.005, 0.0002, "VALIDATED")
    bad = decide("AAPL", 15, 0.30, 0.01, 0.005, 0.0002, "VALIDATED")
    assert good.action == "BUY" and good.expected_value_pct > 0
    assert bad.action == "NO TRADE" and bad.expected_value_pct < 0


def test_costs_can_flip_a_winning_probability_into_no_trade() -> None:
    """Same probability, wider spread: the edge disappears."""
    cheap = decide("AAPL", 15, 0.40, 0.002, 0.001, 0.0001, "VALIDATED")
    expensive = decide("AAPL", 15, 0.40, 0.002, 0.001, 0.0020, "VALIDATED")
    assert cheap.expected_value_pct > expensive.expected_value_pct
    assert expensive.action != "BUY"


def test_round_trip_cost_grows_with_the_spread() -> None:
    model = CostModel()
    tight = model.round_trip(price=200.0, spread_pct=0.0001, shares=100)
    wide = model.round_trip(price=200.0, spread_pct=0.0030, shares=100)
    assert 0 < tight < wide


def test_position_size_caps_the_loss_at_the_stop() -> None:
    shares = position_size(
        account_equity=50_000, risk_per_trade_pct=0.005, price=100.0, stop_pct=0.01
    )
    assert shares * 100.0 * 0.01 <= 50_000 * 0.005 + 1e-9


def test_expected_value_is_linear_in_probability() -> None:
    low = expected_value(0.4, 0.01, 0.005, 0.0)
    high = expected_value(0.5, 0.01, 0.005, 0.0)
    assert high - low == pytest.approx(0.015 * 0.1)

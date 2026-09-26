"""Cost model and the prediction -> action layer.

A prediction is a statement about the market. An action is a statement about
your money. They are deliberately kept apart: the model outputs probabilities,
and this module decides whether those probabilities survive contact with
spread, slippage and commission.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

IBKR_PER_SHARE = 0.0035
IBKR_MIN_TICKET = 0.35
IBKR_MAX_PCT_OF_TRADE = 0.01


@dataclass(frozen=True)
class CostModel:
    """Conservative round-trip cost, as a fraction of notional.

    Assumes you cross the spread on the way in and on the way out, which is
    the pessimistic case for a market order.
    """

    slippage_ticks: float = 1.0
    tick_size: float = 0.01
    spread_multiplier: float = 1.0

    def round_trip(self, price: float, spread_pct: float, shares: int) -> float:
        spread_cost = self.spread_multiplier * spread_pct
        slippage_cost = 2 * self.slippage_ticks * self.tick_size / price
        commission = 2 * min(
            max(shares * IBKR_PER_SHARE, IBKR_MIN_TICKET),
            IBKR_MAX_PCT_OF_TRADE * shares * price,
        )
        commission_pct = commission / (shares * price) if shares and price else 0.0
        return float(spread_cost + slippage_cost + commission_pct)


@dataclass(frozen=True)
class TradeIdea:
    symbol: str
    horizon_minutes: int
    probability: float
    target_pct: float
    stop_pct: float
    cost_pct: float
    action: str
    expected_value_pct: float
    breakeven_probability: float
    reason: str
    eli5: str
    model_status: str


def breakeven_probability(
    target_pct: float, stop_pct: float, cost_pct: float
) -> float:
    """Win rate required just to break even after costs."""
    denominator = target_pct + stop_pct
    if denominator <= 0:
        return float("nan")
    return float((stop_pct + cost_pct) / denominator)


def expected_value(
    probability: float, target_pct: float, stop_pct: float, cost_pct: float
) -> float:
    return float(probability * target_pct - (1 - probability) * stop_pct - cost_pct)


def decide(
    symbol: str,
    horizon_minutes: int,
    probability: float,
    target_pct: float,
    stop_pct: float,
    cost_pct: float,
    model_status: str,
    liquidity_ok: bool = True,
    min_edge_pct: float = 0.0005,
    trigger_probability: float = 0.45,
) -> TradeIdea:
    """Turn one calibrated probability into an action, or refuse to."""
    ev = expected_value(probability, target_pct, stop_pct, cost_pct)
    breakeven = breakeven_probability(target_pct, stop_pct, cost_pct)

    if model_status != "VALIDATED":
        action = "NO TRADE"
        reason = "model has not beaten its baseline out of sample"
        eli5 = (
            "We do not have proof this prediction is better than a coin flip yet, "
            "so we are not risking money on it."
        )
    elif not liquidity_ok:
        action = "NO TRADE"
        reason = "spread or volume too thin for this size"
        eli5 = "The toll to get in and out of this stock eats the whole prize."
    elif ev >= min_edge_pct:
        action = "BUY"
        reason = (
            f"edge {ev:.4%} after costs, needs {breakeven:.1%} win rate, "
            f"model says {probability:.1%}"
        )
        eli5 = (
            f"Out of 100 times we've seen a setup like this, about {probability * 100:.0f} "
            f"hit the +{target_pct:.2%} target before the -{stop_pct:.2%} stop. "
            f"We only need {breakeven * 100:.0f} to break even after fees, "
            "so the odds are on our side."
        )
    elif probability >= trigger_probability:
        action = "BUY ON TRIGGER"
        reason = "close to profitable; wait for a better price or confirmation"
        eli5 = (
            "Almost worth it, but not quite. If the price dips a little or the move "
            "confirms, the same trade becomes profitable — so we wait for that."
        )
    else:
        action = "NO TRADE"
        reason = f"expected value {ev:.4%} below the {min_edge_pct:.2%} minimum edge"
        eli5 = "After the spread and fees, this trade loses money on average. We skip it."

    return TradeIdea(
        symbol=symbol,
        horizon_minutes=horizon_minutes,
        probability=float(probability),
        target_pct=float(target_pct),
        stop_pct=float(stop_pct),
        cost_pct=float(cost_pct),
        action=action,
        expected_value_pct=ev,
        breakeven_probability=breakeven,
        reason=reason,
        eli5=eli5,
        model_status=model_status,
    )


def position_size(
    account_equity: float, risk_per_trade_pct: float, price: float, stop_pct: float
) -> int:
    """Shares such that hitting the stop loses at most `risk_per_trade_pct` of equity."""
    if price <= 0 or stop_pct <= 0:
        return 0
    risk_dollars = account_equity * risk_per_trade_pct
    return int(np.floor(risk_dollars / (price * stop_pct)))

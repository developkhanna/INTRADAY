"""Positions you already hold, and what the engine suggests doing with them.

These are not the engine's trades: it never opened them, so it has no entry
thesis to fall back on. It therefore judges them only on where they are now
relative to your hard loss ceiling, plus whatever the model currently expects.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from intraday.config import DATA_DIR, ensure_dirs
from intraday.risk import RiskProfile

POSITIONS_FILE = DATA_DIR / "positions.json"


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: float
    avg_price: float

    def cost_basis(self) -> float:
        return self.quantity * self.avg_price

    def unrealized_pct(self, price: float) -> float:
        if self.avg_price <= 0:
            return 0.0
        return price / self.avg_price - 1.0

    def unrealized_dollars(self, price: float) -> float:
        return (price - self.avg_price) * self.quantity


@dataclass(frozen=True)
class PositionView:
    symbol: str
    quantity: float
    avg_price: float
    price: float
    unrealized_pct: float
    unrealized_dollars: float
    hard_stop_price: float
    distance_to_hard_stop_pct: float
    status: str
    urgency: int
    action: str
    reason: str
    eli5: str
    probability: float | None
    model_status: str


def review(
    position: Position,
    price: float,
    risk: RiskProfile,
    probability: float | None = None,
    model_status: str = "UNVALIDATED",
    hold_threshold: float = 0.5,
    watch_drawdown_pct: float = 0.02,
) -> PositionView:
    """Judge one held position: EXIT NOW, SELL, TRIM, or HOLD."""
    pnl_pct = position.unrealized_pct(price)
    hard_stop_price = position.avg_price * (1 - risk.hard_stop_pct)
    usable = model_status == "VALIDATED" and probability is not None

    if price <= hard_stop_price:
        action = "EXIT NOW"
        reason = f"down {pnl_pct:.1%}, at or past the {risk.hard_stop_pct:.0%} hard ceiling"
        eli5 = (
            "This is the line you said you would never cross. We are at it, so the "
            "position closes regardless of what anyone thinks happens next."
        )
    elif usable and probability is not None and probability < 1 - hold_threshold:
        action = "SELL"
        reason = f"model gives only {probability:.0%} odds of going up from here"
        eli5 = (
            "The model thinks this is more likely to keep falling than to recover, "
            "so holding it is a bet against our own numbers."
        )
    elif usable and probability is not None and probability >= hold_threshold:
        action = "HOLD"
        reason = f"model gives {probability:.0%} odds of going up from here"
        eli5 = "Our numbers still lean in your favour here, so there is no reason to sell."
    else:
        action = "HOLD"
        reason = "no validated model view; holding per your instruction"
        eli5 = (
            "We have no proven read on this one yet. You said you would rather wait "
            f"than take a loss, so we hold and watch the {risk.hard_stop_pct:.0%} line at "
            f"${hard_stop_price:.2f}."
        )

    if action == "EXIT NOW":
        status, urgency = "EXIT NOW", 3
    elif action == "SELL":
        status, urgency = "DECIDE", 2
    elif pnl_pct <= -watch_drawdown_pct:
        status, urgency = "WATCHING", 1
    else:
        status, urgency = "OK", 0

    distance_to_hard_stop = (
        price / hard_stop_price - 1.0 if hard_stop_price > 0 else float("nan")
    )

    return PositionView(
        symbol=position.symbol,
        quantity=position.quantity,
        avg_price=position.avg_price,
        price=float(price),
        unrealized_pct=float(pnl_pct),
        unrealized_dollars=float(position.unrealized_dollars(price)),
        hard_stop_price=float(hard_stop_price),
        distance_to_hard_stop_pct=float(distance_to_hard_stop),
        status=status,
        urgency=urgency,
        action=action,
        reason=reason,
        eli5=eli5,
        probability=None if probability is None else float(probability),
        model_status=model_status,
    )


def load_positions() -> list[Position]:
    if not POSITIONS_FILE.exists():
        return []
    raw = json.loads(POSITIONS_FILE.read_text())
    return [
        Position(
            symbol=str(item["symbol"]).strip().upper(),
            quantity=float(item["quantity"]),
            avg_price=float(item["avg_price"]),
        )
        for item in raw
        if item.get("symbol")
    ]


def save_positions(positions: list[Position]) -> list[Position]:
    ensure_dirs()
    POSITIONS_FILE.write_text(json.dumps([asdict(p) for p in positions], indent=2))
    return positions

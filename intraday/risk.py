"""Risk limits: how much may be lost per trade and per day, and how big a trade is."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

from intraday.config import DATA_DIR, ensure_dirs

RISK_FILE = DATA_DIR / "risk.json"


@dataclass(frozen=True)
class RiskProfile:
    """User-set risk limits.

    ``hard_stop_pct`` is the absolute ceiling on a losing position, the point at
    which the position is exited regardless of the model's view.
    ``working_stop_atr`` is the normal exit, expressed in units of the symbol's
    own one-minute volatility so it survives noise.
    """

    account_equity: float = 25_000.0
    position_size: float = 1_000.0
    hard_stop_pct: float = 0.20
    working_stop_atr: float = 1.0
    max_working_stop_pct: float = 0.01
    min_working_stop_pct: float = 0.003
    daily_loss_limit_pct: float = 0.03
    max_open_positions: int = 5
    honor_stop_on_model_trades: bool = True

    @property
    def hard_stop_dollars(self) -> float:
        return self.position_size * self.hard_stop_pct

    @property
    def daily_loss_limit_dollars(self) -> float:
        return self.account_equity * self.daily_loss_limit_pct

    def working_stop_pct(self, atr_pct: float) -> float:
        """Clamp the volatility-based stop into a sane band."""
        if not math.isfinite(atr_pct) or atr_pct <= 0:
            return self.min_working_stop_pct
        raw = atr_pct * self.working_stop_atr
        return min(max(raw, self.min_working_stop_pct), self.max_working_stop_pct)

    def shares_for(self, price: float) -> int:
        if price <= 0:
            return 0
        return int(self.position_size // price)


def load_risk() -> RiskProfile:
    if RISK_FILE.exists():
        stored = json.loads(RISK_FILE.read_text())
        known = {f: stored[f] for f in RiskProfile.__dataclass_fields__ if f in stored}
        return RiskProfile(**known)
    return RiskProfile()


def save_risk(profile: RiskProfile) -> RiskProfile:
    ensure_dirs()
    RISK_FILE.write_text(json.dumps(asdict(profile), indent=2))
    return profile

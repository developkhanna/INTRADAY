import pytest

from intraday.positions import Position, review
from intraday.risk import RiskProfile


@pytest.fixture
def risk() -> RiskProfile:
    return RiskProfile(account_equity=25_000, position_size=1_000, hard_stop_pct=0.20)


def test_working_stop_tracks_volatility_but_stays_in_band(risk: RiskProfile) -> None:
    assert risk.working_stop_pct(0.006) == pytest.approx(0.006)
    assert risk.working_stop_pct(0.05) == pytest.approx(risk.max_working_stop_pct)
    assert risk.working_stop_pct(0.0001) == pytest.approx(risk.min_working_stop_pct)
    assert risk.working_stop_pct(float("nan")) == pytest.approx(risk.min_working_stop_pct)


def test_dollar_limits(risk: RiskProfile) -> None:
    assert risk.hard_stop_dollars == pytest.approx(200.0)
    assert risk.daily_loss_limit_dollars == pytest.approx(625.0)
    assert risk.shares_for(250.0) == 4


def test_defaults_match_the_owners_account() -> None:
    profile = RiskProfile()
    assert profile.account_equity == 40_000.0
    assert profile.position_size == 1_000.0
    assert profile.daily_loss_limit_dollars == pytest.approx(1_000.0)
    assert profile.hard_stop_pct == 0.20
    assert profile.max_open_positions == 5
    assert profile.honor_stop_on_model_trades is True


def test_hard_ceiling_forces_exit(risk: RiskProfile) -> None:
    position = Position("ZS", quantity=10, avg_price=200.0)
    view = review(position, price=160.0, risk=risk, probability=0.9, model_status="VALIDATED")
    assert view.action == "EXIT NOW"
    assert view.hard_stop_price == pytest.approx(160.0)


def test_unvalidated_model_cannot_trigger_a_sell(risk: RiskProfile) -> None:
    position = Position("ZS", quantity=10, avg_price=200.0)
    view = review(position, price=190.0, risk=risk, probability=0.1, model_status="UNVALIDATED")
    assert view.action == "HOLD"
    assert "no proven read" in view.eli5


def test_validated_bearish_model_sells(risk: RiskProfile) -> None:
    position = Position("ZS", quantity=10, avg_price=200.0)
    view = review(position, price=190.0, risk=risk, probability=0.2, model_status="VALIDATED")
    assert view.action == "SELL"
    assert view.unrealized_pct == pytest.approx(-0.05)
    assert view.unrealized_dollars == pytest.approx(-100.0)


def test_validated_bullish_model_holds(risk: RiskProfile) -> None:
    position = Position("ZS", quantity=10, avg_price=200.0)
    view = review(position, price=190.0, risk=risk, probability=0.65, model_status="VALIDATED")
    assert view.action == "HOLD"


def test_status_escalates_with_drawdown(risk: RiskProfile) -> None:
    position = Position("ZS", quantity=10, avg_price=200.0)
    assert review(position, 201.0, risk).status == "OK"
    assert review(position, 190.0, risk).status == "WATCHING"
    decide = review(position, 190.0, risk, probability=0.2, model_status="VALIDATED")
    assert (decide.status, decide.urgency) == ("DECIDE", 2)
    assert review(position, 150.0, risk).status == "EXIT NOW"

from __future__ import annotations

from datetime import UTC, datetime

from core.models import EventFlag, EventType, PositionSnapshot, Signal
from execution.market_constraints import MarketConstraintInput, MarketConstraintValidator
from tests.test_execution.test_bootstrap import build_settings


def _buy_signal() -> Signal:
    return Signal(
        ticker="005930",
        market="KR",
        action="buy",
        strategy="trend_following",
        strength=1.0,
        reason="entry",
    )


def _sell_signal() -> Signal:
    return Signal(
        ticker="005930",
        market="KR",
        action="sell",
        strategy="trend_following",
        strength=1.0,
        reason="exit",
        is_exit=True,
    )


def test_market_constraints_rejects_kr_price_limit_break(tmp_path) -> None:
    validator = MarketConstraintValidator(build_settings(tmp_path))

    decision = validator.evaluate(
        MarketConstraintInput(
            signal=_buy_signal(),
            quantity=1,
            current_price=131_000,
            previous_close=100_000,
            as_of=datetime(2026, 4, 24, 1, 0, tzinfo=UTC),
            cash_available=1_000_000,
        )
    )

    assert decision.approved is False
    assert decision.reason == "kr_price_limit"


def test_market_constraints_rejects_auction_session(tmp_path) -> None:
    validator = MarketConstraintValidator(build_settings(tmp_path))

    decision = validator.evaluate(
        MarketConstraintInput(
            signal=_buy_signal(),
            quantity=1,
            current_price=70_000,
            previous_close=70_000,
            as_of=datetime(2026, 4, 24, 6, 25, tzinfo=UTC),
            cash_available=1_000_000,
        )
    )

    assert decision.approved is False
    assert decision.reason == "kr_auction_session"


def test_market_constraints_rejects_short_sell(tmp_path) -> None:
    validator = MarketConstraintValidator(build_settings(tmp_path))
    position = PositionSnapshot(
        ticker="005930",
        market="KR",
        strategy="trend_following",
        quantity=3,
        avg_cost=70_000,
        current_price=71_000,
        highest_price=72_000,
        entry_date=datetime(2026, 4, 1, tzinfo=UTC),
    )

    decision = validator.evaluate(
        MarketConstraintInput(
            signal=_sell_signal(),
            quantity=4,
            current_price=71_000,
            previous_close=70_000,
            as_of=datetime(2026, 4, 24, 1, 0, tzinfo=UTC),
            position=position,
        )
    )

    assert decision.approved is False
    assert decision.reason == "short_sell_blocked"


def test_market_constraints_rejects_overheated_buy(tmp_path) -> None:
    validator = MarketConstraintValidator(build_settings(tmp_path))

    decision = validator.evaluate(
        MarketConstraintInput(
            signal=_buy_signal(),
            quantity=1,
            current_price=70_000,
            previous_close=70_000,
            as_of=datetime(2026, 4, 24, 1, 0, tzinfo=UTC),
            cash_available=1_000_000,
            event_flags=[
                EventFlag(
                    event_type=EventType.KR_OVERHEATED,
                    market="KR",
                    ticker="005930",
                    metadata={"action": "exclude_universe"},
                )
            ],
        )
    )

    assert decision.approved is False
    assert decision.reason == "kr_overheated"


def test_market_constraints_rejects_settlement_cash_shortfall(tmp_path) -> None:
    validator = MarketConstraintValidator(build_settings(tmp_path))

    decision = validator.evaluate(
        MarketConstraintInput(
            signal=_buy_signal(),
            quantity=10,
            current_price=70_000,
            previous_close=70_000,
            as_of=datetime(2026, 4, 24, 1, 0, tzinfo=UTC),
            cash_available=600_000,
        )
    )

    assert decision.approved is False
    assert decision.reason == "settlement_cash_unavailable"

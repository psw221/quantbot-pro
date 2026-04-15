from __future__ import annotations

from datetime import UTC, datetime

from core.models import EventFlag, EventType, PositionSnapshot, Signal, SizingInput
from risk.event_filter import EventFilter
from risk.exit_manager import ExitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from strategy.signal_resolver import SignalResolver
from tests.test_execution.test_bootstrap import build_settings


def test_event_filter_blocks_us_buy_on_fomc(tmp_path) -> None:
    event_filter = EventFilter(build_settings(tmp_path))
    decision = event_filter.evaluate_signal(
        Signal(ticker="AAPL", market="US", action="buy", strategy="dual_momentum", strength=1.0, reason="entry"),
        [EventFlag(event_type=EventType.FOMC, market="US")],
    )

    assert decision.approved is False
    assert decision.reason == "blocked_by_fomc"


def test_event_filter_scales_us_risk_on_vix(tmp_path) -> None:
    event_filter = EventFilter(build_settings(tmp_path))
    decision = event_filter.evaluate_signal(
        Signal(ticker="AAPL", market="US", action="buy", strategy="trend_following", strength=1.0, reason="entry"),
        [EventFlag(event_type=EventType.VIX_HIGH, market="US")],
    )

    assert decision.approved is True
    assert decision.scale_factor == 0.5


def test_exit_manager_detects_trailing_stop(tmp_path) -> None:
    manager = ExitManager(build_settings(tmp_path))
    position = PositionSnapshot(
        ticker="005930",
        market="KR",
        strategy="trend_following",
        quantity=10,
        avg_cost=70000,
        current_price=80000,
        highest_price=100000,
        entry_date=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert manager.trailing_stop_breached(position, 89000) is True


def test_risk_manager_integrates_event_gate(tmp_path) -> None:
    manager = RiskManager(build_settings(tmp_path))
    decision = manager.evaluate_signal(
        Signal(ticker="005930", market="KR", action="buy", strategy="dual_momentum", strength=1.0, reason="entry"),
        current_price=70000,
        event_flags=[EventFlag(event_type=EventType.BOK, market="KR")],
    )

    assert decision.approved is False
    assert decision.reason == "blocked_by_bok"


def test_position_sizer_applies_volatility_scaling_and_min_threshold(tmp_path) -> None:
    sizer = PositionSizer(build_settings(tmp_path))
    small = sizer.size_position(
        SizingInput(
            ticker="AAPL",
            market="US",
            strategy="trend_following",
            cash_available=100000,
            price=1000,
            volatility=0.60,
            target_volatility=0.13,
            min_position_fraction=0.02,
        )
    )
    large = sizer.size_position(
        SizingInput(
            ticker="AAPL",
            market="US",
            strategy="trend_following",
            cash_available=100000,
            price=50,
            volatility=0.20,
            target_volatility=0.13,
            min_position_fraction=0.01,
        )
    )

    assert small.quantity == 0
    assert small.reason == "below_min_position"
    assert large.quantity > 0
    assert large.volatility_scale < 1.0


def test_signal_resolver_blocks_same_day_rebuy_after_stop_exit() -> None:
    resolver = SignalResolver()
    signals = [
        Signal(
            ticker="AAPL",
            market="US",
            action="buy",
            strategy="dual_momentum",
            strength=0.8,
            reason="rebalance",
        ),
        Signal(
            ticker="AAPL",
            market="US",
            action="sell",
            strategy="trend_following",
            strength=0.4,
            reason="stop",
            is_exit=True,
            metadata={"exit_reason": "stop_loss"},
        ),
    ]

    resolved = resolver.resolve(signals)

    assert len(resolved) == 1
    assert resolved[0].action == "sell"
    assert resolved[0].metadata["same_day_rebuy_blocked"] is True

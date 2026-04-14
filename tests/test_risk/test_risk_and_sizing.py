from datetime import datetime, timezone

from core.models import PositionSnapshot, Signal, SizingInput
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from tests.test_execution.test_bootstrap import build_settings


def test_risk_manager_blocks_on_daily_loss(tmp_path) -> None:
    manager = RiskManager(build_settings(tmp_path))
    decision = manager.evaluate_signal(
        Signal(ticker="005930", market="KR", action="buy", strategy="dual_momentum", strength=1.0, reason="entry"),
        current_price=70000,
        daily_pnl_pct=-0.03,
    )

    assert decision.approved is False
    assert decision.reason == "daily_loss_limit"


def test_position_sizer_caps_single_stock_exposure(tmp_path) -> None:
    sizer = PositionSizer(build_settings(tmp_path))
    decision = sizer.size_position(
        SizingInput(
            ticker="AAPL",
            market="US",
            strategy="factor_investing",
            cash_available=100000,
            price=1000,
            volatility=0.2,
        )
    )

    assert decision.quantity == 3
    assert decision.capped is True


def test_risk_manager_blocks_buy_after_stop_loss_breach(tmp_path) -> None:
    manager = RiskManager(build_settings(tmp_path))
    position = PositionSnapshot(
        ticker="AAPL",
        market="US",
        strategy="dual_momentum",
        quantity=10,
        avg_cost=100,
        current_price=90,
        highest_price=110,
        entry_date=datetime.now(timezone.utc),
    )

    decision = manager.evaluate_signal(
        Signal(ticker="AAPL", market="US", action="buy", strategy="dual_momentum", strength=1.0, reason="reenter"),
        current_price=94,
        position=position,
    )

    assert decision.approved is False
    assert decision.reason == "stop_loss_cooldown"

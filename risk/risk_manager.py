from __future__ import annotations

from core.models import PositionSnapshot, RiskDecision, Signal
from core.settings import Settings, get_settings


class RiskManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def evaluate_signal(
        self,
        signal: Signal,
        *,
        current_price: float,
        position: PositionSnapshot | None = None,
        daily_pnl_pct: float = 0.0,
        portfolio_drawdown_pct: float = 0.0,
        blocked: bool = False,
    ) -> RiskDecision:
        if blocked:
            return RiskDecision(approved=False, reason="trading_blocked", tags=["system_block"])

        if daily_pnl_pct <= self.settings.risk.daily_max_loss:
            return RiskDecision(approved=False, reason="daily_loss_limit", tags=["daily_loss"])

        if portfolio_drawdown_pct <= self.settings.risk.max_drawdown_limit:
            return RiskDecision(approved=False, reason="max_drawdown_limit", tags=["drawdown"])

        if signal.action == "sell" and position is not None and position.quantity <= 0:
            return RiskDecision(approved=False, reason="no_position_to_sell", tags=["sell_guard"])

        if signal.action == "buy" and position is not None and position.quantity > 0:
            pnl_pct = 0.0 if position.avg_cost == 0 else (current_price - position.avg_cost) / position.avg_cost
            threshold = (
                self.settings.risk.stop_loss_domestic
                if signal.market == "KR"
                else self.settings.risk.stop_loss_overseas
            )
            if pnl_pct <= threshold:
                return RiskDecision(approved=False, reason="stop_loss_cooldown", tags=["stop_loss"])

        return RiskDecision(approved=True, reason="approved")

from __future__ import annotations

from core.models import PositionSnapshot, Signal
from core.settings import Settings, get_settings


class ExitManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def stop_loss_breached(self, position: PositionSnapshot, current_price: float) -> bool:
        threshold = (
            self.settings.risk.stop_loss_domestic
            if position.market == "KR"
            else self.settings.risk.stop_loss_overseas
        )
        if position.avg_cost <= 0:
            return False
        pnl_pct = (current_price - position.avg_cost) / position.avg_cost
        return pnl_pct <= threshold

    def trailing_stop_breached(self, position: PositionSnapshot, current_price: float) -> bool:
        if position.highest_price <= 0:
            return False
        stop_price = position.highest_price * (1 + self.settings.risk.trailing_stop)
        return current_price <= stop_price

    def atr_exit_breached(self, current_price: float, atr: float, recent_high: float, multiple: float = 2.0) -> bool:
        if atr <= 0 or recent_high <= 0:
            return False
        return current_price <= recent_high - (atr * multiple)

    def build_exit_signal(
        self,
        *,
        strategy: str,
        position: PositionSnapshot,
        reason: str,
        strength: float = 1.0,
    ) -> Signal:
        return Signal(
            ticker=position.ticker,
            market=position.market,
            action="sell",
            strategy=strategy,
            strength=strength,
            reason=reason,
            is_exit=True,
            metadata={"exit_reason": reason},
        )

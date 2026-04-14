from __future__ import annotations

from math import floor

from core.models import SizingDecision, SizingInput
from core.settings import Settings, get_settings


class PositionSizer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def size_position(self, sizing_input: SizingInput) -> SizingDecision:
        market_bucket = (
            self.settings.allocation.domestic
            if sizing_input.market == "KR"
            else self.settings.allocation.overseas
        )
        strategy_weight = getattr(self.settings.strategy_weights, sizing_input.strategy)
        gross_budget = sizing_input.cash_available * (1 - self.settings.allocation.cash_buffer)
        target_notional = gross_budget * market_bucket * strategy_weight

        stock_cap = (
            self.settings.risk.max_single_stock_domestic
            if sizing_input.market == "KR"
            else self.settings.risk.max_single_stock_overseas
        )
        capped_notional = min(target_notional, sizing_input.cash_available * stock_cap)
        capped = capped_notional < target_notional

        if sizing_input.price <= 0:
            return SizingDecision(quantity=0, target_notional=0, capped=capped, reason="invalid_price")

        quantity = max(floor(capped_notional / sizing_input.price), 0)
        return SizingDecision(
            quantity=quantity,
            target_notional=capped_notional,
            capped=capped,
            reason="capped" if capped else "sized",
        )

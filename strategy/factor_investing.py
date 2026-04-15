from __future__ import annotations

from datetime import datetime

from core.models import PositionSnapshot, Signal
from core.settings import FactorInvestingSettings
from risk.exit_manager import ExitManager
from strategy.base import BaseStrategy


class FactorInvestingStrategy(BaseStrategy):
    def __init__(
        self,
        config: FactorInvestingSettings | dict,
        data_provider=None,
        exit_manager: ExitManager | None = None,
    ) -> None:
        super().__init__(config if isinstance(config, dict) else config.model_dump(), data_provider=data_provider)
        self.name = "factor_investing"
        self.exit_manager = exit_manager or ExitManager()

    def generate_signals(self, universe: list[str], market: str, as_of: datetime) -> list[Signal]:
        if self.data_provider is None:
            return []
        if as_of.month not in self.config["rebalance_months"] or as_of.day != self.config["rebalance_day_of_month"]:
            return []

        factors = self.data_provider.get_factor_inputs(universe, market, as_of)
        scores: dict[str, float] = {}
        for ticker, snapshot in factors.items():
            scores[ticker] = (
                snapshot.value_score * self.config["value_weight"]
                + snapshot.quality_score * self.config["quality_weight"]
                + snapshot.momentum_score * self.config["momentum_weight"]
                + snapshot.low_vol_score * self.config["low_vol_weight"]
            )

        leaders = {ticker for ticker, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[: self.config["top_n"]]}
        signals: list[Signal] = []
        for ticker in universe:
            if ticker in leaders:
                signals.append(
                    Signal(
                        ticker=ticker,
                        market=market,
                        action="buy",
                        strategy="factor_investing",
                        strength=scores[ticker],
                        reason="factor_rebalance_entry",
                        metadata={"rebalance_reason": "quarterly", "source_strategies": ["factor_investing"]},
                    )
                )
            elif ticker in factors:
                signals.append(
                    Signal(
                        ticker=ticker,
                        market=market,
                        action="sell",
                        strategy="factor_investing",
                        strength=1.0,
                        reason="factor_rebalance_exit",
                        is_exit=True,
                        metadata={"rebalance_reason": "quarterly", "exit_reason": "ranking_drop"},
                    )
                )
        return signals

    def get_exit_signal(self, position: PositionSnapshot, current_price: float) -> Signal | None:
        if self.exit_manager.stop_loss_breached(position, current_price):
            return self.exit_manager.build_exit_signal(
                strategy=self.name,
                position=position,
                reason="stop_loss",
            )
        return None

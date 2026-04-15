from __future__ import annotations

from datetime import datetime

from core.models import PositionSnapshot, Signal
from core.settings import DualMomentumSettings
from risk.exit_manager import ExitManager
from strategy.base import BaseStrategy


class DualMomentumStrategy(BaseStrategy):
    def __init__(
        self,
        config: DualMomentumSettings | dict,
        data_provider=None,
        exit_manager: ExitManager | None = None,
    ) -> None:
        super().__init__(config if isinstance(config, dict) else config.model_dump(), data_provider=data_provider)
        self.name = "dual_momentum"
        self.exit_manager = exit_manager or ExitManager()

    def generate_signals(self, universe: list[str], market: str, as_of: datetime) -> list[Signal]:
        if self.data_provider is None or as_of.day != self.config["rebalance_day_of_month"]:
            return []

        histories = self.data_provider.get_price_history(
            universe,
            market,
            as_of,
            self.config["lookback_days"],
        )
        scores: dict[str, float] = {}
        for ticker, bars in histories.items():
            if len(bars) < 2 or bars[0].close <= 0:
                continue
            score = (bars[-1].close - bars[0].close) / bars[0].close
            if score >= self.config["absolute_momentum_floor"]:
                scores[ticker] = score

        leaders = {ticker for ticker, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[: self.config["top_n"]]}
        signals: list[Signal] = []
        for ticker in universe:
            if ticker in leaders:
                signals.append(
                    Signal(
                        ticker=ticker,
                        market=market,
                        action="buy",
                        strategy="dual_momentum",
                        strength=scores[ticker],
                        reason="dual_momentum_rebalance",
                        metadata={"rebalance_reason": "monthly", "source_strategies": ["dual_momentum"]},
                    )
                )
            elif ticker in histories:
                signals.append(
                    Signal(
                        ticker=ticker,
                        market=market,
                        action="sell",
                        strategy="dual_momentum",
                        strength=1.0,
                        reason="dual_momentum_exit",
                        is_exit=True,
                        metadata={"rebalance_reason": "monthly", "exit_reason": "relative_momentum_drop"},
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

from __future__ import annotations

from datetime import UTC, datetime

from core.models import PositionSnapshot, PriceBar, Signal
from core.settings import TrendFollowingSettings
from risk.exit_manager import ExitManager
from strategy.base import BaseStrategy


def _ema(values: list[float], period: int) -> float:
    multiplier = 2 / (period + 1)
    ema_value = values[0]
    for value in values[1:]:
        ema_value = (value - ema_value) * multiplier + ema_value
    return ema_value


def _rsi(values: list[float], period: int) -> float:
    deltas = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(delta, 0.0) for delta in deltas[-period:]]
    losses = [abs(min(delta, 0.0)) for delta in deltas[-period:]]
    average_gain = sum(gains) / period if gains else 0.0
    average_loss = sum(losses) / period if losses else 0.0
    if average_loss == 0:
        return 100.0
    rs = average_gain / average_loss
    return 100 - (100 / (1 + rs))


def _atr(bars: list[PriceBar], period: int) -> float:
    true_ranges: list[float] = []
    for index, bar in enumerate(bars[1:], start=1):
        prev_close = bars[index - 1].close
        high = bar.high if bar.high is not None else bar.close
        low = bar.low if bar.low is not None else bar.close
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    window = true_ranges[-period:]
    return sum(window) / len(window) if window else 0.0


class TrendFollowingStrategy(BaseStrategy):
    def __init__(
        self,
        config: TrendFollowingSettings | dict,
        data_provider=None,
        exit_manager: ExitManager | None = None,
    ) -> None:
        super().__init__(config if isinstance(config, dict) else config.model_dump(), data_provider=data_provider)
        self.name = "trend_following"
        self.exit_manager = exit_manager or ExitManager()

    def generate_signals(self, universe: list[str], market: str, as_of: datetime) -> list[Signal]:
        if self.data_provider is None:
            return []

        lookback = max(self.config["ema_slow_period"], self.config["atr_period"], self.config["rsi_period"]) + 5
        histories = self.data_provider.get_price_history(universe, market, as_of, lookback)
        signals: list[Signal] = []

        for ticker, bars in histories.items():
            closes = [bar.close for bar in bars if bar.close > 0]
            if len(closes) < self.config["ema_slow_period"]:
                continue
            ema_fast = _ema(closes[-self.config["ema_fast_period"] :], self.config["ema_fast_period"])
            ema_slow = _ema(closes[-self.config["ema_slow_period"] :], self.config["ema_slow_period"])
            rsi_value = _rsi(closes, self.config["rsi_period"])
            atr_value = _atr(bars, self.config["atr_period"])

            if ema_fast > ema_slow and rsi_value >= self.config["rsi_entry_floor"]:
                signals.append(
                    Signal(
                        ticker=ticker,
                        market=market,
                        action="buy",
                        strategy="trend_following",
                        strength=max(ema_fast - ema_slow, 0.0),
                        reason="ema_crossover_entry",
                        metadata={
                            "target_vol": self.config["target_volatility"],
                            "rsi": rsi_value,
                            "atr": atr_value,
                            "source_strategies": ["trend_following"],
                        },
                    )
                )
            elif ema_fast < ema_slow:
                signals.append(
                    Signal(
                        ticker=ticker,
                        market=market,
                        action="sell",
                        strategy="trend_following",
                        strength=max(ema_slow - ema_fast, 0.0),
                        reason="ema_crossover_exit",
                        is_exit=True,
                        metadata={"exit_reason": "ema_crossover", "atr": atr_value},
                    )
                )

        return signals

    def get_exit_signal(self, position: PositionSnapshot, current_price: float) -> Signal | None:
        if self.data_provider is None:
            return None

        lookback = max(self.config["ema_slow_period"], self.config["atr_period"]) + 5
        bars = self.data_provider.get_price_history(
            [position.ticker],
            position.market,
            datetime.now(UTC),
            lookback,
        ).get(position.ticker, [])
        if not bars:
            return None

        atr_value = _atr(bars, self.config["atr_period"])
        recent_high = max((bar.high if bar.high is not None else bar.close) for bar in bars[-self.config["atr_period"] :])
        if self.exit_manager.stop_loss_breached(position, current_price):
            return self.exit_manager.build_exit_signal(strategy=self.name, position=position, reason="stop_loss")
        if self.exit_manager.trailing_stop_breached(position, current_price):
            return self.exit_manager.build_exit_signal(strategy=self.name, position=position, reason="trailing_stop")
        if self.exit_manager.atr_exit_breached(
            current_price,
            atr=atr_value,
            recent_high=recent_high,
            multiple=self.config["atr_stop_multiple"],
        ):
            return self.exit_manager.build_exit_signal(strategy=self.name, position=position, reason="atr_stop")
        return None

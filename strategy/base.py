from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Protocol

from core.models import EventFlag, FactorSnapshot, MarketCode, PositionSnapshot, PriceBar, Signal


class StrategyDataProvider(Protocol):
    def get_price_history(
        self,
        tickers: list[str],
        market: MarketCode,
        as_of: datetime,
        lookback_days: int,
    ) -> dict[str, list[PriceBar]]:
        ...

    def get_factor_inputs(
        self,
        tickers: list[str],
        market: MarketCode,
        as_of: datetime,
    ) -> dict[str, FactorSnapshot]:
        ...

    def get_event_flags(
        self,
        tickers: list[str],
        market: MarketCode,
        as_of: datetime,
    ) -> list[EventFlag]:
        ...


class BaseStrategy(ABC):
    def __init__(self, config: dict, data_provider: StrategyDataProvider | None = None) -> None:
        self.config = config
        self.data_provider = data_provider
        self.name: str = ""

    @abstractmethod
    def generate_signals(
        self,
        universe: list[str],
        market: MarketCode,
        as_of: datetime,
    ) -> list[Signal]:
        raise NotImplementedError

    @abstractmethod
    def get_exit_signal(
        self,
        position: PositionSnapshot,
        current_price: float,
    ) -> Signal | None:
        raise NotImplementedError

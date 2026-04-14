from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from core.models import MarketCode, PositionSnapshot, Signal


class BaseStrategy(ABC):
    def __init__(self, config: dict) -> None:
        self.config = config
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

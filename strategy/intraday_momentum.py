from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from math import ceil

from core.models import IntradayBar, MarketCode, PositionSnapshot, Signal
from core.settings import IntradayMomentumSettings
from strategy.base import BaseStrategy


KST = timezone(timedelta(hours=9))
SESSION_OPEN_KST = time(9, 0)


EntryHistoryLoader = Callable[[str, date], int]
TimeProvider = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class OpeningRange:
    high: float
    low: float
    average_volume: float


def calculate_opening_range(
    bars: list[IntradayBar],
    *,
    opening_range_minutes: int,
    bar_interval_minutes: int = 1,
) -> OpeningRange | None:
    if opening_range_minutes <= 0:
        return None

    opening_bars = [
        bar
        for bar in bars
        if SESSION_OPEN_KST <= _to_kst(bar.timestamp).time() < _opening_range_end(opening_range_minutes)
    ]
    expected_bars = ceil(opening_range_minutes / max(bar_interval_minutes, 1))
    if len(opening_bars) < expected_bars:
        return None

    volumes = [max(bar.volume, 0) for bar in opening_bars]
    return OpeningRange(
        high=max(bar.high for bar in opening_bars),
        low=min(bar.low for bar in opening_bars),
        average_volume=sum(volumes) / len(volumes),
    )


def calculate_vwap(bars: list[IntradayBar]) -> float | None:
    total_volume = sum(max(bar.volume, 0) for bar in bars)
    if total_volume <= 0:
        return None

    weighted_price = sum(_typical_price(bar) * max(bar.volume, 0) for bar in bars)
    return weighted_price / total_volume


def calculate_volume_ratio(latest_bar: IntradayBar, opening_range: OpeningRange) -> float:
    if opening_range.average_volume <= 0:
        return 0.0
    return max(latest_bar.volume, 0) / opening_range.average_volume


class IntradayMomentumStrategy(BaseStrategy):
    def __init__(
        self,
        config: IntradayMomentumSettings | dict,
        data_provider=None,
        *,
        entry_history_loader: EntryHistoryLoader | None = None,
        time_provider: TimeProvider | None = None,
    ) -> None:
        super().__init__(config if isinstance(config, dict) else config.model_dump(), data_provider=data_provider)
        self.name = "intraday_momentum"
        self.entry_history_loader = entry_history_loader or (lambda ticker, trading_day: 0)
        self.time_provider = time_provider or (lambda: datetime.now(UTC))

    def generate_signals(self, universe: list[str], market: MarketCode, as_of: datetime) -> list[Signal]:
        if self.data_provider is None or market != "KR" or not universe:
            return []
        if not self._entry_window_open(as_of):
            return []

        histories = self.data_provider.get_intraday_bars(
            list(dict.fromkeys(universe)),
            market,
            as_of,
            self._lookback_minutes(as_of),
        )
        signals: list[Signal] = []
        trading_day = _to_kst(as_of).date()

        for ticker in universe:
            bars = histories.get(ticker, [])
            signal = self._build_entry_signal(ticker, market, bars, as_of, trading_day)
            if signal is not None:
                signals.append(signal)
        return signals

    def get_exit_signal(self, position: PositionSnapshot, current_price: float) -> Signal | None:
        now = self.time_provider()
        if _to_kst(now).time() >= _parse_hhmm(self.config["force_exit_time_kst"]):
            return self._exit_signal(position, "intraday_force_exit", current_price)

        if self._stop_loss_breached(position.avg_cost, current_price):
            return self._exit_signal(position, "intraday_stop_loss", current_price)

        if self._trailing_stop_breached(position.highest_price, current_price):
            return self._exit_signal(position, "intraday_trailing_stop", current_price)

        if self.data_provider is None or position.market != "KR":
            return None

        bars = self.data_provider.get_intraday_bars(
            [position.ticker],
            position.market,
            now,
            self._lookback_minutes(now),
        ).get(position.ticker, [])
        if not bars:
            return None

        opening_range = calculate_opening_range(
            bars,
            opening_range_minutes=self.config["opening_range_minutes"],
            bar_interval_minutes=self.config["bar_interval_minutes"],
        )
        vwap = calculate_vwap(bars)
        if opening_range is None or vwap is None:
            return None

        if current_price < opening_range.low:
            return self._exit_signal(position, "opening_range_low_breakdown", current_price, opening_range, vwap)
        if current_price < vwap:
            return self._exit_signal(position, "vwap_breakdown", current_price, opening_range, vwap)
        return None

    def _build_entry_signal(
        self,
        ticker: str,
        market: MarketCode,
        bars: list[IntradayBar],
        as_of: datetime,
        trading_day: date,
    ) -> Signal | None:
        if self.entry_history_loader(ticker, trading_day) >= self.config["max_entries_per_ticker_per_day"]:
            return None

        if not bars:
            return None

        opening_range = calculate_opening_range(
            bars,
            opening_range_minutes=self.config["opening_range_minutes"],
            bar_interval_minutes=self.config["bar_interval_minutes"],
        )
        if opening_range is None:
            return None

        bars_until_now = [bar for bar in bars if bar.timestamp <= _coerce_utc(as_of)]
        if not bars_until_now:
            return None

        latest_bar = bars_until_now[-1]
        latest_price = latest_bar.close
        vwap = calculate_vwap(bars_until_now)
        if vwap is None:
            return None

        volume_ratio = calculate_volume_ratio(latest_bar, opening_range)
        if latest_price <= opening_range.high or latest_price <= vwap or volume_ratio <= 1.0:
            return None

        breakout_pct = (latest_price - opening_range.high) / opening_range.high if opening_range.high > 0 else 0.0
        return Signal(
            ticker=ticker,
            market=market,
            action="buy",
            strategy="intraday_momentum",
            strength=max(breakout_pct * volume_ratio, 0.0),
            reason="opening_range_vwap_breakout",
            timestamp=_coerce_utc(as_of),
            metadata={
                "entry_reason": "opening_range_vwap_breakout",
                "opening_range_high": opening_range.high,
                "opening_range_low": opening_range.low,
                "vwap": vwap,
                "latest_price": latest_price,
                "volume_ratio": volume_ratio,
                "source_strategies": ["intraday_momentum"],
            },
        )

    def _entry_window_open(self, as_of: datetime) -> bool:
        current_time = _to_kst(as_of).time()
        return _parse_hhmm(self.config["no_entry_before_kst"]) <= current_time < _parse_hhmm(
            self.config["no_entry_after_kst"]
        )

    def _lookback_minutes(self, as_of: datetime) -> int:
        current = _to_kst(as_of)
        session_open = datetime.combine(current.date(), SESSION_OPEN_KST, tzinfo=KST)
        elapsed = int((current - session_open).total_seconds() // 60) + self.config["bar_interval_minutes"]
        return max(elapsed, self.config["opening_range_minutes"] + self.config["bar_interval_minutes"])

    def _stop_loss_breached(self, avg_cost: float, current_price: float) -> bool:
        if avg_cost <= 0:
            return False
        return (current_price - avg_cost) / avg_cost <= self.config["stop_loss_pct"]

    def _trailing_stop_breached(self, highest_price: float, current_price: float) -> bool:
        if highest_price <= 0:
            return False
        return current_price <= highest_price * (1 + self.config["trailing_stop_pct"])

    def _exit_signal(
        self,
        position: PositionSnapshot,
        reason: str,
        current_price: float,
        opening_range: OpeningRange | None = None,
        vwap: float | None = None,
    ) -> Signal:
        metadata = {
            "exit_reason": reason,
            "latest_price": current_price,
            "source_strategies": ["intraday_momentum"],
        }
        if opening_range is not None:
            metadata["opening_range_high"] = opening_range.high
            metadata["opening_range_low"] = opening_range.low
        if vwap is not None:
            metadata["vwap"] = vwap
        return Signal(
            ticker=position.ticker,
            market=position.market,
            action="sell",
            strategy="intraday_momentum",
            strength=1.0,
            reason=reason,
            timestamp=_coerce_utc(self.time_provider()),
            is_exit=True,
            metadata=metadata,
        )


def _typical_price(bar: IntradayBar) -> float:
    return (bar.high + bar.low + bar.close) / 3


def _opening_range_end(opening_range_minutes: int) -> time:
    session_open = datetime.combine(date(2000, 1, 1), SESSION_OPEN_KST)
    return (session_open + timedelta(minutes=opening_range_minutes)).time()


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", maxsplit=1)
    return time(int(hour), int(minute))


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)

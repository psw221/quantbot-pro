from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from core.models import EventFlag, EventType, FactorSnapshot, MarketCode, PriceBar
from core.settings import Settings, get_settings
from data.database import EventCalendar, get_read_session


KST = timezone(timedelta(hours=9))

PriceHistoryLoader = Callable[[list[str], datetime, int], Mapping[str, Sequence[PriceBar | Mapping[str, Any]]]]
FactorInputLoader = Callable[[list[str], MarketCode, datetime], Mapping[str, FactorSnapshot | Mapping[str, Any]]]


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _coerce_kst_date(value: datetime) -> date:
    if value.tzinfo is None:
        return value.replace(tzinfo=KST).date()
    return value.astimezone(KST).date()


def _coerce_price_bar(ticker: str, market: MarketCode, raw_bar: PriceBar | Mapping[str, Any]) -> PriceBar:
    if isinstance(raw_bar, PriceBar):
        return PriceBar(
            ticker=ticker,
            market=market,
            timestamp=_coerce_utc(raw_bar.timestamp),
            close=float(raw_bar.close),
            high=float(raw_bar.high) if raw_bar.high is not None else None,
            low=float(raw_bar.low) if raw_bar.low is not None else None,
        )

    timestamp = raw_bar.get("timestamp")
    close = raw_bar.get("close")
    if not isinstance(timestamp, datetime):
        raise ValueError("price history loader must supply datetime timestamps")
    if close is None:
        raise ValueError("price history loader must supply close prices")

    high = raw_bar.get("high")
    low = raw_bar.get("low")
    return PriceBar(
        ticker=ticker,
        market=market,
        timestamp=_coerce_utc(timestamp),
        close=float(close),
        high=float(high) if high is not None else None,
        low=float(low) if low is not None else None,
    )


def _coerce_factor_score(raw_snapshot: Mapping[str, Any], field_name: str) -> float:
    raw_value = raw_snapshot.get(field_name)
    if raw_value is None:
        raise ValueError(f"factor input loader must supply {field_name}")
    return float(raw_value)


def _coerce_factor_snapshot(
    ticker: str,
    market: MarketCode,
    raw_snapshot: FactorSnapshot | Mapping[str, Any],
) -> FactorSnapshot:
    if isinstance(raw_snapshot, FactorSnapshot):
        if raw_snapshot.ticker != ticker:
            raise ValueError("factor input loader returned mismatched ticker")
        if raw_snapshot.market != market:
            raise ValueError("factor input loader returned mismatched market")
        return FactorSnapshot(
            ticker=ticker,
            market=market,
            value_score=float(raw_snapshot.value_score),
            quality_score=float(raw_snapshot.quality_score),
            momentum_score=float(raw_snapshot.momentum_score),
            low_vol_score=float(raw_snapshot.low_vol_score),
        )

    resolved_ticker = raw_snapshot.get("ticker", ticker)
    if not isinstance(resolved_ticker, str):
        raise ValueError("factor input loader must supply string ticker values")
    if resolved_ticker != ticker:
        raise ValueError("factor input loader returned mismatched ticker")

    resolved_market = raw_snapshot.get("market", market)
    if resolved_market != market:
        raise ValueError("factor input loader returned mismatched market")

    return FactorSnapshot(
        ticker=ticker,
        market=market,
        value_score=_coerce_factor_score(raw_snapshot, "value_score"),
        quality_score=_coerce_factor_score(raw_snapshot, "quality_score"),
        momentum_score=_coerce_factor_score(raw_snapshot, "momentum_score"),
        low_vol_score=_coerce_factor_score(raw_snapshot, "low_vol_score"),
    )


def _coerce_event_type(raw_value: str) -> EventType | None:
    normalized = raw_value.strip().lower()
    for event_type in EventType:
        if event_type.value == normalized:
            return event_type
    return None


class KRStrategyDataProvider:
    """Read-only strategy input provider for the initial KR Phase 4 auto-trading path."""

    def __init__(
        self,
        *,
        price_history_loader: PriceHistoryLoader | None = None,
        factor_input_loader: FactorInputLoader | None = None,
        read_session_factory: Callable[[], Any] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.price_history_loader = price_history_loader
        self.factor_input_loader = factor_input_loader
        self.read_session_factory = read_session_factory or get_read_session
        self.settings = settings or get_settings()
        self._price_history_cache: dict[tuple[str, str, datetime], list[PriceBar]] = {}
        self._price_history_cache_lookback: dict[tuple[str, str, datetime], int] = {}

    def get_price_history(
        self,
        tickers: list[str],
        market: MarketCode,
        as_of: datetime,
        lookback_days: int,
    ) -> dict[str, list[PriceBar]]:
        if market != "KR" or not tickers or lookback_days <= 0:
            return {}
        if self.price_history_loader is None:
            return {}

        unique_tickers = list(dict.fromkeys(tickers))
        as_of_utc = _coerce_utc(as_of)
        histories: dict[str, list[PriceBar]] = {}
        missing_tickers: list[str] = []

        for ticker in unique_tickers:
            cache_key = (ticker, market, as_of_utc)
            cached_bars = self._price_history_cache.get(cache_key)
            cached_lookback = self._price_history_cache_lookback.get(cache_key, 0)
            if cached_bars is not None and cached_lookback >= lookback_days:
                histories[ticker] = cached_bars[-lookback_days:]
            else:
                missing_tickers.append(ticker)

        raw_histories = self.price_history_loader(missing_tickers, as_of, lookback_days) if missing_tickers else {}

        for ticker in missing_tickers:
            raw_bars = raw_histories.get(ticker, [])
            normalized_bars = [
                _coerce_price_bar(ticker, market, raw_bar)
                for raw_bar in raw_bars
                if _coerce_utc(
                    raw_bar.timestamp if isinstance(raw_bar, PriceBar) else raw_bar["timestamp"]  # type: ignore[index]
                )
                <= as_of_utc
            ]
            normalized_bars.sort(key=lambda bar: bar.timestamp)
            if normalized_bars:
                cache_key = (ticker, market, as_of_utc)
                self._price_history_cache[cache_key] = normalized_bars
                self._price_history_cache_lookback[cache_key] = lookback_days
                histories[ticker] = normalized_bars[-lookback_days:]
        return histories

    def get_factor_inputs(
        self,
        tickers: list[str],
        market: MarketCode,
        as_of: datetime,
    ) -> dict[str, FactorSnapshot]:
        if market != "KR" or not tickers:
            return {}
        if self.factor_input_loader is None:
            return {}

        unique_tickers = list(dict.fromkeys(tickers))
        raw_inputs = self.factor_input_loader(unique_tickers, market, as_of)
        factors: dict[str, FactorSnapshot] = {}

        for ticker in unique_tickers:
            raw_snapshot = raw_inputs.get(ticker)
            if raw_snapshot is None:
                continue
            factors[ticker] = _coerce_factor_snapshot(ticker, market, raw_snapshot)
        return factors

    def get_event_flags(
        self,
        tickers: list[str],
        market: MarketCode,
        as_of: datetime,
    ) -> list[EventFlag]:
        if market != "KR":
            return []

        target_date = _coerce_kst_date(as_of)
        ticker_set = set(tickers)

        with self.read_session_factory() as session:
            rows = list(
                session.query(EventCalendar)
                .filter(
                    EventCalendar.market == market,
                    EventCalendar.is_processed.is_(False),
                )
                .order_by(EventCalendar.event_date, EventCalendar.id)
                .all()
            )

        flags: list[EventFlag] = []
        seen: set[tuple[str, str, str | None]] = set()
        for row in rows:
            event_type = _coerce_event_type(row.event_type)
            if event_type is None:
                continue
            if _coerce_kst_date(row.event_date) != target_date:
                continue
            if row.ticker is not None and row.ticker not in ticker_set:
                continue
            dedupe_key = (event_type.value, row.market, row.ticker)
            if dedupe_key in seen:
                continue
            flags.append(
                EventFlag(
                    event_type=event_type,
                    market=market,
                    ticker=row.ticker,
                    active=True,
                    metadata={
                        "title": row.title,
                        "impact": row.impact,
                        "action": row.action,
                        "event_date": row.event_date.isoformat(),
                        "event_time": row.event_time.isoformat() if row.event_time is not None else None,
                    },
                )
            )
            seen.add(dedupe_key)
        flags.sort(key=lambda flag: (flag.event_type.value, flag.ticker or ""))
        return flags

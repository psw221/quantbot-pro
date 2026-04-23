from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from auth.token_manager import TokenManager
from data.database import Position, get_read_session
from execution.kis_api import KISApiClient
from strategy.data_provider import FactorInputLoader


KOSPI200_INDEX_TICKER = "1028"
DEFAULT_KR_AUTO_TRADING_UNIVERSE = (
    "005930",  # Samsung Electronics
    "000660",  # SK hynix
    "035420",  # NAVER
)


def build_default_kr_universe_loader(
    *,
    read_session_factory: Callable[[], Any] | None = None,
    index_ticker_loader: Callable[[datetime], list[str]] | None = None,
) -> Callable[[str, datetime], list[str]]:
    session_factory = read_session_factory or get_read_session
    resolved_index_ticker_loader = index_ticker_loader or build_pykrx_kr_index_ticker_loader()

    def loader(market: str, as_of: datetime) -> list[str]:
        if market.upper() != "KR":
            return []

        with session_factory() as session:
            rows = list(
                session.query(Position.ticker)
                .filter(
                    Position.market == "KR",
                    Position.quantity > 0,
                )
                .distinct()
                .all()
            )

        held_tickers = [row[0].strip().upper() for row in rows if isinstance(row[0], str) and row[0].strip()]
        index_tickers = resolved_index_ticker_loader(as_of)
        base_universe = index_tickers or list(DEFAULT_KR_AUTO_TRADING_UNIVERSE)
        return list(dict.fromkeys([*base_universe, *held_tickers]))

    return loader


def build_pykrx_kr_index_ticker_loader(
    *,
    index_ticker: str = KOSPI200_INDEX_TICKER,
) -> Callable[[datetime], list[str]]:
    def loader(as_of: datetime) -> list[str]:
        del as_of

        try:
            from pykrx import stock  # type: ignore
        except Exception:
            return []

        try:
            raw_tickers = stock.get_index_portfolio_deposit_file(index_ticker)
        except Exception:
            return []

        return [
            ticker.strip().upper()
            for ticker in raw_tickers
            if isinstance(ticker, str) and ticker.strip()
        ]

    return loader


def build_default_kr_factor_input_loader(*, settings: Any | None = None) -> FactorInputLoader | None:
    del settings
    return None


def build_pykrx_price_history_loader() -> Callable[[list[str], datetime, int], dict[str, list[dict[str, object]]]]:
    def loader(tickers: list[str], as_of: datetime, lookback_days: int) -> dict[str, list[dict[str, object]]]:
        if not tickers or lookback_days <= 0:
            return {}

        try:
            from pykrx import stock  # type: ignore
        except Exception:
            return {}

        requested_as_of = _coerce_utc(as_of)
        start_date = (requested_as_of - timedelta(days=max(lookback_days * 2, lookback_days + 60))).date()
        end_date = requested_as_of.date()

        histories: dict[str, list[dict[str, object]]] = {}
        for ticker in dict.fromkeys(tickers):
            try:
                frame = stock.get_market_ohlcv_by_date(
                    start_date.strftime("%Y%m%d"),
                    end_date.strftime("%Y%m%d"),
                    ticker,
                )
            except Exception:
                continue

            if frame is None or frame.empty:
                continue

            bars: list[dict[str, object]] = []
            for timestamp, row in frame.iterrows():
                bars.append(
                    {
                        "timestamp": _coerce_utc(_coerce_datetime(timestamp)),
                        "close": float(row["종가"]),
                        "high": float(row["고가"]),
                        "low": float(row["저가"]),
                    }
                )
            if bars:
                histories[ticker] = bars

        return histories

    return loader


def build_kis_kr_price_history_loader(
    *,
    api_client: KISApiClient,
    token_manager: TokenManager,
    env,
    access_token_provider: Callable[[], str] | None = None,
) -> Callable[[list[str], datetime, int], dict[str, list[dict[str, object]]]]:
    def loader(tickers: list[str], as_of: datetime, lookback_days: int) -> dict[str, list[dict[str, object]]]:
        if not tickers or lookback_days <= 0:
            return {}

        access_token = access_token_provider() if access_token_provider is not None else token_manager.get_valid_token(env)
        requested_as_of = _coerce_utc(as_of)
        start_date = (requested_as_of - timedelta(days=max(lookback_days * 2, lookback_days + 60))).strftime("%Y%m%d")
        end_date = requested_as_of.strftime("%Y%m%d")

        histories: dict[str, list[dict[str, object]]] = {}
        for ticker in dict.fromkeys(tickers):
            try:
                payload = api_client.get_daily_price_history(
                    access_token,
                    ticker=ticker,
                    start_date=start_date,
                    end_date=end_date,
                )
                rows = api_client.normalize_daily_price_history(payload, ticker=ticker)
            except Exception:
                continue
            if rows:
                histories[ticker] = rows[-lookback_days:]
        return histories

    return loader


def build_composite_kr_price_history_loader(
    *loaders: Callable[[list[str], datetime, int], dict[str, list[dict[str, object]]]],
) -> Callable[[list[str], datetime, int], dict[str, list[dict[str, object]]]]:
    usable_loaders = [loader for loader in loaders if loader is not None]

    def loader(tickers: list[str], as_of: datetime, lookback_days: int) -> dict[str, list[dict[str, object]]]:
        remaining = list(dict.fromkeys(tickers))
        histories: dict[str, list[dict[str, object]]] = {}

        for source_loader in usable_loaders:
            if not remaining:
                break
            source_histories = source_loader(remaining, as_of, lookback_days)
            for ticker, rows in source_histories.items():
                if rows:
                    histories[ticker] = rows
            remaining = [ticker for ticker in remaining if not histories.get(ticker)]

        return histories

    return loader


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()  # type: ignore[call-arg]
    raise ValueError("price history index must be datetime-like")


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

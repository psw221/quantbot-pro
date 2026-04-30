from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.models import FactorSnapshot, IntradayBar
from data.database import EventCalendar, get_session_factory, init_db, utc_now
from strategy.data_provider import KRStrategyDataProvider
from tests.test_execution.test_bootstrap import build_settings


def test_kr_strategy_data_provider_returns_sorted_filtered_price_history(tmp_path) -> None:
    settings = build_settings(tmp_path)
    as_of = datetime(2026, 4, 20, 15, 0, tzinfo=UTC)

    def loader(tickers, requested_as_of, lookback_days):
        assert tickers == ["005930"]
        assert requested_as_of == as_of
        assert lookback_days == 3
        return {
            "005930": [
                {"timestamp": datetime(2026, 4, 21, 0, 0, tzinfo=UTC), "close": 73000, "high": 73100, "low": 72900},
                {"timestamp": datetime(2026, 4, 17, 0, 0, tzinfo=UTC), "close": 71000, "high": 71100, "low": 70900},
                {"timestamp": datetime(2026, 4, 18, 0, 0, tzinfo=UTC), "close": 72000, "high": 72100, "low": 71900},
                {"timestamp": datetime(2026, 4, 16, 0, 0, tzinfo=UTC), "close": 70000, "high": 70100, "low": 69900},
            ]
        }

    provider = KRStrategyDataProvider(price_history_loader=loader, settings=settings)

    history = provider.get_price_history(["005930"], "KR", as_of, lookback_days=3)

    assert list(history) == ["005930"]
    assert [bar.close for bar in history["005930"]] == [70000.0, 71000.0, 72000.0]
    assert all(bar.market == "KR" for bar in history["005930"])


def test_kr_strategy_data_provider_returns_empty_price_history_without_loader(tmp_path) -> None:
    provider = KRStrategyDataProvider(settings=build_settings(tmp_path))

    history = provider.get_price_history(["005930"], "KR", datetime(2026, 4, 20, tzinfo=UTC), lookback_days=5)

    assert history == {}


def test_kr_strategy_data_provider_returns_empty_intraday_bars_without_loader(tmp_path) -> None:
    provider = KRStrategyDataProvider(settings=build_settings(tmp_path))

    history = provider.get_intraday_bars(["005930"], "KR", datetime(2026, 4, 20, 9, 35, tzinfo=UTC), 60)

    assert history == {}


def test_kr_strategy_data_provider_normalizes_filtered_intraday_bars(tmp_path) -> None:
    settings = build_settings(tmp_path)
    as_of = datetime(2026, 4, 20, 9, 35, 30, tzinfo=UTC)

    def loader(tickers, market, requested_as_of, lookback_minutes):
        assert tickers == ["005930"]
        assert market == "KR"
        assert requested_as_of == as_of
        assert lookback_minutes == 30
        return {
            "005930": [
                {
                    "timestamp": datetime(2026, 4, 20, 9, 10, tzinfo=UTC),
                    "open": "70000",
                    "high": "70100",
                    "low": "69900",
                    "close": "70050",
                    "volume": "1000",
                },
                IntradayBar(
                    ticker="005930",
                    market="KR",
                    timestamp=datetime(2026, 4, 20, 9, 35, tzinfo=UTC),
                    open=70200,
                    high=70300,
                    low=70150,
                    close=70250,
                    volume=1500,
                ),
                {
                    "timestamp": datetime(2026, 4, 20, 9, 36, tzinfo=UTC),
                    "open": 70300,
                    "high": 70400,
                    "low": 70200,
                    "close": 70350,
                    "volume": 2000,
                },
                {
                    "timestamp": datetime(2026, 4, 20, 9, 0, tzinfo=UTC),
                    "open": 69000,
                    "high": 69100,
                    "low": 68900,
                    "close": 69050,
                    "volume": 900,
                },
            ]
        }

    provider = KRStrategyDataProvider(intraday_bar_loader=loader, settings=settings)

    history = provider.get_intraday_bars(["005930", "005930"], "KR", as_of, lookback_minutes=30)

    assert list(history) == ["005930"]
    assert [bar.close for bar in history["005930"]] == [70050.0, 70250.0]
    assert [bar.volume for bar in history["005930"]] == [1000, 1500]
    assert all(bar.market == "KR" for bar in history["005930"])


def test_kr_strategy_data_provider_reuses_intraday_cache_within_same_minute(tmp_path) -> None:
    settings = build_settings(tmp_path)
    first_as_of = datetime(2026, 4, 20, 9, 35, 10, tzinfo=UTC)
    second_as_of = datetime(2026, 4, 20, 9, 35, 50, tzinfo=UTC)
    calls: list[int] = []

    def loader(tickers, market, requested_as_of, lookback_minutes):
        calls.append(lookback_minutes)
        return {
            "005930": [
                {
                    "timestamp": datetime(2026, 4, 20, 9, 20, tzinfo=UTC),
                    "open": 70000,
                    "high": 70100,
                    "low": 69900,
                    "close": 70050,
                    "volume": 1000,
                },
                {
                    "timestamp": datetime(2026, 4, 20, 9, 34, tzinfo=UTC),
                    "open": 70100,
                    "high": 70200,
                    "low": 70000,
                    "close": 70150,
                    "volume": 1200,
                },
            ]
        }

    provider = KRStrategyDataProvider(intraday_bar_loader=loader, settings=settings)

    first = provider.get_intraday_bars(["005930"], "KR", first_as_of, lookback_minutes=30)
    second = provider.get_intraday_bars(["005930"], "KR", second_as_of, lookback_minutes=10)

    assert calls == [30]
    assert [bar.close for bar in first["005930"]] == [70050.0, 70150.0]
    assert [bar.close for bar in second["005930"]] == [70150.0]


def test_kr_strategy_data_provider_reuses_cached_longer_lookback_for_shorter_request(tmp_path) -> None:
    settings = build_settings(tmp_path)
    as_of = datetime(2026, 4, 20, 15, 0, tzinfo=UTC)
    calls: list[int] = []

    def loader(tickers, requested_as_of, lookback_days):
        calls.append(lookback_days)
        return {
            "005930": [
                {"timestamp": datetime(2026, 4, 16, 0, 0, tzinfo=UTC), "close": 70000, "high": 70100, "low": 69900},
                {"timestamp": datetime(2026, 4, 17, 0, 0, tzinfo=UTC), "close": 71000, "high": 71100, "low": 70900},
                {"timestamp": datetime(2026, 4, 18, 0, 0, tzinfo=UTC), "close": 72000, "high": 72100, "low": 71900},
                {"timestamp": datetime(2026, 4, 19, 0, 0, tzinfo=UTC), "close": 73000, "high": 73100, "low": 72900},
            ]
        }

    provider = KRStrategyDataProvider(price_history_loader=loader, settings=settings)

    first = provider.get_price_history(["005930"], "KR", as_of, lookback_days=4)
    second = provider.get_price_history(["005930"], "KR", as_of, lookback_days=2)

    assert calls == [4]
    assert [bar.close for bar in first["005930"]] == [70000.0, 71000.0, 72000.0, 73000.0]
    assert [bar.close for bar in second["005930"]] == [72000.0, 73000.0]


def test_kr_strategy_data_provider_reads_same_day_event_flags_from_event_calendar(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 4, 20, 1, 0, tzinfo=UTC)

    with session_factory() as session:
        session.add_all(
            [
                EventCalendar(
                    event_date=datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
                    event_time=datetime(2026, 4, 20, 2, 0, tzinfo=UTC),
                    event_type="bok",
                    market="KR",
                    ticker=None,
                    title="BOK meeting",
                    impact="high",
                    action="block_buy",
                    is_processed=False,
                    created_at=utc_now(),
                ),
                EventCalendar(
                    event_date=datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
                    event_time=None,
                    event_type="earnings",
                    market="KR",
                    ticker="005930",
                    title="Samsung earnings",
                    impact="medium",
                    action="block_buy",
                    is_processed=False,
                    created_at=utc_now(),
                ),
                EventCalendar(
                    event_date=datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
                    event_time=None,
                    event_type="earnings",
                    market="KR",
                    ticker="000660",
                    title="SK Hynix earnings",
                    impact="medium",
                    action="block_buy",
                    is_processed=False,
                    created_at=utc_now(),
                ),
                EventCalendar(
                    event_date=datetime(2026, 4, 19, 0, 0, tzinfo=UTC),
                    event_time=None,
                    event_type="vkospi_high",
                    market="KR",
                    ticker=None,
                    title="Yesterday stress",
                    impact="high",
                    action="scale_down",
                    is_processed=False,
                    created_at=utc_now(),
                ),
                EventCalendar(
                    event_date=datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
                    event_time=None,
                    event_type="unknown_type",
                    market="KR",
                    ticker=None,
                    title="Ignore unknown",
                    impact="low",
                    action="noop",
                    is_processed=False,
                    created_at=utc_now(),
                ),
                EventCalendar(
                    event_date=datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
                    event_time=None,
                    event_type="bok",
                    market="KR",
                    ticker=None,
                    title="Already processed",
                    impact="high",
                    action="block_buy",
                    is_processed=True,
                    created_at=utc_now(),
                ),
            ]
        )
        session.commit()

    provider = KRStrategyDataProvider(settings=settings)

    flags = provider.get_event_flags(["005930"], "KR", as_of)

    assert [(flag.event_type.value, flag.ticker) for flag in flags] == [
        ("bok", None),
        ("earnings", "005930"),
    ]
    assert flags[0].metadata["action"] == "block_buy"
    assert flags[1].metadata["title"] == "Samsung earnings"


def test_kr_strategy_data_provider_returns_empty_factor_inputs_without_loader(tmp_path) -> None:
    provider = KRStrategyDataProvider(settings=build_settings(tmp_path))

    factors = provider.get_factor_inputs(["005930"], "KR", datetime(2026, 4, 20, tzinfo=UTC))

    assert factors == {}


def test_kr_strategy_data_provider_normalizes_factor_inputs_from_loader(tmp_path) -> None:
    settings = build_settings(tmp_path)
    as_of = datetime(2026, 4, 20, 15, 0, tzinfo=UTC)

    def loader(tickers, market, requested_as_of):
        assert tickers == ["005930", "000660"]
        assert market == "KR"
        assert requested_as_of == as_of
        return {
            "005930": {
                "value_score": 1,
                "quality_score": "2.5",
                "momentum_score": 3.0,
                "low_vol_score": 4,
            },
            "000660": FactorSnapshot(
                ticker="000660",
                market="KR",
                value_score=0.5,
                quality_score=0.6,
                momentum_score=0.7,
                low_vol_score=0.8,
            ),
            "035420": FactorSnapshot(
                ticker="035420",
                market="KR",
                value_score=9.0,
                quality_score=9.0,
                momentum_score=9.0,
                low_vol_score=9.0,
            ),
        }

    provider = KRStrategyDataProvider(factor_input_loader=loader, settings=settings)

    factors = provider.get_factor_inputs(["005930", "005930", "000660"], "KR", as_of)

    assert list(factors) == ["005930", "000660"]
    assert factors["005930"] == FactorSnapshot(
        ticker="005930",
        market="KR",
        value_score=1.0,
        quality_score=2.5,
        momentum_score=3.0,
        low_vol_score=4.0,
    )
    assert factors["000660"] == FactorSnapshot(
        ticker="000660",
        market="KR",
        value_score=0.5,
        quality_score=0.6,
        momentum_score=0.7,
        low_vol_score=0.8,
    )


def test_kr_strategy_data_provider_rejects_mismatched_factor_input_payload(tmp_path) -> None:
    provider = KRStrategyDataProvider(
        factor_input_loader=lambda tickers, market, as_of: {
            "005930": {
                "ticker": "000660",
                "market": market,
                "value_score": 1.0,
                "quality_score": 1.0,
                "momentum_score": 1.0,
                "low_vol_score": 1.0,
            }
        },
        settings=build_settings(tmp_path),
    )

    with pytest.raises(ValueError, match="mismatched ticker"):
        provider.get_factor_inputs(["005930"], "KR", datetime(2026, 4, 20, tzinfo=UTC))

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import main as main_module
from data.collector import (
    DEFAULT_KR_AUTO_TRADING_UNIVERSE,
    build_cached_kr_index_ticker_loader,
    build_default_kr_factor_input_loader,
    build_default_kr_universe_loader,
    build_kr_intraday_candidate_loader,
    build_kis_kr_intraday_bar_loader,
    build_kis_kr_price_history_loader,
    build_pykrx_kr_previous_turnover_loader,
    build_pykrx_price_history_loader,
    rank_tickers_by_turnover,
)
from data.database import Position, get_session_factory, init_db, utc_now
from main import build_strategy_cycle_runner
from tests.test_execution.test_bootstrap import build_settings


def test_default_kr_universe_loader_prefers_index_constituents_and_keeps_existing_positions(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    session_factory = get_session_factory()

    with session_factory() as session:
        session.add(
            Position(
                ticker="251340",
                market="KR",
                strategy="dual_momentum",
                quantity=3,
                avg_cost=12000,
                current_price=12100,
                highest_price=12200,
                entry_date=datetime(2026, 4, 1, tzinfo=UTC),
                updated_at=utc_now(),
            )
        )
        session.commit()

    loader = build_default_kr_universe_loader(
        read_session_factory=session_factory,
        index_ticker_loader=lambda as_of: ["373220", "207940", "005930"],
        cache_ticker_loader=lambda: ["000660"],
    )

    universe = loader("KR", datetime(2026, 4, 20, tzinfo=UTC))

    assert universe == ["373220", "207940", "005930", "251340"]


def test_default_kr_universe_loader_uses_cache_when_index_constituents_are_unavailable(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    session_factory = get_session_factory()

    with session_factory() as session:
        session.add(
            Position(
                ticker="251340",
                market="KR",
                strategy="dual_momentum",
                quantity=3,
                avg_cost=12000,
                current_price=12100,
                highest_price=12200,
                entry_date=datetime(2026, 4, 1, tzinfo=UTC),
                updated_at=utc_now(),
            )
        )
        session.commit()

    loader = build_default_kr_universe_loader(
        read_session_factory=session_factory,
        index_ticker_loader=lambda as_of: [],
        cache_ticker_loader=lambda: ["005930", "000660", "005930"],
    )

    universe = loader("KR", datetime(2026, 4, 20, tzinfo=UTC))

    assert universe[:2] == ["005930", "000660"]
    assert universe[-1] == "251340"


def test_default_kr_universe_loader_falls_back_to_minimum_universe_when_index_and_cache_are_unavailable(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    session_factory = get_session_factory()

    loader = build_default_kr_universe_loader(
        read_session_factory=session_factory,
        index_ticker_loader=lambda as_of: [],
        cache_ticker_loader=lambda: [],
    )

    universe = loader("KR", datetime(2026, 4, 20, tzinfo=UTC))

    assert universe == list(DEFAULT_KR_AUTO_TRADING_UNIVERSE)


def test_cached_kr_index_ticker_loader_reads_static_kospi200_cache(tmp_path) -> None:
    cache_path = tmp_path / "kospi200.json"
    cache_path.write_text(
        '{"tickers":["005930"," 000660 ","bad","0126Z0","005930",123]}',
        encoding="utf-8",
    )

    loader = build_cached_kr_index_ticker_loader(cache_path=cache_path)

    assert loader() == ["005930", "000660"]


def test_rank_tickers_by_turnover_keeps_stable_order_for_ties() -> None:
    ranked = rank_tickers_by_turnover(
        ["AAA", "BBB", "CCC", "DDD"],
        {"AAA": 1000, "BBB": 3000, "CCC": 3000, "DDD": 0},
        top_n=3,
    )

    assert ranked == ["BBB", "CCC", "AAA"]


def test_intraday_candidate_loader_uses_turnover_top_n_and_includes_positions(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    session_factory = get_session_factory()

    with session_factory() as session:
        session.add(
            Position(
                ticker="251340",
                market="KR",
                strategy="intraday_momentum",
                quantity=3,
                avg_cost=12000,
                current_price=12100,
                highest_price=12200,
                entry_date=datetime(2026, 4, 1, tzinfo=UTC),
                updated_at=utc_now(),
            )
        )
        session.commit()

    loader = build_kr_intraday_candidate_loader(
        universe_loader=lambda market, as_of: ["005930", "000660", "035420", "207940"],
        turnover_loader=lambda tickers, as_of: {
            "005930": 100,
            "000660": 400,
            "035420": 300,
            "207940": 200,
        },
        read_session_factory=session_factory,
        top_n=2,
    )

    candidates = loader("KR", datetime(2026, 4, 20, tzinfo=UTC))

    assert candidates == ["000660", "035420", "251340"]


def test_intraday_candidate_loader_falls_back_to_universe_order_when_turnover_missing(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    session_factory = get_session_factory()

    with session_factory() as session:
        session.add(
            Position(
                ticker="251340",
                market="KR",
                strategy="trend_following",
                quantity=1,
                avg_cost=12000,
                current_price=12100,
                highest_price=12200,
                entry_date=datetime(2026, 4, 1, tzinfo=UTC),
                updated_at=utc_now(),
            )
        )
        session.commit()

    loader = build_kr_intraday_candidate_loader(
        universe_loader=lambda market, as_of: ["005930", "000660", "035420"],
        turnover_loader=lambda tickers, as_of: {},
        read_session_factory=session_factory,
        top_n=2,
    )

    candidates = loader("KR", datetime(2026, 4, 20, tzinfo=UTC))

    assert candidates == ["005930", "000660", "251340"]


def test_pykrx_previous_turnover_loader_reads_latest_available_turnover(monkeypatch) -> None:
    class EmptyFrame:
        empty = True

    class FakeLoc:
        def __getitem__(self, ticker):
            rows = {
                "005930": {"거래대금": 1000},
                "000660": {"거래대금": 2000},
            }
            return rows[ticker]

    class FakeFrame:
        empty = False
        loc = FakeLoc()

    calls: list[str] = []

    def get_market_ohlcv_by_ticker(date: str, market: str):
        calls.append(date)
        assert market == "KOSPI"
        if len(calls) == 1:
            return EmptyFrame()
        return FakeFrame()

    monkeypatch.setitem(
        sys.modules,
        "pykrx",
        SimpleNamespace(stock=SimpleNamespace(get_market_ohlcv_by_ticker=get_market_ohlcv_by_ticker)),
    )

    loader = build_pykrx_kr_previous_turnover_loader()

    turnovers = loader(["005930", "000660", "035420"], datetime(2026, 4, 20, tzinfo=UTC))

    assert calls == ["20260419", "20260418"]
    assert turnovers == {"005930": 1000.0, "000660": 2000.0}


def test_pykrx_price_history_loader_normalizes_rows(monkeypatch) -> None:
    class FakeFrame:
        empty = False

        def iterrows(self):
            yield datetime(2026, 4, 17), {"고가": 71000, "저가": 69000, "종가": 70500}
            yield datetime(2026, 4, 20), {"고가": 71500, "저가": 69500, "종가": 71000}

    def get_market_ohlcv_by_date(start: str, end: str, ticker: str):
        assert ticker == "005930"
        return FakeFrame()

    monkeypatch.setitem(sys.modules, "pykrx", SimpleNamespace(stock=SimpleNamespace(get_market_ohlcv_by_date=get_market_ohlcv_by_date)))

    loader = build_pykrx_price_history_loader()

    history = loader(["005930"], datetime(2026, 4, 20, tzinfo=UTC), 30)

    assert "005930" in history
    assert history["005930"][0]["close"] == 70500.0
    assert history["005930"][1]["high"] == 71500.0
    assert history["005930"][1]["timestamp"] == datetime(2026, 4, 20, tzinfo=UTC)


def test_build_strategy_cycle_runner_passes_access_token_and_strategy_subset_to_auto_trader(tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    calls: list[tuple[str, datetime, str | None, list[str] | None]] = []

    class DummyTokenManager:
        def get_valid_token(self, env):
            assert env == settings.env
            return "access-token"

    class DummyApiClient:
        def get_cash_balance(self, access_token):
            assert access_token == "access-token"
            return {"output": {"ord_psbl_cash": "1500000"}}

        def normalize_cash_available(self, payload):
            return float(payload["output"]["ord_psbl_cash"])

    class DummyAutoTrader:
        def execute_cycle(
            self,
            market: str,
            as_of: datetime,
            *,
            access_token: str | None = None,
            strategies: list[str] | None = None,
        ):
            calls.append((market, as_of, access_token, strategies))
            return {"market": market}

    runner = build_strategy_cycle_runner(
        settings=settings,
        token_manager=DummyTokenManager(),
        api_client=DummyApiClient(),
        order_manager=object(),
        auto_trader=DummyAutoTrader(),
    )

    assert runner is not None
    as_of = datetime(2026, 4, 20, 9, 15, tzinfo=UTC)
    result = runner("KR", as_of, ["trend_following"])

    assert calls == [("KR", as_of, "access-token", ["trend_following"])]
    assert result == {"market": "KR"}


def test_build_strategy_cycle_runner_returns_none_when_auto_trading_disabled(tmp_path) -> None:
    settings = build_settings(tmp_path)

    runner = build_strategy_cycle_runner(
        settings=settings,
        token_manager=object(),
        api_client=object(),
        order_manager=object(),
    )

    assert runner is None


def test_default_kr_factor_input_loader_returns_none_without_source(tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})

    loader = build_default_kr_factor_input_loader(settings=settings)

    assert loader is None


def test_build_strategy_cycle_runner_uses_default_factor_input_loader_builder(monkeypatch, tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    captured = {}

    class DummyTokenManager:
        def get_valid_token(self, env):
            assert env == settings.env
            return "access-token"

    class DummyApiClient:
        def get_cash_balance(self, access_token):
            assert access_token == "access-token"
            return {"output": {"ord_psbl_cash": "1500000"}}

        def normalize_cash_available(self, payload):
            return float(payload["output"]["ord_psbl_cash"])

    class CapturingAutoTrader:
        def __init__(self, **kwargs):
            captured["factor_input_loader"] = kwargs["data_provider"].factor_input_loader
            captured["intraday_bar_loader"] = kwargs["data_provider"].intraday_bar_loader
            captured["universe_loader"] = kwargs["universe_loader"]

        def execute_cycle(
            self,
            market: str,
            as_of: datetime,
            *,
            access_token: str | None = None,
            strategies: list[str] | None = None,
        ):
            return {"market": market, "access_token": access_token}

    default_loader = lambda tickers, market, as_of: {}

    monkeypatch.setattr(main_module, "AutoTrader", CapturingAutoTrader)
    monkeypatch.setattr(main_module, "build_default_kr_factor_input_loader", lambda *, settings: default_loader)

    runner = build_strategy_cycle_runner(
        settings=settings,
        token_manager=DummyTokenManager(),
        api_client=DummyApiClient(),
        order_manager=object(),
    )

    assert runner is not None
    result = runner("KR", datetime(2026, 4, 20, 9, 15, tzinfo=UTC))

    assert captured["factor_input_loader"] is default_loader
    assert captured["intraday_bar_loader"] is not None
    assert captured["universe_loader"] is not None
    assert result == {"market": "KR", "access_token": "access-token"}


def test_build_strategy_cycle_runner_keeps_runner_when_default_factor_input_loader_is_missing(monkeypatch, tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    captured = {}

    class DummyTokenManager:
        def get_valid_token(self, env):
            assert env == settings.env
            return "access-token"

    class DummyApiClient:
        def get_cash_balance(self, access_token):
            assert access_token == "access-token"
            return {"output": {"ord_psbl_cash": "1500000"}}

        def normalize_cash_available(self, payload):
            return float(payload["output"]["ord_psbl_cash"])

    class CapturingAutoTrader:
        def __init__(self, **kwargs):
            captured["factor_input_loader"] = kwargs["data_provider"].factor_input_loader

        def execute_cycle(
            self,
            market: str,
            as_of: datetime,
            *,
            access_token: str | None = None,
            strategies: list[str] | None = None,
        ):
            return {"market": market, "access_token": access_token}

    monkeypatch.setattr(main_module, "AutoTrader", CapturingAutoTrader)
    monkeypatch.setattr(main_module, "build_default_kr_factor_input_loader", lambda *, settings: None)

    runner = build_strategy_cycle_runner(
        settings=settings,
        token_manager=DummyTokenManager(),
        api_client=DummyApiClient(),
        order_manager=object(),
    )

    assert runner is not None
    result = runner("KR", datetime(2026, 4, 20, 9, 15, tzinfo=UTC))

    assert captured["factor_input_loader"] is None
    assert result == {"market": "KR", "access_token": "access-token"}


def test_build_strategy_cycle_runner_preserves_explicit_factor_input_loader_over_default_builder(monkeypatch, tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    captured = {}

    class DummyTokenManager:
        def get_valid_token(self, env):
            assert env == settings.env
            return "access-token"

    class DummyApiClient:
        def get_cash_balance(self, access_token):
            assert access_token == "access-token"
            return {"output": {"ord_psbl_cash": "1500000"}}

        def normalize_cash_available(self, payload):
            return float(payload["output"]["ord_psbl_cash"])

    class CapturingAutoTrader:
        def __init__(self, **kwargs):
            captured["factor_input_loader"] = kwargs["data_provider"].factor_input_loader

        def execute_cycle(
            self,
            market: str,
            as_of: datetime,
            *,
            access_token: str | None = None,
            strategies: list[str] | None = None,
        ):
            return {"market": market, "access_token": access_token}

    default_loader = lambda tickers, market, as_of: {"default": {}}
    explicit_loader = lambda tickers, market, as_of: {}

    monkeypatch.setattr(main_module, "AutoTrader", CapturingAutoTrader)
    monkeypatch.setattr(main_module, "build_default_kr_factor_input_loader", lambda *, settings: default_loader)

    runner = build_strategy_cycle_runner(
        settings=settings,
        token_manager=DummyTokenManager(),
        api_client=DummyApiClient(),
        order_manager=object(),
        factor_input_loader=explicit_loader,
    )

    assert runner is not None
    result = runner("KR", datetime(2026, 4, 20, 9, 15, tzinfo=UTC))

    assert captured["factor_input_loader"] is explicit_loader
    assert result == {"market": "KR", "access_token": "access-token"}


def test_kis_price_history_loader_prefers_cycle_access_token_provider(tmp_path) -> None:
    settings = build_settings(tmp_path)
    calls: list[str] = []

    class DummyTokenManager:
        def get_valid_token(self, env):
            raise AssertionError("token manager should not be called when cycle access token is provided")

    class DummyApiClient:
        def get_daily_price_history(self, access_token, *, ticker, start_date, end_date, period_code="D", adjusted_price=False):
            calls.append(access_token)
            assert ticker == "005930"
            return {
                "output2": [
                    {"stck_bsop_date": "20260418", "stck_clpr": "71000", "stck_hgpr": "71500", "stck_lwpr": "70500"},
                    {"stck_bsop_date": "20260421", "stck_clpr": "72000", "stck_hgpr": "72500", "stck_lwpr": "71500"},
                ]
            }

        def normalize_daily_price_history(self, payload, *, ticker):
            return [
                {"timestamp": datetime(2026, 4, 18, tzinfo=UTC), "close": 71000.0, "high": 71500.0, "low": 70500.0},
                {"timestamp": datetime(2026, 4, 21, tzinfo=UTC), "close": 72000.0, "high": 72500.0, "low": 71500.0},
            ]

    loader = build_kis_kr_price_history_loader(
        api_client=DummyApiClient(),
        token_manager=DummyTokenManager(),
        env=settings.env,
        access_token_provider=lambda: "cycle-access-token",
    )

    histories = loader(["005930"], datetime(2026, 4, 21, 1, 0, tzinfo=UTC), 2)

    assert calls == ["cycle-access-token"]
    assert [row["close"] for row in histories["005930"]] == [71000.0, 72000.0]


def test_kis_intraday_bar_loader_prefers_cycle_access_token_provider(tmp_path) -> None:
    settings = build_settings(tmp_path)
    calls: list[tuple[str, str]] = []

    class DummyTokenManager:
        def get_valid_token(self, env):
            raise AssertionError("token manager should not be called when cycle access token is provided")

    class DummyApiClient:
        def get_intraday_price_history(self, access_token, *, ticker, input_hour, period_code="1", include_prev="N"):
            calls.append((access_token, input_hour))
            assert ticker == "005930"
            return {"output2": [{"stck_bsop_date": "20260421", "stck_cntg_hour": "101500"}]}

        def normalize_intraday_price_history(self, payload, *, ticker):
            assert payload["output2"]
            return [
                {
                    "timestamp": datetime(2026, 4, 21, 1, 15, tzinfo=UTC),
                    "open": 70000.0,
                    "high": 71000.0,
                    "low": 69500.0,
                    "close": 70500.0,
                    "volume": 1000,
                }
            ]

    loader = build_kis_kr_intraday_bar_loader(
        api_client=DummyApiClient(),
        token_manager=DummyTokenManager(),
        env=settings.env,
        access_token_provider=lambda: "cycle-access-token",
    )

    histories = loader(["005930"], "KR", datetime(2026, 4, 21, 1, 15, tzinfo=UTC), 60)

    assert calls == [("cycle-access-token", "101500")]
    assert histories["005930"][0]["close"] == 70500.0

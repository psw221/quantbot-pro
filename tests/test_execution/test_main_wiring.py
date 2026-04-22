from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import main as main_module
from data.collector import (
    DEFAULT_KR_AUTO_TRADING_UNIVERSE,
    build_default_kr_universe_loader,
    build_kis_kr_price_history_loader,
    build_pykrx_price_history_loader,
)
from data.database import Position, get_session_factory, init_db, utc_now
from main import build_strategy_cycle_runner
from tests.test_execution.test_bootstrap import build_settings


def test_default_kr_universe_loader_includes_existing_positions_and_fallback(tmp_path) -> None:
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

    loader = build_default_kr_universe_loader(read_session_factory=session_factory)

    universe = loader("KR", datetime(2026, 4, 20, tzinfo=UTC))

    assert universe[0] == "251340"
    assert universe[1:] == list(DEFAULT_KR_AUTO_TRADING_UNIVERSE)


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


def test_build_strategy_cycle_runner_passes_access_token_to_auto_trader(tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    calls: list[tuple[str, datetime, str | None]] = []

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
        def execute_cycle(self, market: str, as_of: datetime, *, access_token: str | None = None):
            calls.append((market, as_of, access_token))
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
    result = runner("KR", as_of)

    assert calls == [("KR", as_of, "access-token")]
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


def test_build_strategy_cycle_runner_wires_optional_factor_input_loader(monkeypatch, tmp_path) -> None:
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

        def execute_cycle(self, market: str, as_of: datetime, *, access_token: str | None = None):
            return {"market": market, "access_token": access_token}

    monkeypatch.setattr(main_module, "AutoTrader", CapturingAutoTrader)
    factor_input_loader = lambda tickers, market, as_of: {}

    runner = build_strategy_cycle_runner(
        settings=settings,
        token_manager=DummyTokenManager(),
        api_client=DummyApiClient(),
        order_manager=object(),
        factor_input_loader=factor_input_loader,
    )

    assert runner is not None
    result = runner("KR", datetime(2026, 4, 20, 9, 15, tzinfo=UTC))

    assert captured["factor_input_loader"] is factor_input_loader
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

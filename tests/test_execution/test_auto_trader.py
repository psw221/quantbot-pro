from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.models import BrokerOrderResult
from core.models import FactorSnapshot, PositionSnapshot, PriceBar, Signal
from data.database import Order, PortfolioSnapshot, Position, Signal as SignalRow, get_session_factory, init_db, utc_now
from execution.auto_trader import AutoTrader
from execution.order_manager import OrderManager
from execution.writer_queue import WriterQueue
from strategy.base import BaseStrategy
from strategy.data_provider import KRStrategyDataProvider
from strategy.factor_investing import FactorInvestingStrategy
from tests.test_execution.test_bootstrap import build_settings


class StubEntryStrategy(BaseStrategy):
    def __init__(self, strategy_name: str) -> None:
        super().__init__({})
        self.name = strategy_name

    def generate_signals(self, universe: list[str], market: str, as_of: datetime) -> list[Signal]:
        if not universe:
            return []
        return [
            Signal(
                ticker=universe[0],
                market=market,
                action="buy",
                strategy=self.name,  # type: ignore[arg-type]
                strength=1.0,
                reason=f"{self.name}_entry",
            )
        ]

    def get_exit_signal(self, position: PositionSnapshot, current_price: float) -> Signal | None:
        return None


class StubExitStrategy(BaseStrategy):
    def __init__(self, strategy_name: str) -> None:
        super().__init__({})
        self.name = strategy_name

    def generate_signals(self, universe: list[str], market: str, as_of: datetime) -> list[Signal]:
        return []

    def get_exit_signal(self, position: PositionSnapshot, current_price: float) -> Signal | None:
        return Signal(
            ticker=position.ticker,
            market=position.market,
            action="sell",
            strategy=self.name,  # type: ignore[arg-type]
            strength=1.0,
            reason="stub_exit",
            is_exit=True,
            metadata={"exit_reason": "stop_loss"},
        )


class MultiTickerEntryStrategy(BaseStrategy):
    def __init__(self, strategy_name: str) -> None:
        super().__init__({})
        self.name = strategy_name

    def generate_signals(self, universe: list[str], market: str, as_of: datetime) -> list[Signal]:
        return [
            Signal(
                ticker=ticker,
                market=market,
                action="buy",
                strategy=self.name,  # type: ignore[arg-type]
                strength=1.0,
                reason=f"{self.name}_entry",
            )
            for ticker in universe
        ]

    def get_exit_signal(self, position: PositionSnapshot, current_price: float) -> Signal | None:
        return None


def _kr_bars(ticker: str, closes: list[float]) -> list[PriceBar]:
    start = datetime(2025, 8, 15, tzinfo=UTC)
    bars: list[PriceBar] = []
    for index, close in enumerate(closes):
        bars.append(
            PriceBar(
                ticker=ticker,
                market="KR",
                timestamp=start + timedelta(days=index),
                close=close,
                high=close + 50,
                low=close - 50,
            )
        )
    return bars


def _settings_with_auto_trading_strategies(tmp_path, strategies: list[str]):
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    return settings.model_copy(
        update={
            "auto_trading": settings.auto_trading.model_copy(
                update={"strategies": strategies}
            )
        }
    )


def _factor_snapshot(
    ticker: str,
    *,
    value: float = 0.9,
    quality: float = 0.8,
    momentum: float = 0.7,
    low_vol: float = 0.6,
) -> FactorSnapshot:
    return FactorSnapshot(
        ticker=ticker,
        market="KR",
        value_score=value,
        quality_score=quality,
        momentum_score=momentum,
        low_vol_score=low_vol,
    )


def test_auto_trader_builds_dry_candidate_from_resolved_signals_without_writing(tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 5, 1, tzinfo=UTC)
    closes = [70000 + index * 100 for index in range(280)]

    with session_factory() as session:
        session.add(
            PortfolioSnapshot(
                snapshot_date=as_of - timedelta(days=1),
                total_value_krw=12000000,
                cash_krw=3000000,
                domestic_value_krw=7000000,
                overseas_value_krw=2000000,
                usd_krw_rate=1350,
                daily_return=0.01,
                cumulative_return=0.10,
                drawdown=-0.02,
                max_drawdown=-0.05,
                position_count=0,
                created_at=utc_now(),
            )
        )
        session.commit()

    provider = KRStrategyDataProvider(price_history_loader=lambda tickers, requested_as_of, lookback_days: {"005930": _kr_bars("005930", closes)}, settings=settings)
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: ["005930"],
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of)

    assert result.signals_generated == 2
    assert result.signals_resolved == 1
    assert len(result.order_candidates) == 1
    candidate = result.order_candidates[0]
    assert candidate.signal.ticker == "005930"
    assert candidate.quantity > 0
    assert candidate.order_type == "market"
    assert candidate.metadata["source_strategies"] == ["dual_momentum", "trend_following"]
    with session_factory() as session:
        assert session.query(SignalRow).count() == 0
        assert session.query(Order).count() == 0


def test_auto_trader_rejects_ticker_with_existing_open_order(tmp_path) -> None:
    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["dual_momentum"]},
    )
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 5, 1, tzinfo=UTC)

    with session_factory() as session:
        signal_row = SignalRow(
            ticker="005930",
            market="KR",
            strategy="dual_momentum",
            action="buy",
            strength=1.0,
            reason="existing",
            status="resolved",
            generated_at=as_of,
            processed_at=as_of,
        )
        session.add(signal_row)
        session.flush()
        session.add(
            Order(
                client_order_id="existing-open-order",
                signal_id=signal_row.id,
                ticker="005930",
                market="KR",
                strategy="dual_momentum",
                side="buy",
                order_type="market",
                quantity=1,
                price=None,
                status="submitted",
                submitted_at=as_of,
                updated_at=as_of,
            )
        )
        session.add(
            PortfolioSnapshot(
                snapshot_date=as_of - timedelta(days=1),
                total_value_krw=1000000,
                cash_krw=500000,
                domestic_value_krw=500000,
                overseas_value_krw=0,
                usd_krw_rate=1350,
                daily_return=0,
                cumulative_return=0,
                drawdown=0,
                max_drawdown=0,
                position_count=0,
                created_at=utc_now(),
            )
        )
        session.commit()

    provider = KRStrategyDataProvider(
        price_history_loader=lambda tickers, requested_as_of, lookback_days: {"005930": _kr_bars("005930", [70000, 70500, 71000])},
        settings=settings,
    )
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: ["005930"],
        strategy_builders={"dual_momentum": lambda settings, provider: StubEntryStrategy("dual_momentum")},
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of)

    assert result.order_candidates == []
    assert len(result.rejected_signals) == 1
    assert result.rejected_signals[0].reason == "open_order_exists"


def test_auto_trader_skips_factor_strategy_when_factor_input_loader_is_missing(tmp_path) -> None:
    settings = _settings_with_auto_trading_strategies(tmp_path, ["dual_momentum", "factor_investing"])
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 4, 1, tzinfo=UTC)

    with session_factory() as session:
        session.add(
            PortfolioSnapshot(
                snapshot_date=as_of - timedelta(days=1),
                total_value_krw=12000000,
                cash_krw=3000000,
                domestic_value_krw=7000000,
                overseas_value_krw=2000000,
                usd_krw_rate=1350,
                daily_return=0.01,
                cumulative_return=0.10,
                drawdown=-0.02,
                max_drawdown=-0.05,
                position_count=0,
                created_at=utc_now(),
            )
        )
        session.commit()

    provider = KRStrategyDataProvider(
        price_history_loader=lambda tickers, requested_as_of, lookback_days: {
            "005930": _kr_bars("005930", [70000, 70500, 71000]),
        },
        settings=settings,
    )
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: ["005930"],
        strategy_builders={
            "dual_momentum": lambda settings, provider: StubEntryStrategy("dual_momentum"),
            "factor_investing": lambda settings, provider: StubEntryStrategy("factor_investing"),
        },
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of)

    assert [signal.strategy for signal in result.generated_signals] == ["dual_momentum"]
    assert len(result.order_candidates) == 1
    diagnostics = {item.strategy_name: item for item in result.strategy_diagnostics}
    assert diagnostics["dual_momentum"].status == "completed"
    assert diagnostics["dual_momentum"].skip_reason is None
    assert diagnostics["dual_momentum"].factor_input_available is None
    assert diagnostics["factor_investing"].status == "skipped"
    assert diagnostics["factor_investing"].skip_reason == "factor_input_unavailable"
    assert diagnostics["factor_investing"].factor_input_available is False


def test_auto_trader_runs_only_requested_strategy_subset(tmp_path) -> None:
    settings = _settings_with_auto_trading_strategies(
        tmp_path,
        ["dual_momentum", "trend_following", "factor_investing"],
    )
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 4, 1, tzinfo=UTC)

    with session_factory() as session:
        session.add(
            Position(
                ticker="000660",
                market="KR",
                strategy="factor_investing",
                quantity=2,
                avg_cost=120000,
                current_price=121000,
                highest_price=122000,
                entry_date=as_of - timedelta(days=10),
                updated_at=utc_now(),
            )
        )
        session.add(
            PortfolioSnapshot(
                snapshot_date=as_of - timedelta(days=1),
                total_value_krw=12_000_000,
                cash_krw=3_000_000,
                domestic_value_krw=7_000_000,
                overseas_value_krw=2_000_000,
                usd_krw_rate=1350,
                daily_return=0.01,
                cumulative_return=0.10,
                drawdown=-0.02,
                max_drawdown=-0.05,
                position_count=1,
                created_at=utc_now(),
            )
        )
        session.commit()

    provider = KRStrategyDataProvider(
        price_history_loader=lambda tickers, requested_as_of, lookback_days: {
            "005930": _kr_bars("005930", [70000, 70500, 71000]),
            "000660": _kr_bars("000660", [120000, 120500, 121000]),
        },
        settings=settings,
    )
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: ["005930"],
        strategy_builders={
            "trend_following": lambda settings, provider: StubEntryStrategy("trend_following")
        },
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of, strategies=["trend_following"])

    assert result.configured_strategies == ["trend_following"]
    assert result.details["position_tickers"] == []
    assert [item.strategy_name for item in result.strategy_diagnostics] == ["trend_following"]
    assert [signal.strategy for signal in result.generated_signals] == ["trend_following"]
    assert len(result.order_candidates) == 1


def test_auto_trader_dedupes_requested_strategy_subset_preserving_order(tmp_path) -> None:
    settings = _settings_with_auto_trading_strategies(tmp_path, ["dual_momentum", "trend_following"])
    init_db(settings)
    provider = KRStrategyDataProvider(settings=settings)
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: [],
        strategy_builders={
            "dual_momentum": lambda settings, provider: StubEntryStrategy("dual_momentum"),
            "trend_following": lambda settings, provider: StubEntryStrategy("trend_following"),
        },
        settings=settings,
    )

    result = trader.run_cycle(
        "KR",
        datetime(2026, 4, 1, tzinfo=UTC),
        strategies=["trend_following", "trend_following", "dual_momentum"],
    )

    assert result.configured_strategies == ["trend_following", "dual_momentum"]
    assert [item.strategy_name for item in result.strategy_diagnostics] == ["trend_following", "dual_momentum"]


def test_auto_trader_rejects_empty_strategy_subset(tmp_path) -> None:
    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["trend_following"]},
    )
    init_db(settings)
    provider = KRStrategyDataProvider(settings=settings)
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: [],
        strategy_builders={
            "trend_following": lambda settings, provider: StubEntryStrategy("trend_following")
        },
        settings=settings,
    )

    with pytest.raises(ValueError, match="must not be empty"):
        trader.run_cycle("KR", datetime(2026, 4, 1, tzinfo=UTC), strategies=[])


def test_auto_trader_rejects_strategy_subset_outside_configured_settings(tmp_path) -> None:
    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["trend_following"]},
    )
    init_db(settings)
    provider = KRStrategyDataProvider(settings=settings)
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: [],
        strategy_builders={
            "trend_following": lambda settings, provider: StubEntryStrategy("trend_following")
        },
        settings=settings,
    )

    with pytest.raises(ValueError, match="must be enabled in settings: factor_investing"):
        trader.run_cycle(
            "KR",
            datetime(2026, 4, 1, tzinfo=UTC),
            strategies=["factor_investing"],
        )


def test_auto_trader_propagates_factor_input_payload_errors(tmp_path) -> None:
    settings = _settings_with_auto_trading_strategies(tmp_path, ["factor_investing"])
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
        settings=settings,
    )
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: ["005930"],
        strategy_builders={
            "factor_investing": lambda settings, provider: FactorInvestingStrategy(
                settings.strategies.factor_investing,
                data_provider=provider,
            )
        },
        settings=settings,
    )

    with pytest.raises(ValueError, match="mismatched ticker"):
        trader.run_cycle("KR", datetime(2026, 4, 1, tzinfo=UTC))


def test_auto_trader_marks_factor_strategy_available_when_loader_exists(tmp_path) -> None:
    settings = _settings_with_auto_trading_strategies(tmp_path, ["factor_investing"])
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 4, 1, tzinfo=UTC)

    with session_factory() as session:
        session.add(
            PortfolioSnapshot(
                snapshot_date=as_of - timedelta(days=1),
                total_value_krw=12_000_000,
                cash_krw=3_000_000,
                domestic_value_krw=7_000_000,
                overseas_value_krw=2_000_000,
                usd_krw_rate=1350,
                daily_return=0.01,
                cumulative_return=0.10,
                drawdown=-0.02,
                max_drawdown=-0.05,
                position_count=0,
                created_at=utc_now(),
            )
        )
        session.commit()

    provider = KRStrategyDataProvider(
        price_history_loader=lambda tickers, requested_as_of, lookback_days: {
            "005930": _kr_bars("005930", [70000, 70500, 71000]),
            "000660": _kr_bars("000660", [120000, 120500, 121000]),
        },
        factor_input_loader=lambda tickers, market, requested_as_of: {
            "005930": FactorSnapshot(
                ticker="005930",
                market="KR",
                value_score=0.9,
                quality_score=0.8,
                momentum_score=0.7,
                low_vol_score=0.6,
            ),
            "000660": FactorSnapshot(
                ticker="000660",
                market="KR",
                value_score=0.2,
                quality_score=0.2,
                momentum_score=0.2,
                low_vol_score=0.2,
            ),
        },
        settings=settings,
    )
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: ["005930", "000660"],
        strategy_builders={
            "factor_investing": lambda settings, provider: FactorInvestingStrategy(
                settings.strategies.factor_investing.model_copy(update={"top_n": 1}),
                data_provider=provider,
            )
        },
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of)

    diagnostics = {item.strategy_name: item for item in result.strategy_diagnostics}
    assert diagnostics["factor_investing"].status == "completed"
    assert diagnostics["factor_investing"].skip_reason is None
    assert diagnostics["factor_investing"].factor_input_available is True
    assert any(signal.strategy == "factor_investing" and signal.action == "buy" for signal in result.generated_signals)
    assert len(result.order_candidates) == 1


def test_auto_trader_default_builder_runs_factor_strategy_with_canonical_settings(tmp_path) -> None:
    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["factor_investing"]},
    )
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 4, 1, tzinfo=UTC)

    with session_factory() as session:
        session.add(
            PortfolioSnapshot(
                snapshot_date=as_of - timedelta(days=1),
                total_value_krw=12_000_000,
                cash_krw=3_000_000,
                domestic_value_krw=7_000_000,
                overseas_value_krw=2_000_000,
                usd_krw_rate=1350,
                daily_return=0.01,
                cumulative_return=0.10,
                drawdown=-0.02,
                max_drawdown=-0.05,
                position_count=0,
                created_at=utc_now(),
            )
        )
        session.commit()

    provider = KRStrategyDataProvider(
        price_history_loader=lambda tickers, requested_as_of, lookback_days: {
            "005930": _kr_bars("005930", [70000, 70500, 71000]),
        },
        factor_input_loader=lambda tickers, market, requested_as_of: {
            "005930": _factor_snapshot("005930")
        },
        settings=settings,
    )
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: ["005930"],
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of)

    diagnostics = {item.strategy_name: item for item in result.strategy_diagnostics}
    assert diagnostics["factor_investing"].status == "completed"
    assert diagnostics["factor_investing"].skip_reason is None
    assert diagnostics["factor_investing"].factor_input_available is True
    assert [signal.strategy for signal in result.generated_signals] == ["factor_investing"]
    assert len(result.order_candidates) == 1
    assert result.order_candidates[0].metadata["source_strategies"] == ["factor_investing"]


def test_auto_trader_default_builder_blocks_factor_reentry_when_position_exists(tmp_path) -> None:
    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["factor_investing"]},
    )
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 4, 1, tzinfo=UTC)

    with session_factory() as session:
        session.add(
            Position(
                ticker="005930",
                market="KR",
                strategy="factor_investing",
                quantity=3,
                avg_cost=70000,
                current_price=71000,
                highest_price=71500,
                entry_date=as_of - timedelta(days=10),
                updated_at=utc_now(),
            )
        )
        session.add(
            PortfolioSnapshot(
                snapshot_date=as_of - timedelta(days=1),
                total_value_krw=4_000_000,
                cash_krw=2_500_000,
                domestic_value_krw=1_500_000,
                overseas_value_krw=0,
                usd_krw_rate=1350,
                daily_return=0,
                cumulative_return=0,
                drawdown=0,
                max_drawdown=0,
                position_count=1,
                created_at=utc_now(),
            )
        )
        session.commit()

    provider = KRStrategyDataProvider(
        price_history_loader=lambda tickers, requested_as_of, lookback_days: {
            "005930": _kr_bars("005930", [70000, 70500, 71000]),
        },
        factor_input_loader=lambda tickers, market, requested_as_of: {
            "005930": _factor_snapshot("005930")
        },
        settings=settings,
    )
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: ["005930"],
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of)

    assert result.order_candidates == []
    assert len(result.rejected_signals) == 1
    assert result.rejected_signals[0].reason == "existing_position_reentry_blocked"
    assert result.rejected_signals[0].signal.strategy == "factor_investing"


def test_auto_trader_execute_cycle_persists_factor_strategy_order_when_loader_exists(tmp_path) -> None:
    class AcceptedSubmitClient:
        def submit_order(self, payload, access_token=None):
            return {"rt_cd": "0", "msg_cd": "APBK0012", "msg1": "ok", "output": {"ODNO": "20001"}}

        def normalize_order_result(self, payload):
            return BrokerOrderResult(
                accepted=True,
                broker_order_no=payload["output"]["ODNO"],
                broker_order_orgno="06010",
                raw_payload=payload,
            )

    settings = _settings_with_auto_trading_strategies(tmp_path, ["factor_investing"])
    init_db(settings)
    session_factory = get_session_factory()
    writer_queue = WriterQueue()
    writer_queue.start()
    as_of = datetime(2026, 4, 1, tzinfo=UTC)

    try:
        with session_factory() as session:
            session.add(
                PortfolioSnapshot(
                    snapshot_date=as_of - timedelta(days=1),
                    total_value_krw=12_000_000,
                    cash_krw=3_000_000,
                    domestic_value_krw=7_000_000,
                    overseas_value_krw=2_000_000,
                    usd_krw_rate=1350,
                    daily_return=0.01,
                    cumulative_return=0.10,
                    drawdown=-0.02,
                    max_drawdown=-0.05,
                    position_count=0,
                    created_at=utc_now(),
                )
            )
            session.commit()

        provider = KRStrategyDataProvider(
            price_history_loader=lambda tickers, requested_as_of, lookback_days: {
                "005930": _kr_bars("005930", [70000, 70500, 71000]),
            },
            factor_input_loader=lambda tickers, market, requested_as_of: {
                "005930": FactorSnapshot(
                    ticker="005930",
                    market="KR",
                    value_score=0.9,
                    quality_score=0.8,
                    momentum_score=0.7,
                    low_vol_score=0.6,
                )
            },
            settings=settings,
        )
        manager = OrderManager(writer_queue=writer_queue, api_client=AcceptedSubmitClient(), settings=settings)
        trader = AutoTrader(
            data_provider=provider,
            universe_loader=lambda market, timestamp: ["005930"],
            strategy_builders={
                "factor_investing": lambda settings, provider: FactorInvestingStrategy(
                    settings.strategies.factor_investing,
                    data_provider=provider,
                )
            },
            order_manager=manager,
            settings=settings,
        )

        result = trader.execute_cycle("KR", as_of)
    finally:
        writer_queue.stop()

    diagnostics = {item.strategy_name: item for item in result.strategy_diagnostics}
    assert diagnostics["factor_investing"].status == "completed"
    assert diagnostics["factor_investing"].factor_input_available is True
    assert result.orders_submitted == 1
    with session_factory() as session:
        signal_row = session.query(SignalRow).one()
        order = session.query(Order).one()

    assert signal_row.strategy == "factor_investing"
    assert order.strategy == "factor_investing"
    assert order.status == "submitted"
    assert order.kis_order_no == "20001"


def test_auto_trader_default_builder_executes_factor_strategy_with_canonical_settings(tmp_path) -> None:
    class AcceptedSubmitClient:
        def submit_order(self, payload, access_token=None):
            return {"rt_cd": "0", "msg_cd": "APBK0012", "msg1": "ok", "output": {"ODNO": "20002"}}

        def normalize_order_result(self, payload):
            return BrokerOrderResult(
                accepted=True,
                broker_order_no=payload["output"]["ODNO"],
                broker_order_orgno="06010",
                raw_payload=payload,
            )

    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["factor_investing"]},
    )
    init_db(settings)
    session_factory = get_session_factory()
    writer_queue = WriterQueue()
    writer_queue.start()
    as_of = datetime(2026, 4, 1, tzinfo=UTC)

    try:
        with session_factory() as session:
            session.add(
                PortfolioSnapshot(
                    snapshot_date=as_of - timedelta(days=1),
                    total_value_krw=12_000_000,
                    cash_krw=3_000_000,
                    domestic_value_krw=7_000_000,
                    overseas_value_krw=2_000_000,
                    usd_krw_rate=1350,
                    daily_return=0.01,
                    cumulative_return=0.10,
                    drawdown=-0.02,
                    max_drawdown=-0.05,
                    position_count=0,
                    created_at=utc_now(),
                )
            )
            session.commit()

        provider = KRStrategyDataProvider(
            price_history_loader=lambda tickers, requested_as_of, lookback_days: {
                "005930": _kr_bars("005930", [70000, 70500, 71000]),
            },
            factor_input_loader=lambda tickers, market, requested_as_of: {
                "005930": _factor_snapshot("005930")
            },
            settings=settings,
        )
        manager = OrderManager(writer_queue=writer_queue, api_client=AcceptedSubmitClient(), settings=settings)
        trader = AutoTrader(
            data_provider=provider,
            universe_loader=lambda market, timestamp: ["005930"],
            order_manager=manager,
            settings=settings,
        )

        result = trader.execute_cycle("KR", as_of)
    finally:
        writer_queue.stop()

    diagnostics = {item.strategy_name: item for item in result.strategy_diagnostics}
    assert diagnostics["factor_investing"].status == "completed"
    assert diagnostics["factor_investing"].factor_input_available is True
    assert result.orders_submitted == 1
    with session_factory() as session:
        signal_row = session.query(SignalRow).one()
        order = session.query(Order).one()

    assert signal_row.strategy == "factor_investing"
    assert order.strategy == "factor_investing"
    assert order.status == "submitted"
    assert order.kis_order_no == "20002"


def test_auto_trader_execute_cycle_uses_requested_strategy_subset(tmp_path) -> None:
    class AcceptedSubmitClient:
        def submit_order(self, payload, access_token=None):
            return {"rt_cd": "0", "msg_cd": "APBK0012", "msg1": "ok", "output": {"ODNO": "20003"}}

        def normalize_order_result(self, payload):
            return BrokerOrderResult(
                accepted=True,
                broker_order_no=payload["output"]["ODNO"],
                broker_order_orgno="06010",
                raw_payload=payload,
            )

    settings = _settings_with_auto_trading_strategies(tmp_path, ["dual_momentum", "trend_following"])
    init_db(settings)
    session_factory = get_session_factory()
    writer_queue = WriterQueue()
    writer_queue.start()
    as_of = datetime(2026, 4, 1, tzinfo=UTC)

    try:
        with session_factory() as session:
            session.add(
                PortfolioSnapshot(
                    snapshot_date=as_of - timedelta(days=1),
                    total_value_krw=12_000_000,
                    cash_krw=3_000_000,
                    domestic_value_krw=7_000_000,
                    overseas_value_krw=2_000_000,
                    usd_krw_rate=1350,
                    daily_return=0.01,
                    cumulative_return=0.10,
                    drawdown=-0.02,
                    max_drawdown=-0.05,
                    position_count=0,
                    created_at=utc_now(),
                )
            )
            session.commit()

        provider = KRStrategyDataProvider(
            price_history_loader=lambda tickers, requested_as_of, lookback_days: {
                "005930": _kr_bars("005930", [70000, 70500, 71000]),
            },
            settings=settings,
        )
        manager = OrderManager(writer_queue=writer_queue, api_client=AcceptedSubmitClient(), settings=settings)
        trader = AutoTrader(
            data_provider=provider,
            universe_loader=lambda market, timestamp: ["005930"],
            strategy_builders={
                "trend_following": lambda settings, provider: StubEntryStrategy("trend_following")
            },
            order_manager=manager,
            settings=settings,
        )

        result = trader.execute_cycle("KR", as_of, strategies=["trend_following"])
    finally:
        writer_queue.stop()

    assert result.configured_strategies == ["trend_following"]
    assert [item.strategy_name for item in result.strategy_diagnostics] == ["trend_following"]
    assert result.orders_submitted == 1
    with session_factory() as session:
        signal_row = session.query(SignalRow).one()
        order = session.query(Order).one()

    assert signal_row.strategy == "trend_following"
    assert order.strategy == "trend_following"
    assert order.kis_order_no == "20003"


def test_auto_trader_skips_factor_exit_evaluation_when_loader_is_missing(tmp_path) -> None:
    settings = _settings_with_auto_trading_strategies(tmp_path, ["factor_investing"])
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 4, 1, tzinfo=UTC)

    with session_factory() as session:
        session.add(
            Position(
                ticker="005930",
                market="KR",
                strategy="factor_investing",
                quantity=5,
                avg_cost=70000,
                current_price=65000,
                highest_price=72000,
                entry_date=as_of - timedelta(days=10),
                updated_at=utc_now(),
            )
        )
        session.add(
            PortfolioSnapshot(
                snapshot_date=as_of - timedelta(days=1),
                total_value_krw=2_000_000,
                cash_krw=300_000,
                domestic_value_krw=1_700_000,
                overseas_value_krw=0,
                usd_krw_rate=1350,
                daily_return=0.0,
                cumulative_return=0.0,
                drawdown=0.0,
                max_drawdown=0.0,
                position_count=1,
                created_at=utc_now(),
            )
        )
        session.commit()

    provider = KRStrategyDataProvider(
        price_history_loader=lambda tickers, requested_as_of, lookback_days: {
            "005930": _kr_bars("005930", [70000, 69000, 68000, 67000, 66000]),
        },
        settings=settings,
    )
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: [],
        strategy_builders={
            "factor_investing": lambda settings, provider: StubExitStrategy("factor_investing")
        },
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of)

    diagnostics = {item.strategy_name: item for item in result.strategy_diagnostics}
    assert diagnostics["factor_investing"].status == "skipped"
    assert diagnostics["factor_investing"].skip_reason == "factor_input_unavailable"
    assert result.generated_signals == []
    assert result.order_candidates == []
    assert result.rejected_signals == []


def test_auto_trader_rejects_buy_reentry_when_same_strategy_position_exists(tmp_path) -> None:
    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["trend_following"]},
    )
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 5, 1, tzinfo=UTC)

    with session_factory() as session:
        session.add(
            Position(
                ticker="005930",
                market="KR",
                strategy="trend_following",
                quantity=3,
                avg_cost=70000,
                current_price=71000,
                highest_price=71500,
                entry_date=as_of - timedelta(days=5),
                updated_at=utc_now(),
            )
        )
        session.add(
            PortfolioSnapshot(
                snapshot_date=as_of - timedelta(days=1),
                total_value_krw=2000000,
                cash_krw=500000,
                domestic_value_krw=1500000,
                overseas_value_krw=0,
                usd_krw_rate=1350,
                daily_return=0,
                cumulative_return=0,
                drawdown=0,
                max_drawdown=0,
                position_count=1,
                created_at=utc_now(),
            )
        )
        session.commit()

    provider = KRStrategyDataProvider(
        price_history_loader=lambda tickers, requested_as_of, lookback_days: {
            "005930": _kr_bars("005930", [70000 + index * 100 for index in range(90)])
        },
        settings=settings,
    )
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: ["005930"],
        strategy_builders={"trend_following": lambda settings, provider: StubEntryStrategy("trend_following")},
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of)

    assert result.order_candidates == []
    assert len(result.rejected_signals) == 1
    assert result.rejected_signals[0].reason == "existing_position_reentry_blocked"


def test_auto_trader_allows_buy_when_position_exists_for_different_strategy(tmp_path) -> None:
    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["trend_following"]},
    )
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 5, 1, tzinfo=UTC)

    with session_factory() as session:
        session.add(
            Position(
                ticker="005930",
                market="KR",
                strategy="dual_momentum",
                quantity=3,
                avg_cost=70000,
                current_price=71000,
                highest_price=71500,
                entry_date=as_of - timedelta(days=5),
                updated_at=utc_now(),
            )
        )
        session.add(
            PortfolioSnapshot(
                snapshot_date=as_of - timedelta(days=1),
                total_value_krw=4000000,
                cash_krw=2500000,
                domestic_value_krw=1500000,
                overseas_value_krw=0,
                usd_krw_rate=1350,
                daily_return=0,
                cumulative_return=0,
                drawdown=0,
                max_drawdown=0,
                position_count=1,
                created_at=utc_now(),
            )
        )
        session.commit()

    provider = KRStrategyDataProvider(
        price_history_loader=lambda tickers, requested_as_of, lookback_days: {
            "005930": _kr_bars("005930", [70000 + index * 100 for index in range(90)])
        },
        settings=settings,
    )
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: ["005930"],
        strategy_builders={"trend_following": lambda settings, provider: StubEntryStrategy("trend_following")},
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of)

    assert len(result.order_candidates) == 1
    assert result.rejected_signals == []


def test_auto_trader_builds_exit_candidate_from_existing_position(tmp_path) -> None:
    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["trend_following"]},
    )
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 5, 1, tzinfo=UTC)

    with session_factory() as session:
        session.add(
            Position(
                ticker="005930",
                market="KR",
                strategy="trend_following",
                quantity=7,
                avg_cost=70000,
                current_price=68000,
                highest_price=72000,
                entry_date=as_of - timedelta(days=5),
                updated_at=utc_now(),
            )
        )
        session.add(
            PortfolioSnapshot(
                snapshot_date=as_of - timedelta(days=1),
                total_value_krw=2000000,
                cash_krw=300000,
                domestic_value_krw=1700000,
                overseas_value_krw=0,
                usd_krw_rate=1350,
                daily_return=0,
                cumulative_return=0,
                drawdown=0,
                max_drawdown=0,
                position_count=1,
                created_at=utc_now(),
            )
        )
        session.commit()

    provider = KRStrategyDataProvider(
        price_history_loader=lambda tickers, requested_as_of, lookback_days: {"005930": _kr_bars("005930", [70000, 69500, 69000, 68500, 68000])},
        settings=settings,
    )
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: [],
        strategy_builders={"trend_following": lambda settings, provider: StubExitStrategy("trend_following")},
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of)

    assert result.signals_generated == 1
    assert len(result.order_candidates) == 1
    candidate = result.order_candidates[0]
    assert candidate.signal.action == "sell"
    assert candidate.quantity == 7
    assert candidate.sizing_decision.reason == "exit_full_position"


def test_auto_trader_rejects_candidate_when_latest_price_is_missing(tmp_path) -> None:
    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["dual_momentum"]},
    )
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 5, 1, tzinfo=UTC)

    with session_factory() as session:
        session.add(
            PortfolioSnapshot(
                snapshot_date=as_of - timedelta(days=1),
                total_value_krw=2000000,
                cash_krw=500000,
                domestic_value_krw=1500000,
                overseas_value_krw=0,
                usd_krw_rate=1350,
                daily_return=0,
                cumulative_return=0,
                drawdown=0,
                max_drawdown=0,
                position_count=0,
                created_at=utc_now(),
            )
        )
        session.commit()

    provider = KRStrategyDataProvider(price_history_loader=lambda tickers, requested_as_of, lookback_days: {}, settings=settings)
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: ["005930"],
        strategy_builders={"dual_momentum": lambda settings, provider: StubEntryStrategy("dual_momentum")},
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of)

    assert result.order_candidates == []
    assert len(result.rejected_signals) == 1
    assert result.rejected_signals[0].reason == "data_unavailable"
    assert result.rejected_signals[0].detail == "latest_price_missing"


def test_auto_trader_execute_cycle_persists_signal_and_submits_order(tmp_path) -> None:
    class AcceptedSubmitClient:
        def submit_order(self, payload, access_token=None):
            return {"rt_cd": "0", "msg_cd": "APBK0012", "msg1": "ok", "output": {"ODNO": "10001"}}

        def normalize_order_result(self, payload):
            return BrokerOrderResult(
                accepted=True,
                broker_order_no=payload["output"]["ODNO"],
                broker_order_orgno="06010",
                raw_payload=payload,
            )

    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["dual_momentum"]},
    )
    init_db(settings)
    session_factory = get_session_factory()
    writer_queue = WriterQueue()
    writer_queue.start()
    as_of = datetime(2026, 5, 1, tzinfo=UTC)

    try:
        with session_factory() as session:
            session.add(
                PortfolioSnapshot(
                    snapshot_date=as_of - timedelta(days=1),
                    total_value_krw=12000000,
                    cash_krw=3000000,
                    domestic_value_krw=7000000,
                    overseas_value_krw=2000000,
                    usd_krw_rate=1350,
                    daily_return=0.01,
                    cumulative_return=0.10,
                    drawdown=-0.02,
                    max_drawdown=-0.05,
                    position_count=0,
                    created_at=utc_now(),
                )
            )
            session.commit()

        provider = KRStrategyDataProvider(
            price_history_loader=lambda tickers, requested_as_of, lookback_days: {"005930": _kr_bars("005930", [70000 + index * 100 for index in range(280)])},
            settings=settings,
        )
        manager = OrderManager(writer_queue=writer_queue, api_client=AcceptedSubmitClient(), settings=settings)
        trader = AutoTrader(
            data_provider=provider,
            universe_loader=lambda market, timestamp: ["005930"],
            strategy_builders={"dual_momentum": lambda settings, provider: StubEntryStrategy("dual_momentum")},
            order_manager=manager,
            settings=settings,
        )

        result = trader.execute_cycle("KR", as_of)
    finally:
        writer_queue.stop()

    assert result.orders_submitted == 1
    assert result.details["submitted_order_count"] == 1
    with session_factory() as session:
        assert session.query(SignalRow).count() == 1
        order = session.query(Order).one()

    assert order.status == "submitted"
    assert order.kis_order_no == "10001"
    assert order.kis_order_orgno == "06010"


def test_auto_trader_execute_cycle_respects_cycle_order_limit(tmp_path) -> None:
    class AcceptedSubmitClient:
        def submit_order(self, payload, access_token=None):
            return {"rt_cd": "0", "msg_cd": "APBK0012", "msg1": "ok", "output": {"ODNO": f"ODNO-{payload['ticker']}"}}

        def normalize_order_result(self, payload):
            return BrokerOrderResult(
                accepted=True,
                broker_order_no=payload["output"]["ODNO"],
                broker_order_orgno="06010",
                raw_payload=payload,
            )

    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["dual_momentum"], "max_orders_per_cycle": 1},
    )
    init_db(settings)
    session_factory = get_session_factory()
    writer_queue = WriterQueue()
    writer_queue.start()
    as_of = datetime(2026, 5, 1, tzinfo=UTC)

    try:
        with session_factory() as session:
            session.add(
                PortfolioSnapshot(
                    snapshot_date=as_of - timedelta(days=1),
                    total_value_krw=12000000,
                    cash_krw=3000000,
                    domestic_value_krw=7000000,
                    overseas_value_krw=2000000,
                    usd_krw_rate=1350,
                    daily_return=0.01,
                    cumulative_return=0.10,
                    drawdown=-0.02,
                    max_drawdown=-0.05,
                    position_count=0,
                    created_at=utc_now(),
                )
            )
            session.commit()

        provider = KRStrategyDataProvider(
            price_history_loader=lambda tickers, requested_as_of, lookback_days: {
                "005930": _kr_bars("005930", [70000 + index * 100 for index in range(280)]),
                "000660": _kr_bars("000660", [120000 + index * 100 for index in range(280)]),
            },
            settings=settings,
        )
        manager = OrderManager(writer_queue=writer_queue, api_client=AcceptedSubmitClient(), settings=settings)
        trader = AutoTrader(
            data_provider=provider,
            universe_loader=lambda market, timestamp: ["005930", "000660"],
            strategy_builders={"dual_momentum": lambda settings, provider: MultiTickerEntryStrategy("dual_momentum")},
            order_manager=manager,
            settings=settings,
        )

        result = trader.execute_cycle("KR", as_of)
    finally:
        writer_queue.stop()

    assert result.orders_submitted == 1
    assert any(rejection.reason == "cycle_order_limit" for rejection in result.rejected_signals)
    with session_factory() as session:
        assert session.query(Order).count() == 1


def test_auto_trader_execute_cycle_surfaces_submit_failure_without_marking_submitted(tmp_path) -> None:
    class FailingSubmitClient:
        def submit_order(self, payload, access_token=None):
            return {"rt_cd": "1", "msg_cd": "ERR001", "msg1": "submit failed"}

        def normalize_order_result(self, payload):
            return BrokerOrderResult(
                accepted=False,
                error_code=payload["msg_cd"],
                error_message=payload["msg1"],
                raw_payload=payload,
            )

    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["dual_momentum"]},
    )
    init_db(settings)
    session_factory = get_session_factory()
    writer_queue = WriterQueue()
    writer_queue.start()
    as_of = datetime(2026, 5, 1, tzinfo=UTC)

    try:
        with session_factory() as session:
            session.add(
                PortfolioSnapshot(
                    snapshot_date=as_of - timedelta(days=1),
                    total_value_krw=12000000,
                    cash_krw=3000000,
                    domestic_value_krw=7000000,
                    overseas_value_krw=2000000,
                    usd_krw_rate=1350,
                    daily_return=0.01,
                    cumulative_return=0.10,
                    drawdown=-0.02,
                    max_drawdown=-0.05,
                    position_count=0,
                    created_at=utc_now(),
                )
            )
            session.commit()

        provider = KRStrategyDataProvider(
            price_history_loader=lambda tickers, requested_as_of, lookback_days: {"005930": _kr_bars("005930", [70000 + index * 100 for index in range(280)])},
            settings=settings,
        )
        manager = OrderManager(writer_queue=writer_queue, api_client=FailingSubmitClient(), settings=settings)
        trader = AutoTrader(
            data_provider=provider,
            universe_loader=lambda market, timestamp: ["005930"],
            strategy_builders={"dual_momentum": lambda settings, provider: StubEntryStrategy("dual_momentum")},
            order_manager=manager,
            settings=settings,
        )

        result = trader.execute_cycle("KR", as_of)
    finally:
        writer_queue.stop()

    assert result.orders_submitted == 0
    assert any(rejection.reason == "submit failed" for rejection in result.rejected_signals)
    with session_factory() as session:
        order = session.query(Order).one()

    assert order.status == "failed"


def test_auto_trader_uses_cash_available_loader_when_snapshot_is_missing(tmp_path) -> None:
    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["dual_momentum"]},
    )
    init_db(settings)
    as_of = datetime(2026, 5, 1, tzinfo=UTC)

    provider = KRStrategyDataProvider(
        price_history_loader=lambda tickers, requested_as_of, lookback_days: {
            "005930": _kr_bars("005930", [70000 + index * 100 for index in range(280)])
        },
        settings=settings,
    )
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: ["005930"],
        strategy_builders={"dual_momentum": lambda settings, provider: StubEntryStrategy("dual_momentum")},
        cash_available_loader=lambda market, timestamp: 2_000_000.0,
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of)

    assert result.cash_available == 2_000_000.0
    assert len(result.order_candidates) == 1


def test_auto_trader_reuses_strategy_loaded_history_for_price_context(tmp_path) -> None:
    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["trend_following"]},
    )
    init_db(settings)
    session_factory = get_session_factory()
    as_of = datetime(2026, 5, 1, tzinfo=UTC)

    with session_factory() as session:
        session.add(
            PortfolioSnapshot(
                snapshot_date=as_of - timedelta(days=1),
                total_value_krw=12000000,
                cash_krw=3000000,
                domestic_value_krw=7000000,
                overseas_value_krw=2000000,
                usd_krw_rate=1350,
                daily_return=0.01,
                cumulative_return=0.10,
                drawdown=-0.02,
                max_drawdown=-0.05,
                position_count=0,
                created_at=utc_now(),
            )
        )
        session.commit()

    def loader(tickers, requested_as_of, lookback_days):
        if lookback_days >= 65:
            return {"005930": _kr_bars("005930", [70000 + index * 120 for index in range(90)])}
        return {}

    provider = KRStrategyDataProvider(price_history_loader=loader, settings=settings)
    trader = AutoTrader(
        data_provider=provider,
        universe_loader=lambda market, timestamp: ["005930"],
        settings=settings,
    )

    result = trader.run_cycle("KR", as_of)

    assert result.signals_generated == 1
    assert result.rejected_signals == []
    assert len(result.order_candidates) == 1
    assert result.order_candidates[0].signal.ticker == "005930"

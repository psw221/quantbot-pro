from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from data.database import BacktestResult, SystemLog, get_session_factory, init_db
from execution.writer_queue import WriterQueue
from backtest.backtest_runner import BacktestRunner
from core.models import EventFlag, FactorSnapshot, PriceBar
from monitor.operations import OperationsRecorder
from tests.test_execution.test_bootstrap import build_settings


class FakeStrategyDataProvider:
    def __init__(self) -> None:
        start = datetime(2025, 4, 20, tzinfo=UTC)
        self._price_history: dict[str, list[PriceBar]] = {
            "AAPL": [
                PriceBar(
                    ticker="AAPL",
                    market="US",
                    timestamp=start + timedelta(days=index),
                    close=100 + index,
                    high=101 + index,
                    low=99 + index,
                )
                for index in range(270)
            ],
            "MSFT": [
                PriceBar(
                    ticker="MSFT",
                    market="US",
                    timestamp=start + timedelta(days=index),
                    close=100 + (index * 0.05),
                    high=101 + (index * 0.05),
                    low=99 + (index * 0.05),
                )
                for index in range(270)
            ],
        }

    def get_price_history(self, tickers, market, as_of, lookback_days):
        result = {}
        for ticker in tickers:
            bars = [bar for bar in self._price_history[ticker] if bar.timestamp <= as_of]
            result[ticker] = bars[-lookback_days:]
        return result

    def get_factor_inputs(self, tickers, market, as_of):
        return {
            ticker: FactorSnapshot(
                ticker=ticker,
                market=market,
                value_score=1.0,
                quality_score=1.0,
                momentum_score=1.0,
                low_vol_score=1.0,
            )
            for ticker in tickers
        }

    def get_event_flags(self, tickers, market, as_of):
        return []


def test_backtest_runner_persists_result_and_system_log(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()

    try:
        runner = BacktestRunner(
            data_provider=FakeStrategyDataProvider(),
            writer_queue=writer_queue,
            operations_recorder=OperationsRecorder(writer_queue),
            settings=settings,
        )
        result = runner.run(
            "dual_momentum",
            "US",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 10, tzinfo=UTC),
            universe=["AAPL", "MSFT"],
            persist=True,
        )
    finally:
        writer_queue.stop()

    assert result.backtest_result_id is not None
    assert result.total_trades >= 1
    assert result.engine in {"vectorbt", "fallback"}

    session_factory = get_session_factory()
    with session_factory() as session:
        row = session.query(BacktestResult).one()
        assert row.strategy == "dual_momentum"
        assert row.market == "US"
        assert "engine=" in (row.notes or "")
        log_row = session.query(SystemLog).one()
        assert log_row.module == "backtest.backtest_runner"


def test_backtest_runner_rejects_invalid_period(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()

    try:
        runner = BacktestRunner(
            data_provider=FakeStrategyDataProvider(),
            writer_queue=writer_queue,
            operations_recorder=OperationsRecorder(writer_queue),
            settings=settings,
        )
        try:
            runner.run(
                "dual_momentum",
                "US",
                datetime(2026, 1, 10, tzinfo=UTC),
                datetime(2026, 1, 1, tzinfo=UTC),
                universe=["AAPL"],
            )
        except ValueError as exc:
            assert "end_date" in str(exc)
        else:
            raise AssertionError("expected ValueError")
    finally:
        writer_queue.stop()


def test_backtest_runner_persists_actual_engine_metadata(tmp_path, monkeypatch) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()

    try:
        runner = BacktestRunner(
            data_provider=FakeStrategyDataProvider(),
            writer_queue=writer_queue,
            operations_recorder=OperationsRecorder(writer_queue),
            settings=settings,
        )

        def fake_run_engine(*args, **kwargs):
            return {
                "annual_return": 0.12,
                "sharpe_ratio": 1.5,
                "max_drawdown": -0.08,
                "win_rate": 0.6,
                "total_trades": 4,
                "profit_factor": 1.9,
                "engine": "vectorbt",
            }

        monkeypatch.setattr(runner, "_run_engine", fake_run_engine)

        result = runner.run(
            "dual_momentum",
            "US",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 10, tzinfo=UTC),
            universe=["AAPL", "MSFT"],
            persist=True,
        )
    finally:
        writer_queue.stop()

    assert result.engine == "vectorbt"

    session_factory = get_session_factory()
    with session_factory() as session:
        row = session.query(BacktestResult).one()
        assert row.notes == "engine=vectorbt"

        log_row = session.query(SystemLog).one()
        payload = json.loads(log_row.extra_json or "{}")
        assert payload["engine"] == "vectorbt"


def test_backtest_runner_rejects_empty_universe(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()

    try:
        runner = BacktestRunner(
            data_provider=FakeStrategyDataProvider(),
            writer_queue=writer_queue,
            operations_recorder=OperationsRecorder(writer_queue),
            settings=settings,
        )
        try:
            runner.run(
                "dual_momentum",
                "US",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 10, tzinfo=UTC),
                universe=[],
            )
        except ValueError as exc:
            assert "universe" in str(exc)
        else:
            raise AssertionError("expected ValueError")
    finally:
        writer_queue.stop()

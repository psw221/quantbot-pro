from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data.database import (
    BacktestResult,
    Order,
    OrderExecution,
    PortfolioSnapshot,
    ReconciliationRun,
    Signal as SignalRow,
    SystemLog,
    Trade,
    get_session_factory,
    init_db,
    utc_now,
)
from execution.runtime import TradingRuntime
from execution.writer_queue import WriterQueue
from monitor.dashboard import build_dashboard_snapshot, build_read_only_dashboard_snapshot, dashboard_snapshot_to_dict
from tests.test_execution.test_bootstrap import build_settings


KST = timezone(timedelta(hours=9))


def test_dashboard_snapshot_aggregates_runtime_and_recent_db_rows(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    runtime = TradingRuntime(writer_queue=writer_queue, settings=settings)
    session_factory = get_session_factory()
    reference_now = datetime(2026, 4, 16, 10, 0, tzinfo=KST)

    with session_factory() as session:
        signal_row = SignalRow(
            ticker="AAPL",
            market="US",
            strategy="dual_momentum",
            action="buy",
            strength=1.0,
            reason="dashboard fixture",
            status="ordered",
            generated_at=reference_now,
            processed_at=reference_now,
        )
        session.add(signal_row)
        session.flush()
        order_row = Order(
            client_order_id="dash-order",
            kis_order_no="B-100",
            signal_id=signal_row.id,
            ticker="AAPL",
            market="US",
            strategy="dual_momentum",
            side="buy",
            order_type="limit",
            quantity=3,
            price=180,
            status="submitted",
            submitted_at=reference_now,
            updated_at=reference_now,
        )
        session.add(order_row)
        session.flush()
        execution_row = OrderExecution(
            order_id=order_row.id,
            execution_no="dash-exec",
            fill_seq=1,
            filled_quantity=3,
            filled_price=180,
            fee=1,
            tax=0,
            currency="USD",
            trade_fx_rate=1320,
            settlement_date=reference_now,
            settlement_fx_rate=1330,
            fx_rate_source="test",
            executed_at=reference_now - timedelta(minutes=30),
            created_at=utc_now(),
        )
        session.add(execution_row)
        session.flush()
        session.add(
            Trade(
                order_id=order_row.id,
                execution_id=execution_row.id,
                ticker="AAPL",
                market="US",
                strategy="dual_momentum",
                side="buy",
                quantity=3,
                price=180,
                amount=540,
                fee=1,
                tax=0,
                net_amount=541,
                currency="USD",
                trade_fx_rate=1320,
                settlement_date=reference_now,
                settlement_fx_rate=1330,
                fx_rate_source="test",
                signal_id=None,
                executed_at=reference_now - timedelta(minutes=30),
                created_at=utc_now(),
            )
        )
        session.add(
            PortfolioSnapshot(
                snapshot_date=reference_now - timedelta(days=1),
                total_value_krw=12000000,
                cash_krw=2000000,
                domestic_value_krw=5000000,
                overseas_value_krw=5000000,
                usd_krw_rate=1330,
                daily_return=0.01,
                cumulative_return=0.12,
                drawdown=-0.03,
                max_drawdown=-0.10,
                position_count=7,
                created_at=utc_now(),
            )
        )
        session.add(
            ReconciliationRun(
                run_type="scheduled_poll",
                source_env="vts",
                started_at=reference_now - timedelta(hours=2),
                completed_at=reference_now - timedelta(hours=2) + timedelta(minutes=1),
                mismatch_count=2,
                status="warning",
                summary_json="{}",
                created_at=utc_now(),
            )
        )
        session.add(
            ReconciliationRun(
                run_type="manual_restore",
                source_env="vts",
                started_at=reference_now - timedelta(hours=1),
                completed_at=reference_now - timedelta(hours=1) + timedelta(minutes=2),
                mismatch_count=1,
                status="warning",
                summary_json="{}",
                created_at=utc_now(),
            )
        )
        session.add(
            BacktestResult(
                strategy="dual_momentum",
                market="US",
                start_date=reference_now - timedelta(days=30),
                end_date=reference_now - timedelta(days=1),
                params_json='{"universe":["AAPL","MSFT"]}',
                annual_return=0.12,
                sharpe_ratio=1.4,
                max_drawdown=-0.08,
                win_rate=0.6,
                total_trades=5,
                profit_factor=1.8,
                notes="engine=vectorbt",
                created_at=reference_now - timedelta(minutes=10),
            )
        )
        session.add(
            SystemLog(
                level="ERROR",
                module="execution.runtime",
                message="polling mismatch detected",
                extra_json=None,
                created_at=reference_now - timedelta(minutes=5),
            )
        )
        session.commit()

    runtime.state.scheduler_running = True
    runtime.state.last_token_refresh_at = reference_now - timedelta(hours=1)
    runtime.state.last_poll_success_at = reference_now - timedelta(minutes=5)
    snapshot = build_dashboard_snapshot(runtime, now=reference_now.astimezone(timezone.utc))
    payload = dashboard_snapshot_to_dict(snapshot)

    assert snapshot.health.scheduler_running is True
    assert len(snapshot.open_orders) == 1
    assert snapshot.open_orders[0]["ticker"] == "AAPL"
    assert len(snapshot.recent_trades) == 1
    assert snapshot.latest_portfolio_snapshot is not None
    assert snapshot.latest_portfolio_snapshot["position_count"] == 7
    assert snapshot.reconciliation_summary["warning_count"] == 2
    assert snapshot.reconciliation_summary["mismatch_total"] == 3
    assert snapshot.reconciliation_summary["latest_run_type"] == "manual_restore"
    assert len(snapshot.recent_manual_restores) == 1
    assert snapshot.recent_manual_restores[0]["status"] == "warning"
    assert len(snapshot.recent_backtests) == 1
    assert snapshot.recent_backtests[0]["notes"] == "engine=vectorbt"
    assert snapshot.operational_summary["health_status"] == snapshot.health.status.value
    assert snapshot.operational_summary["trading_blocked"] is False
    assert snapshot.operational_summary["has_recent_mismatch"] is True
    assert snapshot.operational_summary["recent_manual_restore_count"] == 1
    assert snapshot.operational_summary["latest_manual_restore_status"] == "warning"
    assert snapshot.operational_summary["recent_backtest_count"] == 1
    assert snapshot.operational_summary["latest_backtest_strategy"] == "dual_momentum"
    assert len(snapshot.recent_logs) == 1
    assert payload["health"]["status"] == snapshot.health.status
    assert payload["health"]["details"]["status_source"] == "external_canonical"
    assert payload["operational_summary"]["latest_reconciliation_run_type"] == "manual_restore"


def test_dashboard_snapshot_handles_empty_read_models(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    runtime = TradingRuntime(writer_queue=WriterQueue(), settings=settings)
    reference_now = datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc)

    snapshot = build_dashboard_snapshot(runtime, now=reference_now)

    assert snapshot.open_orders == []
    assert snapshot.recent_trades == []
    assert snapshot.latest_portfolio_snapshot is None
    assert snapshot.reconciliation_summary["run_count"] == 0
    assert snapshot.recent_manual_restores == []
    assert snapshot.recent_backtests == []
    assert snapshot.operational_summary["recent_manual_restore_count"] == 0
    assert snapshot.operational_summary["recent_backtest_count"] == 0
    assert snapshot.operational_summary["has_recent_mismatch"] is False
    assert snapshot.recent_logs == []


def test_read_only_dashboard_snapshot_builds_health_from_db_rows(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    session_factory = get_session_factory()
    reference_now = datetime(2026, 4, 21, 13, 0, tzinfo=timezone.utc)

    with session_factory() as session:
        session.add(
            PortfolioSnapshot(
                snapshot_date=reference_now - timedelta(days=1),
                total_value_krw=10000000,
                cash_krw=2500000,
                domestic_value_krw=7500000,
                overseas_value_krw=0,
                usd_krw_rate=1330,
                daily_return=0.0,
                cumulative_return=0.01,
                drawdown=-0.01,
                max_drawdown=-0.05,
                position_count=2,
                created_at=utc_now(),
            )
        )
        session.add(
            ReconciliationRun(
                run_type="scheduled_poll",
                source_env="vts",
                started_at=reference_now - timedelta(minutes=12),
                completed_at=reference_now - timedelta(minutes=11),
                mismatch_count=0,
                status="ok",
                summary_json="{}",
                created_at=utc_now(),
            )
        )
        session.add(
            SystemLog(
                level="INFO",
                module="execution.runtime",
                message="dashboard fixture",
                extra_json=None,
                created_at=reference_now - timedelta(minutes=5),
            )
        )
        from data.database import TokenStore

        session.add(
            TokenStore(
                env="vts",
                expires_at=reference_now + timedelta(hours=23),
                issued_at=reference_now - timedelta(hours=2),
                is_valid=True,
            )
        )
        session.commit()

    snapshot = build_read_only_dashboard_snapshot(env="vts", now=reference_now)

    assert snapshot.health.details["status_source"] == "external_canonical"
    assert snapshot.health.token_stale is False
    assert snapshot.health.poll_stale is False
    assert snapshot.health.trading_blocked is False
    assert snapshot.latest_portfolio_snapshot is not None
    assert snapshot.reconciliation_summary["latest_status"] == "ok"

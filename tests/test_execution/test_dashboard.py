from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data.database import (
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
from monitor.dashboard import build_dashboard_snapshot, dashboard_snapshot_to_dict
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
    assert snapshot.reconciliation_summary["warning_count"] == 1
    assert snapshot.reconciliation_summary["mismatch_total"] == 2
    assert len(snapshot.recent_logs) == 1
    assert payload["health"]["status"] == snapshot.health.status


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
    assert snapshot.recent_logs == []

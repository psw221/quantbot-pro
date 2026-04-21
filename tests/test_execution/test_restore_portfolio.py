from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data.database import BrokerPosition, Order, PortfolioSnapshot, Position, ReconciliationRun, Signal as SignalRow, SystemLog, get_session_factory, init_db
from execution.order_manager import OrderManager
from execution.reconciliation import ReconciliationService
from execution.writer_queue import WriterQueue
from monitor.operations import OperationsRecorder
from scripts.restore_portfolio import RestorePortfolioService
from tests.test_execution.test_bootstrap import build_settings
from core.models import BrokerOrderSnapshot, BrokerPollingSnapshot, BrokerPositionSnapshot


UTC = timezone.utc


class RecordingTelegramNotifier:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def send_event(
        self,
        event_type: str,
        message: str,
        context: dict[str, object] | None = None,
        *,
        severity: str = "warning",
        title: str | None = None,
        created_at=None,
        source_env: str | None = None,
    ) -> None:
        self.events.append(
            {
                "event_type": event_type,
                "message": message,
                "context": dict(context or {}),
                "severity": severity,
                "title": title,
                "source_env": source_env,
            }
        )


class FailingReconciliationService:
    def reconcile_snapshot(self, snapshot, run_type: str = "scheduled_poll"):  # noqa: ANN001
        raise RuntimeError("forced reconciliation failure")


def _build_snapshot(reference_now: datetime) -> BrokerPollingSnapshot:
    return BrokerPollingSnapshot(
        positions=[
            BrokerPositionSnapshot(
                ticker="AAPL",
                market="US",
                quantity=3,
                avg_cost=180,
                currency="USD",
                snapshot_at=reference_now,
                source_env="vts",
            )
        ],
        open_orders=[
            BrokerOrderSnapshot(
                order_no="BROKER-ONLY",
                ticker="AAPL",
                market="US",
                side="buy",
                quantity=3,
                remaining_quantity=3,
                status="submitted",
                price=180,
            )
        ],
        cash_available=2500,
        raw_payloads={
            "portfolio_snapshot": {
                "snapshot_date": reference_now.isoformat(),
                "total_value_krw": 12300000,
                "cash_krw": 2000000,
                "domestic_value_krw": 5000000,
                "overseas_value_krw": 5300000,
                "usd_krw_rate": 1325,
                "position_count": 1,
            }
        },
    )


def _seed_internal_state() -> None:
    session_factory = get_session_factory()
    reference_now = datetime(2026, 4, 16, 0, 0, tzinfo=UTC)
    with session_factory() as session:
        signal_row = SignalRow(
            ticker="AAPL",
            market="US",
            strategy="dual_momentum",
            action="buy",
            strength=1.0,
            reason="restore fixture",
            status="ordered",
            generated_at=reference_now,
            processed_at=reference_now,
        )
        session.add(signal_row)
        session.flush()
        session.add(
            Position(
                ticker="AAPL",
                market="US",
                strategy="dual_momentum",
                quantity=5,
                avg_cost=180,
                current_price=180,
                highest_price=180,
                entry_date=reference_now,
                updated_at=reference_now,
            )
        )
        session.add(
            Order(
                client_order_id="restore-order",
                kis_order_no="INTERNAL-ONLY",
                signal_id=signal_row.id,
                ticker="AAPL",
                market="US",
                strategy="dual_momentum",
                side="buy",
                order_type="limit",
                quantity=5,
                price=180,
                status="submitted",
                submitted_at=reference_now,
                updated_at=reference_now,
            )
        )
        session.commit()


def test_restore_portfolio_preview_reports_position_and_order_mismatches(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    _seed_internal_state()
    writer_queue = WriterQueue()
    writer_queue.start()
    reference_now = datetime(2026, 4, 16, 9, 0, tzinfo=UTC)
    notifier = RecordingTelegramNotifier()

    try:
        service = RestorePortfolioService(
            writer_queue=writer_queue,
            reconciliation_service=ReconciliationService(writer_queue=writer_queue, settings=settings),
            order_manager=OrderManager(writer_queue=writer_queue, settings=settings),
            operations_recorder=OperationsRecorder(writer_queue),
            telegram_notifier=notifier,
            settings=settings,
        )
        summary = service.preview(_build_snapshot(reference_now), market="US")
    finally:
        writer_queue.stop()

    assert summary.mode == "dry-run"
    assert summary.mismatch_count == 3
    assert summary.position_mismatches[0]["internal_quantity"] == 5
    assert {item["reason"] for item in summary.order_mismatches} == {"broker_only", "internal_only"}
    assert notifier.events == []

    session_factory = get_session_factory()
    with session_factory() as session:
        assert session.query(BrokerPosition).count() == 0
        assert session.query(ReconciliationRun).count() == 0
        assert session.query(SystemLog).count() == 0
        assert session.query(PortfolioSnapshot).count() == 0


def test_restore_portfolio_apply_records_reconciliation_logs_and_snapshot(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    _seed_internal_state()
    writer_queue = WriterQueue()
    writer_queue.start()
    reference_now = datetime(2026, 4, 16, 9, 0, tzinfo=UTC)
    notifier = RecordingTelegramNotifier()

    try:
        order_manager = OrderManager(writer_queue=writer_queue, settings=settings)
        order_manager.trading_blocked = True
        service = RestorePortfolioService(
            writer_queue=writer_queue,
            reconciliation_service=ReconciliationService(writer_queue=writer_queue, settings=settings),
            order_manager=order_manager,
            operations_recorder=OperationsRecorder(writer_queue),
            telegram_notifier=notifier,
            settings=settings,
        )
        summary = service.restore(_build_snapshot(reference_now), market="US", apply=True)
    finally:
        writer_queue.stop()

    assert summary.mode == "apply"
    assert summary.reconciliation_status == "mismatch_detected"
    assert order_manager.reconciliation_status.value == "mismatch_detected"
    assert [event["event_type"] for event in notifier.events] == [
        "dr_restore_started",
        "dr_restore_completed",
    ]
    assert notifier.events[1]["context"]["status"] == "mismatch_detected"

    session_factory = get_session_factory()
    with session_factory() as session:
        assert session.query(BrokerPosition).count() == 1
        restore_run = (
            session.query(ReconciliationRun)
            .filter(ReconciliationRun.run_type == "manual_restore")
            .order_by(ReconciliationRun.id.desc())
            .first()
        )
        assert restore_run is not None
        assert restore_run.run_type == "manual_restore"
        assert session.query(SystemLog).count() >= 2
        snapshot_row = session.query(PortfolioSnapshot).one()
        assert snapshot_row.total_value_krw == 12300000


def test_restore_portfolio_apply_requires_trading_block_confirmation(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()

    try:
        service = RestorePortfolioService(
            writer_queue=writer_queue,
            reconciliation_service=ReconciliationService(writer_queue=writer_queue, settings=settings),
            order_manager=OrderManager(writer_queue=writer_queue, settings=settings),
            operations_recorder=OperationsRecorder(writer_queue),
            settings=settings,
        )
        try:
            service.restore(_build_snapshot(datetime(2026, 4, 16, 9, 0, tzinfo=UTC)), market="US", apply=True)
        except RuntimeError as exc:
            assert "trading_blocked" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        writer_queue.stop()


def test_restore_portfolio_apply_emits_failure_notification(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    notifier = RecordingTelegramNotifier()

    try:
        order_manager = OrderManager(writer_queue=writer_queue, settings=settings)
        order_manager.trading_blocked = True
        service = RestorePortfolioService(
            writer_queue=writer_queue,
            reconciliation_service=FailingReconciliationService(),
            order_manager=order_manager,
            operations_recorder=OperationsRecorder(writer_queue),
            telegram_notifier=notifier,
            settings=settings,
        )
        try:
            service.restore(_build_snapshot(datetime(2026, 4, 16, 9, 0, tzinfo=UTC)), market="US", apply=True)
        except RuntimeError as exc:
            assert "forced reconciliation failure" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        writer_queue.stop()

    assert [event["event_type"] for event in notifier.events] == [
        "dr_restore_started",
        "dr_restore_failed",
    ]
    assert notifier.events[1]["severity"] == "critical"

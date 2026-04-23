from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from core.models import BrokerFillSnapshot, BrokerPollingSnapshot, BrokerPositionSnapshot, ExecutionFill, Signal
from data.database import BrokerPosition, Order, Position, PositionLot, ReconciliationRun, Signal as SignalRow, get_read_session, init_db
from execution.fill_processor import FillProcessor
from execution.order_manager import OrderManager
from execution.reconciliation import ReconciliationService
from execution.writer_queue import WriterQueue
from monitor.operations import OperationsRecorder
from scripts.repair_manual_fills import MANUAL_REPAIR_REASON, ManualFillRepairService
from tests.test_execution.test_bootstrap import build_settings


UTC = timezone.utc


def _seed_buys(writer_queue: WriterQueue, settings, *, ticker: str = "005930") -> FillProcessor:
    manager = OrderManager(writer_queue=writer_queue, settings=settings)
    processor = FillProcessor(writer_queue)
    base_time = datetime(2026, 4, 23, 0, 0, tzinfo=UTC)

    for index, price in enumerate((100.0, 120.0), start=1):
        signal = Signal(
            ticker=ticker,
            market="KR",
            action="buy",
            strategy="trend_following",
            strength=1.0,
            reason=f"seed-buy-{index}",
        )
        signal_id = manager.persist_signal(signal)
        intent = manager.create_order_intent(signal, signal_id=signal_id, quantity=1, price=price)
        submission = manager.persist_validated_order(intent)
        manager.mark_submission_result(submission.order_id, broker_order_no=f"BUY-{index}", accepted=True)
        processor.process_fill(
            ExecutionFill(
                order_id=submission.order_id,
                execution_no=f"BUY-FILL-{index}",
                fill_seq=1,
                filled_quantity=1,
                filled_price=price,
                fee=0.0,
                tax=0.0,
                executed_at=base_time + timedelta(minutes=index),
            )
        )

    return processor


def _build_snapshot(reference_now: datetime, *, quantity: int) -> BrokerPollingSnapshot:
    return BrokerPollingSnapshot(
        positions=[
            BrokerPositionSnapshot(
                ticker="005930",
                market="KR",
                quantity=quantity,
                avg_cost=120.0,
                currency="KRW",
                snapshot_at=reference_now,
                source_env="vts",
            )
        ],
        open_orders=[],
        cash_available=1000000.0,
        raw_payloads={},
    )


def test_manual_fill_repair_replays_broker_sell_and_reconciles(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()

    try:
        fill_processor = _seed_buys(writer_queue, settings)
        service = ManualFillRepairService(
            writer_queue=writer_queue,
            reconciliation_service=ReconciliationService(writer_queue=writer_queue, settings=settings),
            fill_processor=fill_processor,
            operations_recorder=OperationsRecorder(writer_queue),
            runtime_pid_path=tmp_path / "missing.pid",
            settings=settings,
        )
        summary = service.repair(
            _build_snapshot(datetime(2026, 4, 23, 9, 0, tzinfo=UTC), quantity=1),
            market="KR",
            ticker="005930",
            strategy="trend_following",
            fill_snapshots=[
                BrokerFillSnapshot(
                    order_no="MANUAL-SELL-1",
                    order_orgno="06010",
                    ticker="005930",
                    market="KR",
                    side="sell",
                    order_quantity=1,
                    cumulative_filled_quantity=1,
                    remaining_quantity=0,
                    average_filled_price=130.0,
                    occurred_at=datetime(2026, 4, 23, 9, 1, tzinfo=UTC),
                    execution_hint="MSELL-1",
                )
            ],
            apply=True,
        )
    finally:
        writer_queue.stop()

    assert summary.mode == "apply"
    assert summary.internal_quantity_before == 2
    assert summary.internal_quantity_after == 1
    assert summary.reconciliation_status == "reconciled"

    with get_read_session() as session:
        position = session.scalar(select(Position).where(Position.ticker == "005930"))
        lots = list(session.scalars(select(PositionLot).where(PositionLot.ticker == "005930").order_by(PositionLot.opened_at)))
        sell_order = session.scalar(
            select(Order).where(
                Order.ticker == "005930",
                Order.side == "sell",
                Order.kis_order_no == "MANUAL-SELL-1",
            )
        )
        restore_run = (
            session.query(ReconciliationRun)
            .filter(ReconciliationRun.run_type == "manual_restore")
            .order_by(ReconciliationRun.id.desc())
            .first()
        )

    assert position is not None
    assert position.quantity == 1
    assert position.avg_cost == 120.0
    assert [lot.remaining_quantity for lot in lots] == [0, 1]
    assert sell_order is not None
    assert sell_order.status == "filled"
    assert sell_order.price == 130.0
    assert restore_run is not None
    assert restore_run.status == "ok"


def test_manual_fill_repair_requires_exact_quantity_match(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()

    try:
        fill_processor = _seed_buys(writer_queue, settings)
        service = ManualFillRepairService(
            writer_queue=writer_queue,
            reconciliation_service=ReconciliationService(writer_queue=writer_queue, settings=settings),
            fill_processor=fill_processor,
            operations_recorder=OperationsRecorder(writer_queue),
            runtime_pid_path=tmp_path / "missing.pid",
            settings=settings,
        )
        with pytest.raises(RuntimeError, match="exactly match"):
            service.preview(
                _build_snapshot(datetime(2026, 4, 23, 9, 0, tzinfo=UTC), quantity=1),
                market="KR",
                ticker="005930",
                strategy="trend_following",
                fill_snapshots=[
                    BrokerFillSnapshot(
                        order_no="MANUAL-SELL-2",
                        order_orgno="06010",
                        ticker="005930",
                        market="KR",
                        side="sell",
                        order_quantity=2,
                        cumulative_filled_quantity=2,
                        remaining_quantity=0,
                        average_filled_price=130.0,
                        occurred_at=datetime(2026, 4, 23, 9, 1, tzinfo=UTC),
                        execution_hint="MSELL-2",
                    )
                ],
            )
    finally:
        writer_queue.stop()


def test_manual_fill_repair_marks_manual_sell_reason(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()

    try:
        fill_processor = _seed_buys(writer_queue, settings)
        service = ManualFillRepairService(
            writer_queue=writer_queue,
            reconciliation_service=ReconciliationService(writer_queue=writer_queue, settings=settings),
            fill_processor=fill_processor,
            operations_recorder=OperationsRecorder(writer_queue),
            runtime_pid_path=tmp_path / "missing.pid",
            settings=settings,
        )
        service.repair(
            _build_snapshot(datetime(2026, 4, 23, 9, 0, tzinfo=UTC), quantity=1),
            market="KR",
            ticker="005930",
            strategy="trend_following",
            fill_snapshots=[
                BrokerFillSnapshot(
                    order_no="MANUAL-SELL-3",
                    order_orgno="06010",
                    ticker="005930",
                    market="KR",
                    side="sell",
                    order_quantity=1,
                    cumulative_filled_quantity=1,
                    remaining_quantity=0,
                    average_filled_price=130.0,
                    occurred_at=datetime(2026, 4, 23, 9, 1, tzinfo=UTC),
                    execution_hint="MSELL-3",
                )
            ],
            apply=True,
        )
    finally:
        writer_queue.stop()

    with get_read_session() as session:
        signal_row = session.scalar(
            select(SignalRow).where(
                SignalRow.ticker == "005930",
                SignalRow.strategy == "trend_following",
                SignalRow.reason == MANUAL_REPAIR_REASON,
            )
        )

    assert signal_row is not None
    assert signal_row.action == "sell"


def test_reconciliation_service_skips_duplicate_broker_snapshot_insert(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()

    try:
        service = ReconciliationService(writer_queue=writer_queue, settings=settings)
        snapshot = _build_snapshot(datetime(2026, 4, 23, 9, 0, tzinfo=UTC), quantity=1)
        first = service.reconcile_snapshot(snapshot, run_type="manual_restore")
        second = service.reconcile_snapshot(snapshot, run_type="manual_restore")
    finally:
        writer_queue.stop()

    assert first.status.value == "mismatch_detected"
    assert second.status.value == "mismatch_detected"

    with get_read_session() as session:
        assert session.query(BrokerPosition).count() == 1
        assert session.query(ReconciliationRun).count() == 2

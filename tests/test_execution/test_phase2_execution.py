from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from core.models import (
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    ExecutionFill,
    OrderStatus,
    ReconciliationStatus,
    Signal,
)
from data.database import Order, Position, PositionLot, Signal as SignalRow, TaxEvent, get_read_session, init_db
from execution.fill_processor import FillProcessor
from execution.order_manager import OrderManager
from execution.reconciliation import ReconciliationService
from execution.writer_queue import WriterQueue
from tests.test_execution.test_bootstrap import build_settings


def test_order_manager_creates_validated_order(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)

    try:
        signal = Signal(
            ticker="005930",
            market="KR",
            action="buy",
            strategy="dual_momentum",
            strength=1.0,
            reason="entry",
        )
        signal_id = manager.persist_signal(signal)
        intent = manager.create_order_intent(signal, signal_id=signal_id, quantity=3)
        submission = manager.persist_validated_order(intent)
    finally:
        writer_queue.stop()

    assert submission.status == OrderStatus.VALIDATED
    with get_read_session() as session:
        order = session.get(Order, submission.order_id)
    assert order is not None
    assert order.status == OrderStatus.VALIDATED.value


def test_order_manager_retries_and_fails_after_limit(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)

    try:
        signal = Signal(
            ticker="005930",
            market="KR",
            action="buy",
            strategy="dual_momentum",
            strength=1.0,
            reason="entry",
        )
        signal_id = manager.persist_signal(signal)
        intent = manager.create_order_intent(signal, signal_id=signal_id, quantity=3)
        submission = manager.persist_validated_order(intent)
        manager.record_submit_failure(submission.order_id, error_message="temporary", error_code="E1", retryable=True)
        manager.record_submit_failure(submission.order_id, error_message="temporary", error_code="E1", retryable=True)
        manager.record_submit_failure(submission.order_id, error_message="fatal", error_code="E2", retryable=True)
    finally:
        writer_queue.stop()

    with get_read_session() as session:
        order = session.get(Order, submission.order_id)

    assert order is not None
    assert order.retry_count == 3
    assert order.status == OrderStatus.FAILED.value
    assert order.error_code == "E2"


def test_order_manager_cancel_flow(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)

    try:
        signal = Signal(ticker="AAPL", market="US", action="buy", strategy="dual_momentum", strength=1.0, reason="entry")
        signal_id = manager.persist_signal(signal)
        intent = manager.create_order_intent(signal, signal_id=signal_id, quantity=2, price=100)
        submission = manager.persist_validated_order(intent)
        manager.mark_submission_result(submission.order_id, broker_order_no="B-1", accepted=True)
        manager.request_cancel(submission.order_id)
        manager.confirm_cancel(submission.order_id)
    finally:
        writer_queue.stop()

    with get_read_session() as session:
        order = session.get(Order, submission.order_id)

    assert order is not None
    assert order.status == OrderStatus.CANCELLED.value


def test_fill_processor_handles_partial_and_full_fill(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)
    processor = FillProcessor(writer_queue)

    try:
        signal = Signal(ticker="AAPL", market="US", action="buy", strategy="factor_investing", strength=1.0, reason="entry")
        signal_id = manager.persist_signal(signal)
        intent = manager.create_order_intent(signal, signal_id=signal_id, quantity=10, price=100)
        submission = manager.persist_validated_order(intent)
        manager.mark_submission_result(submission.order_id, broker_order_no="B-1", accepted=True)

        processor.process_fill(
            ExecutionFill(
                order_id=submission.order_id,
                execution_no="E-1",
                fill_seq=1,
                filled_quantity=4,
                filled_price=100,
                fee=1,
                tax=0,
                executed_at=datetime.now(timezone.utc),
                currency="USD",
                trade_fx_rate=1300,
                settlement_date=datetime.now(timezone.utc),
                settlement_fx_rate=1310,
                fx_rate_source="test",
            )
        )
        processor.process_fill(
            ExecutionFill(
                order_id=submission.order_id,
                execution_no="E-2",
                fill_seq=2,
                filled_quantity=6,
                filled_price=101,
                fee=1,
                tax=0,
                executed_at=datetime.now(timezone.utc),
                currency="USD",
                trade_fx_rate=1305,
                settlement_date=datetime.now(timezone.utc),
                settlement_fx_rate=1315,
                fx_rate_source="test",
            )
        )
    finally:
        writer_queue.stop()

    with get_read_session() as session:
        order = session.get(Order, submission.order_id)
        position = session.scalar(select(Position).where(Position.ticker == "AAPL"))
        lots = list(session.scalars(select(PositionLot).where(PositionLot.ticker == "AAPL").order_by(PositionLot.opened_at)))
        signal_row = session.get(SignalRow, signal_id)

    assert order is not None
    assert order.status == OrderStatus.FILLED.value
    assert position is not None
    assert position.quantity == 10
    assert len(lots) == 2
    assert lots[0].open_settlement_fx_rate == 1310
    assert signal_row is not None
    assert signal_row.status == "ordered"


def test_fill_processor_creates_tax_hook_for_us_sell(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)
    processor = FillProcessor(writer_queue)

    try:
        buy_signal = Signal(ticker="AAPL", market="US", action="buy", strategy="dual_momentum", strength=1.0, reason="entry")
        buy_signal_id = manager.persist_signal(buy_signal)
        buy_intent = manager.create_order_intent(buy_signal, signal_id=buy_signal_id, quantity=5, price=100)
        buy_submission = manager.persist_validated_order(buy_intent)
        manager.mark_submission_result(buy_submission.order_id, broker_order_no="BUY-1", accepted=True)
        processor.process_fill(
            ExecutionFill(
                order_id=buy_submission.order_id,
                execution_no="BUY-FILL",
                fill_seq=1,
                filled_quantity=5,
                filled_price=100,
                fee=0,
                tax=0,
                executed_at=datetime.now(timezone.utc),
                currency="USD",
                trade_fx_rate=1300,
                settlement_date=datetime.now(timezone.utc),
                settlement_fx_rate=1310,
                fx_rate_source="test",
            )
        )

        sell_signal = Signal(ticker="AAPL", market="US", action="sell", strategy="dual_momentum", strength=1.0, reason="exit")
        sell_signal_id = manager.persist_signal(sell_signal)
        sell_intent = manager.create_order_intent(sell_signal, signal_id=sell_signal_id, quantity=5, price=120)
        sell_submission = manager.persist_validated_order(sell_intent)
        manager.mark_submission_result(sell_submission.order_id, broker_order_no="SELL-1", accepted=True)
        processor.process_fill(
            ExecutionFill(
                order_id=sell_submission.order_id,
                execution_no="SELL-FILL",
                fill_seq=1,
                filled_quantity=5,
                filled_price=120,
                fee=0,
                tax=0,
                executed_at=datetime.now(timezone.utc),
                currency="USD",
                trade_fx_rate=1320,
                settlement_date=datetime.now(timezone.utc),
                settlement_fx_rate=1330,
                fx_rate_source="test",
            )
        )
    finally:
        writer_queue.stop()

    with get_read_session() as session:
        tax_event = session.scalar(select(TaxEvent).where(TaxEvent.ticker == "AAPL"))

    assert tax_event is not None
    assert tax_event.buy_settlement_fx_rate == 1310
    assert tax_event.sell_settlement_fx_rate == 1330


def test_reconciliation_service_flags_quantity_mismatch(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)
    processor = FillProcessor(writer_queue)
    reconciliation = ReconciliationService(writer_queue=writer_queue, settings=settings)

    try:
        signal = Signal(ticker="005930", market="KR", action="buy", strategy="trend_following", strength=1.0, reason="entry")
        signal_id = manager.persist_signal(signal)
        intent = manager.create_order_intent(signal, signal_id=signal_id, quantity=5, price=70000)
        submission = manager.persist_validated_order(intent)
        manager.mark_submission_result(submission.order_id, broker_order_no="KR-1", accepted=True)
        processor.process_fill(
            ExecutionFill(
                order_id=submission.order_id,
                execution_no="KR-FILL-1",
                fill_seq=1,
                filled_quantity=5,
                filled_price=70000,
                fee=0,
                tax=0,
                executed_at=datetime.now(timezone.utc),
            )
        )

        result = reconciliation.reconcile(
            broker_positions=[
                BrokerPositionSnapshot(
                    ticker="005930",
                    market="KR",
                    quantity=3,
                    avg_cost=70000,
                    currency="KRW",
                    snapshot_at=datetime.now(timezone.utc),
                    source_env="vts",
                )
            ],
            open_orders=[],
            cash_available=1000000,
        )
    finally:
        writer_queue.stop()

    assert result.status == ReconciliationStatus.MISMATCH_DETECTED
    assert result.summary["mismatch_count"] == 1


def test_reconciliation_service_accepts_broker_order_snapshot_input(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)
    reconciliation = ReconciliationService(writer_queue=writer_queue, settings=settings)

    try:
        signal = Signal(ticker="005930", market="KR", action="buy", strategy="trend_following", strength=1.0, reason="entry")
        signal_id = manager.persist_signal(signal)
        intent = manager.create_order_intent(signal, signal_id=signal_id, quantity=5, price=70000)
        submission = manager.persist_validated_order(intent)
        manager.mark_submission_result(submission.order_id, broker_order_no="KR-1", accepted=True)

        result = reconciliation.reconcile(
            broker_positions=[],
            open_orders=[
                BrokerOrderSnapshot(
                    order_no="KR-1",
                    ticker="005930",
                    market="KR",
                    side="buy",
                    quantity=5,
                    remaining_quantity=5,
                    status="submitted",
                )
            ],
            cash_available=1000000,
        )
    finally:
        writer_queue.stop()

    assert result.status == ReconciliationStatus.RECONCILED

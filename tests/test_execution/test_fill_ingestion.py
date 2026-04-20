from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from core.models import BrokerFillSnapshot, ExecutionFill, OrderStatus, Signal
from data.database import Order, OrderExecution, get_read_session, init_db
from execution.fill_ingestion import BrokerFillIngestionService
from execution.fill_processor import FillProcessor
from execution.order_manager import OrderManager
from execution.writer_queue import WriterQueue
from tests.test_execution.test_bootstrap import build_settings


def test_fill_ingestion_collects_new_fill_from_broker_snapshot(tmp_path) -> None:
    class DummyApiClient:
        def list_daily_order_fills(self, access_token, **kwargs):
            return {"rt_cd": "0", "output1": []}

        def normalize_daily_order_fills(self, payload, *, default_market="KR"):
            return [
                BrokerFillSnapshot(
                    order_no="KR-OPEN-1",
                    order_orgno="06010",
                    ticker="005930",
                    market="KR",
                    side="buy",
                    order_quantity=1,
                    cumulative_filled_quantity=1,
                    remaining_quantity=0,
                    average_filled_price=70000.0,
                    occurred_at=datetime(2026, 4, 15, 9, 1, tzinfo=timezone.utc),
                )
            ]

    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)
    service = BrokerFillIngestionService(api_client=DummyApiClient(), settings=settings)

    try:
        signal = Signal(ticker="005930", market="KR", action="buy", strategy="dual_momentum", strength=1.0, reason="entry")
        signal_id = manager.persist_signal(signal)
        intent = manager.create_order_intent(signal, signal_id=signal_id, quantity=1, price=70000)
        submission = manager.persist_validated_order(intent)
        manager.mark_submission_result(
            submission.order_id,
            broker_order_no="KR-OPEN-1",
            broker_order_orgno="06010",
            accepted=True,
        )

        fills = service.collect_execution_fills("token", market="KR")
    finally:
        writer_queue.stop()

    assert len(fills) == 1
    fill = fills[0]
    assert fill.order_id == submission.order_id
    assert fill.fill_seq == 1
    assert fill.filled_quantity == 1
    assert fill.filled_price == 70000.0
    assert fill.execution_no.startswith("KR-SYNC-KR-OPEN-1-1-1-")


def test_fill_ingestion_derives_delta_fill_price_from_cumulative_average(tmp_path) -> None:
    class DummyApiClient:
        def list_daily_order_fills(self, access_token, **kwargs):
            return {"rt_cd": "0", "output1": []}

        def normalize_daily_order_fills(self, payload, *, default_market="KR"):
            return [
                BrokerFillSnapshot(
                    order_no="KR-OPEN-1",
                    order_orgno="06010",
                    ticker="005930",
                    market="KR",
                    side="buy",
                    order_quantity=10,
                    cumulative_filled_quantity=10,
                    remaining_quantity=0,
                    average_filled_price=101.0,
                    occurred_at=datetime(2026, 4, 15, 9, 2, tzinfo=timezone.utc),
                )
            ]

    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)
    processor = FillProcessor(writer_queue)
    service = BrokerFillIngestionService(api_client=DummyApiClient(), settings=settings)

    try:
        signal = Signal(ticker="005930", market="KR", action="buy", strategy="dual_momentum", strength=1.0, reason="entry")
        signal_id = manager.persist_signal(signal)
        intent = manager.create_order_intent(signal, signal_id=signal_id, quantity=10, price=100)
        submission = manager.persist_validated_order(intent)
        manager.mark_submission_result(
            submission.order_id,
            broker_order_no="KR-OPEN-1",
            broker_order_orgno="06010",
            accepted=True,
        )
        processor.process_fill(
            ExecutionFill(
                order_id=submission.order_id,
                execution_no="KR-FILL-1",
                fill_seq=1,
                filled_quantity=4,
                filled_price=100.0,
                fee=0.0,
                tax=0.0,
                executed_at=datetime(2026, 4, 15, 9, 1, tzinfo=timezone.utc),
            )
        )

        fills = service.collect_execution_fills("token", market="KR")
    finally:
        writer_queue.stop()

    assert len(fills) == 1
    fill = fills[0]
    assert fill.fill_seq == 2
    assert fill.filled_quantity == 6
    assert fill.filled_price == 101.66666666666667


def test_fill_ingestion_returns_no_fills_for_us_market(tmp_path) -> None:
    class DummyApiClient:
        def list_daily_order_fills(self, access_token, **kwargs):
            raise AssertionError("should not query daily fills for US")

        def normalize_daily_order_fills(self, payload, *, default_market="KR"):
            raise AssertionError("should not normalize daily fills for US")

    settings = build_settings(tmp_path)
    init_db(settings)
    service = BrokerFillIngestionService(api_client=DummyApiClient(), settings=settings)

    fills = service.collect_execution_fills("token", market="US")

    assert fills == []

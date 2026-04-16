from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from core.models import ExecutionFill, Signal
from data.database import (
    Order,
    OrderExecution,
    Position,
    PositionLot,
    Signal as SignalRow,
    TaxEvent,
    Trade,
    get_read_session,
    get_session_factory,
    init_db,
    utc_now,
)
from execution.fill_processor import FillProcessor
from execution.order_manager import OrderManager
from execution.writer_queue import WriterQueue
from tax.tax_calculator import TaxCalculator
from tests.test_execution.test_bootstrap import build_settings


def test_tax_calculator_uses_tax_event_settlement_fx_for_us_sell(tmp_path) -> None:
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
                execution_no="BUY-FILL-TAX",
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
                execution_no="SELL-FILL-TAX",
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

    calculator = TaxCalculator()
    report = calculator.build_trade_report(datetime.now(timezone.utc).year, market="US")
    summary = calculator.calculate_yearly_summary(datetime.now(timezone.utc).year, market="US")

    assert len(report) == 1
    assert report[0]["source"] == "tax_event"
    assert report[0]["buy_fx_rate"] == 1310
    assert report[0]["sell_fx_rate"] == 1330
    assert report[0]["realized_gain_loss_krw"] == 143000
    assert summary["realized_gain_loss_krw"] == 143000


def test_tax_calculator_falls_back_to_trade_fx_when_settlement_fx_missing(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    session_factory = get_session_factory()

    with session_factory() as session:
        signal_row = SignalRow(
            ticker="MSFT",
            market="US",
            strategy="factor_investing",
            action="buy",
            strength=1.0,
            reason="fixture",
            status="ordered",
            generated_at=datetime.now(timezone.utc),
            processed_at=datetime.now(timezone.utc),
        )
        session.add(signal_row)
        session.flush()
        buy_order = Order(
            client_order_id="fixture-buy",
            kis_order_no="B1",
            signal_id=signal_row.id,
            ticker="MSFT",
            market="US",
            strategy="factor_investing",
            side="buy",
            order_type="limit",
            quantity=2,
            price=50,
            status="filled",
            submitted_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        sell_order = Order(
            client_order_id="fixture-sell",
            kis_order_no="S1",
            signal_id=signal_row.id,
            ticker="MSFT",
            market="US",
            strategy="factor_investing",
            side="sell",
            order_type="limit",
            quantity=2,
            price=60,
            status="filled",
            submitted_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add_all([buy_order, sell_order])
        session.flush()
        buy_execution = OrderExecution(
            order_id=buy_order.id,
            execution_no="fixture-buy-exec",
            fill_seq=1,
            filled_quantity=2,
            filled_price=50,
            fee=0,
            tax=0,
            currency="USD",
            trade_fx_rate=1300,
            settlement_date=datetime.now(timezone.utc),
            settlement_fx_rate=None,
            fx_rate_source="fallback",
            executed_at=datetime.now(timezone.utc),
            created_at=utc_now(),
        )
        sell_execution = OrderExecution(
            order_id=sell_order.id,
            execution_no="fixture-sell-exec",
            fill_seq=1,
            filled_quantity=2,
            filled_price=60,
            fee=0,
            tax=0,
            currency="USD",
            trade_fx_rate=1320,
            settlement_date=datetime.now(timezone.utc),
            settlement_fx_rate=None,
            fx_rate_source="fallback",
            executed_at=datetime.now(timezone.utc),
            created_at=utc_now(),
        )
        session.add_all([buy_execution, sell_execution])
        session.flush()
        buy_trade = Trade(
            order_id=buy_order.id,
            execution_id=buy_execution.id,
            ticker="MSFT",
            market="US",
            strategy="factor_investing",
            side="buy",
            quantity=2,
            price=50,
            amount=100,
            fee=0,
            tax=0,
            net_amount=100,
            currency="USD",
            trade_fx_rate=1300,
            settlement_date=datetime.now(timezone.utc),
            settlement_fx_rate=None,
            fx_rate_source="fallback",
            signal_id=None,
            executed_at=datetime.now(timezone.utc),
            created_at=utc_now(),
        )
        sell_trade = Trade(
            order_id=sell_order.id,
            execution_id=sell_execution.id,
            ticker="MSFT",
            market="US",
            strategy="factor_investing",
            side="sell",
            quantity=2,
            price=60,
            amount=120,
            fee=0,
            tax=0,
            net_amount=120,
            currency="USD",
            trade_fx_rate=1320,
            settlement_date=datetime.now(timezone.utc),
            settlement_fx_rate=None,
            fx_rate_source="fallback",
            signal_id=None,
            executed_at=datetime.now(timezone.utc),
            created_at=utc_now(),
        )
        session.add_all([buy_trade, sell_trade])
        session.flush()
        position = Position(
            ticker="MSFT",
            market="US",
            strategy="factor_investing",
            quantity=0,
            avg_cost=0,
            current_price=60,
            highest_price=60,
            entry_date=buy_trade.executed_at,
            updated_at=buy_trade.executed_at,
        )
        session.add(position)
        session.flush()
        session.add(
            PositionLot(
                position_id=position.id,
                strategy="factor_investing",
                ticker="MSFT",
                market="US",
                open_quantity=2,
                remaining_quantity=0,
                open_price=50,
                open_trade_fx_rate=1300,
                open_settlement_date=buy_trade.settlement_date,
                open_settlement_fx_rate=None,
                opened_at=buy_trade.executed_at,
                source_trade_id=buy_trade.id,
                updated_at=sell_trade.executed_at,
            )
        )
        session.commit()

    calculator = TaxCalculator()
    report = calculator.build_trade_report(datetime.now(timezone.utc).year, market="US")

    assert len(report) == 1
    assert report[0]["source"] == "fifo_reconstructed"
    assert report[0]["buy_fx_rate"] == 1300
    assert report[0]["sell_fx_rate"] == 1320
    assert report[0]["realized_gain_loss_krw"] == 28400


def test_tax_calculator_reconstructs_fifo_for_kr_and_keeps_fx_null(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)
    processor = FillProcessor(writer_queue)

    try:
        buy_signal = Signal(ticker="005930", market="KR", action="buy", strategy="trend_following", strength=1.0, reason="entry")
        buy_signal_id = manager.persist_signal(buy_signal)
        buy_intent = manager.create_order_intent(buy_signal, signal_id=buy_signal_id, quantity=5, price=70000)
        buy_submission = manager.persist_validated_order(buy_intent)
        manager.mark_submission_result(buy_submission.order_id, broker_order_no="KR-BUY", accepted=True)
        processor.process_fill(
            ExecutionFill(
                order_id=buy_submission.order_id,
                execution_no="KR-BUY-FILL",
                fill_seq=1,
                filled_quantity=5,
                filled_price=70000,
                fee=500,
                tax=0,
                executed_at=datetime.now(timezone.utc),
            )
        )

        sell_signal = Signal(ticker="005930", market="KR", action="sell", strategy="trend_following", strength=1.0, reason="exit")
        sell_signal_id = manager.persist_signal(sell_signal)
        sell_intent = manager.create_order_intent(sell_signal, signal_id=sell_signal_id, quantity=5, price=73000)
        sell_submission = manager.persist_validated_order(sell_intent)
        manager.mark_submission_result(sell_submission.order_id, broker_order_no="KR-SELL", accepted=True)
        processor.process_fill(
            ExecutionFill(
                order_id=sell_submission.order_id,
                execution_no="KR-SELL-FILL",
                fill_seq=1,
                filled_quantity=5,
                filled_price=73000,
                fee=500,
                tax=100,
                executed_at=datetime.now(timezone.utc),
            )
        )
    finally:
        writer_queue.stop()

    with get_read_session() as session:
        assert session.scalar(select(TaxEvent).where(TaxEvent.ticker == "005930")) is None
        assert session.scalar(select(Trade).where(Trade.ticker == "005930", Trade.side == "sell")) is not None
        assert session.scalar(select(OrderExecution).where(OrderExecution.execution_no == "KR-SELL-FILL")) is not None

    calculator = TaxCalculator()
    report = calculator.build_trade_report(datetime.now(timezone.utc).year, market="KR")
    summary = calculator.calculate_yearly_summary(datetime.now(timezone.utc).year, market="KR")

    assert len(report) == 1
    assert report[0]["source"] == "fifo_reconstructed"
    assert report[0]["buy_fx_rate"] is None
    assert report[0]["sell_fx_rate"] is None
    assert report[0]["realized_gain_loss_krw"] == 14400
    assert summary["taxable_gain_krw"] == 15000


def test_tax_calculator_fifo_fallback_prefers_position_lot_and_sell_settlement_fx(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    session_factory = get_session_factory()
    buy_time = datetime(2026, 1, 2, tzinfo=timezone.utc)
    sell_time = datetime(2026, 2, 2, tzinfo=timezone.utc)

    with session_factory() as session:
        signal_row = SignalRow(
            ticker="NVDA",
            market="US",
            strategy="dual_momentum",
            action="buy",
            strength=1.0,
            reason="fixture",
            status="ordered",
            generated_at=buy_time,
            processed_at=buy_time,
        )
        session.add(signal_row)
        session.flush()
        buy_order = Order(
            client_order_id="nvda-buy",
            kis_order_no="NB1",
            signal_id=signal_row.id,
            ticker="NVDA",
            market="US",
            strategy="dual_momentum",
            side="buy",
            order_type="limit",
            quantity=2,
            price=50,
            status="filled",
            submitted_at=buy_time,
            updated_at=buy_time,
        )
        sell_order = Order(
            client_order_id="nvda-sell",
            kis_order_no="NS1",
            signal_id=signal_row.id,
            ticker="NVDA",
            market="US",
            strategy="dual_momentum",
            side="sell",
            order_type="limit",
            quantity=2,
            price=60,
            status="filled",
            submitted_at=sell_time,
            updated_at=sell_time,
        )
        session.add_all([buy_order, sell_order])
        session.flush()
        buy_execution = OrderExecution(
            order_id=buy_order.id,
            execution_no="nvda-buy-exec",
            fill_seq=1,
            filled_quantity=2,
            filled_price=50,
            fee=0,
            tax=0,
            currency="USD",
            trade_fx_rate=1300,
            settlement_date=buy_time,
            settlement_fx_rate=1310,
            fx_rate_source="fixture",
            executed_at=buy_time,
            created_at=utc_now(),
        )
        sell_execution = OrderExecution(
            order_id=sell_order.id,
            execution_no="nvda-sell-exec",
            fill_seq=1,
            filled_quantity=2,
            filled_price=60,
            fee=0,
            tax=0,
            currency="USD",
            trade_fx_rate=1320,
            settlement_date=sell_time,
            settlement_fx_rate=1330,
            fx_rate_source="fixture",
            executed_at=sell_time,
            created_at=utc_now(),
        )
        session.add_all([buy_execution, sell_execution])
        session.flush()
        buy_trade = Trade(
            order_id=buy_order.id,
            execution_id=buy_execution.id,
            ticker="NVDA",
            market="US",
            strategy="dual_momentum",
            side="buy",
            quantity=2,
            price=50,
            amount=100,
            fee=0,
            tax=0,
            net_amount=100,
            currency="USD",
            trade_fx_rate=1300,
            settlement_date=buy_time,
            settlement_fx_rate=1310,
            fx_rate_source="fixture",
            signal_id=None,
            executed_at=buy_time,
            created_at=utc_now(),
        )
        sell_trade = Trade(
            order_id=sell_order.id,
            execution_id=sell_execution.id,
            ticker="NVDA",
            market="US",
            strategy="dual_momentum",
            side="sell",
            quantity=2,
            price=60,
            amount=120,
            fee=0,
            tax=0,
            net_amount=120,
            currency="USD",
            trade_fx_rate=1320,
            settlement_date=sell_time,
            settlement_fx_rate=1330,
            fx_rate_source="fixture",
            signal_id=None,
            executed_at=sell_time,
            created_at=utc_now(),
        )
        session.add_all([buy_trade, sell_trade])
        session.flush()
        position = Position(
            ticker="NVDA",
            market="US",
            strategy="dual_momentum",
            quantity=0,
            avg_cost=0,
            current_price=60,
            highest_price=60,
            entry_date=buy_time,
            updated_at=sell_time,
        )
        session.add(position)
        session.flush()
        session.add(
            PositionLot(
                position_id=position.id,
                strategy="dual_momentum",
                ticker="NVDA",
                market="US",
                open_quantity=2,
                remaining_quantity=0,
                open_price=50,
                open_trade_fx_rate=1300,
                open_settlement_date=buy_time,
                open_settlement_fx_rate=1310,
                opened_at=buy_time,
                source_trade_id=buy_trade.id,
                updated_at=sell_time,
            )
        )
        session.commit()

    calculator = TaxCalculator()
    report = calculator.build_trade_report(2026, market="US")

    assert len(report) == 1
    assert report[0]["source"] == "fifo_reconstructed"
    assert report[0]["buy_fx_rate"] == 1310
    assert report[0]["sell_fx_rate"] == 1330
    assert report[0]["realized_gain_loss_krw"] == 28600

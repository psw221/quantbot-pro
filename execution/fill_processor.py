from __future__ import annotations

from collections import deque

from sqlalchemy import func, select

from core.models import ExecutionFill, OrderStatus, SignalStatus
from data.database import Order, OrderExecution, Position, PositionLot, Signal, TaxEvent, Trade, utc_now
from execution.writer_queue import WriterQueue


class FillProcessor:
    def __init__(self, writer_queue: WriterQueue) -> None:
        self.writer_queue = writer_queue

    def process_fill(self, fill: ExecutionFill) -> None:
        future = self.writer_queue.submit(
            lambda session: self._process_fill(session, fill),
            description=f"process fill:{fill.execution_no}",
        )
        future.result()

    def _process_fill(self, session, fill: ExecutionFill) -> None:
        if session.scalar(select(OrderExecution).where(OrderExecution.execution_no == fill.execution_no)) is not None:
            return

        order = session.get(Order, fill.order_id)
        if order is None:
            raise ValueError(f"unknown order_id: {fill.order_id}")

        execution_row = OrderExecution(
            order_id=fill.order_id,
            execution_no=fill.execution_no,
            fill_seq=fill.fill_seq,
            filled_quantity=fill.filled_quantity,
            filled_price=fill.filled_price,
            fee=fill.fee,
            tax=fill.tax,
            currency=fill.currency,
            trade_fx_rate=fill.trade_fx_rate,
            settlement_date=fill.settlement_date,
            settlement_fx_rate=fill.settlement_fx_rate,
            fx_rate_source=fill.fx_rate_source,
            executed_at=fill.executed_at,
            created_at=utc_now(),
        )
        session.add(execution_row)
        session.flush()

        trade_amount = fill.filled_quantity * fill.filled_price
        net_amount = trade_amount + fill.fee + fill.tax if order.side == "buy" else trade_amount - fill.fee - fill.tax
        trade_row = Trade(
            order_id=order.id,
            execution_id=execution_row.id,
            ticker=order.ticker,
            market=order.market,
            strategy=order.strategy,
            side=order.side,
            quantity=fill.filled_quantity,
            price=fill.filled_price,
            amount=trade_amount,
            fee=fill.fee,
            tax=fill.tax,
            net_amount=net_amount,
            currency=fill.currency,
            trade_fx_rate=fill.trade_fx_rate,
            settlement_date=fill.settlement_date,
            settlement_fx_rate=fill.settlement_fx_rate,
            fx_rate_source=fill.fx_rate_source,
            signal_id=order.signal_id,
            executed_at=fill.executed_at,
            created_at=utc_now(),
        )
        session.add(trade_row)
        session.flush()

        if order.side == "buy":
            self._apply_buy(session, order, trade_row, fill)
        else:
            self._apply_sell(session, order, trade_row, fill)

        cumulative_filled = session.scalar(
            select(func.coalesce(func.sum(OrderExecution.filled_quantity), 0)).where(OrderExecution.order_id == order.id)
        ) or 0
        order.status = (
            OrderStatus.FILLED.value if cumulative_filled >= order.quantity else OrderStatus.PARTIALLY_FILLED.value
        )
        order.updated_at = utc_now()
        signal_row = session.get(Signal, order.signal_id)
        if signal_row is not None and order.status == OrderStatus.FILLED.value:
            signal_row.status = SignalStatus.ORDERED.value
            signal_row.processed_at = utc_now()

    def _apply_buy(self, session, order: Order, trade_row: Trade, fill: ExecutionFill) -> None:
        position = session.scalar(
            select(Position).where(
                Position.ticker == order.ticker,
                Position.market == order.market,
                Position.strategy == order.strategy,
            )
        )
        if position is None:
            position = Position(
                ticker=order.ticker,
                market=order.market,
                strategy=order.strategy,
                quantity=0,
                avg_cost=0,
                current_price=fill.filled_price,
                highest_price=fill.filled_price,
                entry_date=fill.executed_at,
                updated_at=utc_now(),
            )
            session.add(position)
            session.flush()

        total_cost = (position.avg_cost * position.quantity) + (fill.filled_price * fill.filled_quantity)
        new_quantity = position.quantity + fill.filled_quantity
        position.quantity = new_quantity
        position.avg_cost = total_cost / new_quantity
        position.current_price = fill.filled_price
        position.highest_price = max(position.highest_price, fill.filled_price)
        position.updated_at = utc_now()

        session.add(
            PositionLot(
                position_id=position.id,
                strategy=order.strategy,
                ticker=order.ticker,
                market=order.market,
                open_quantity=fill.filled_quantity,
                remaining_quantity=fill.filled_quantity,
                open_price=fill.filled_price,
                open_trade_fx_rate=fill.trade_fx_rate,
                open_settlement_date=fill.settlement_date,
                open_settlement_fx_rate=fill.settlement_fx_rate,
                opened_at=fill.executed_at,
                source_trade_id=trade_row.id,
                updated_at=utc_now(),
            )
        )

    def _apply_sell(self, session, order: Order, trade_row: Trade, fill: ExecutionFill) -> None:
        position = session.scalar(
            select(Position).where(
                Position.ticker == order.ticker,
                Position.market == order.market,
                Position.strategy == order.strategy,
            )
        )
        if position is None:
            raise ValueError("sell fill received without position")

        lots = deque(
            session.scalars(
                select(PositionLot)
                .where(
                    PositionLot.ticker == order.ticker,
                    PositionLot.market == order.market,
                    PositionLot.strategy == order.strategy,
                    PositionLot.remaining_quantity > 0,
                )
                .order_by(PositionLot.opened_at)
            )
        )
        remaining = fill.filled_quantity
        realized_cost_basis = 0.0
        tax_source_lot = None

        while remaining > 0 and lots:
            lot = lots.popleft()
            consume = min(lot.remaining_quantity, remaining)
            realized_cost_basis += consume * lot.open_price
            lot.remaining_quantity -= consume
            lot.updated_at = utc_now()
            remaining -= consume
            if tax_source_lot is None:
                tax_source_lot = lot

        if remaining > 0:
            raise ValueError("insufficient lots for sell fill")

        position.quantity -= fill.filled_quantity
        position.current_price = fill.filled_price
        position.updated_at = utc_now()
        if position.quantity <= 0:
            position.quantity = 0
            position.avg_cost = 0

        if order.market == "US":
            session.add(
                TaxEvent(
                    trade_id=trade_row.id,
                    ticker=order.ticker,
                    market=order.market,
                    sell_date=fill.executed_at,
                    quantity=fill.filled_quantity,
                    sell_price=fill.filled_price,
                    cost_basis=realized_cost_basis,
                    gain_loss_usd=(trade_row.amount - realized_cost_basis) if fill.trade_fx_rate else None,
                    gain_loss_krw=(trade_row.amount - realized_cost_basis),
                    buy_trade_fx_rate=None if tax_source_lot is None else tax_source_lot.open_trade_fx_rate,
                    buy_settlement_date=None if tax_source_lot is None else tax_source_lot.open_settlement_date,
                    buy_settlement_fx_rate=None if tax_source_lot is None else tax_source_lot.open_settlement_fx_rate,
                    sell_trade_fx_rate=fill.trade_fx_rate,
                    sell_settlement_date=fill.settlement_date,
                    sell_settlement_fx_rate=fill.settlement_fx_rate,
                    fx_rate_source=fill.fx_rate_source,
                    taxable_gain=max(trade_row.amount - realized_cost_basis, 0),
                    tax_year=fill.executed_at.year,
                    is_included_in_report=False,
                )
            )

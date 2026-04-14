from __future__ import annotations

import json

from sqlalchemy import select

from core.models import (
    BrokerOrderSnapshot,
    BrokerPollingSnapshot,
    BrokerPositionSnapshot,
    ExecutionFill,
    OrderStatus,
    ReconciliationMismatch,
    ReconciliationMismatchType,
    ReconciliationResult,
    ReconciliationStatus,
)
from core.settings import Settings, get_settings
from data.database import BrokerPosition, Order, Position, ReconciliationRun, utc_now
from execution.writer_queue import WriterQueue


class ReconciliationService:
    def __init__(self, writer_queue: WriterQueue, settings: Settings | None = None) -> None:
        self.writer_queue = writer_queue
        self.settings = settings or get_settings()

    def reconcile(
        self,
        *,
        broker_positions: list[BrokerPositionSnapshot],
        open_orders: list[BrokerOrderSnapshot | dict],
        cash_available: float,
        missing_fills: list[ExecutionFill] | None = None,
    ) -> ReconciliationResult:
        future = self.writer_queue.submit(
            lambda session: self._reconcile(session, broker_positions, open_orders, cash_available, missing_fills or []),
            description="reconcile broker state",
        )
        return future.result()

    def reconcile_snapshot(
        self,
        snapshot: BrokerPollingSnapshot,
        *,
        missing_fills: list[ExecutionFill] | None = None,
    ) -> ReconciliationResult:
        return self.reconcile(
            broker_positions=snapshot.positions,
            open_orders=snapshot.open_orders,
            cash_available=snapshot.cash_available,
            missing_fills=missing_fills,
        )

    def _reconcile(self, session, broker_positions, open_orders, cash_available, missing_fills) -> ReconciliationResult:
        mismatches: list[ReconciliationMismatch] = []

        internal_positions = {
            (row.ticker, row.market): row
            for row in session.scalars(select(Position))
        }
        broker_position_map = {(row.ticker, row.market): row for row in broker_positions}

        for key, broker_row in broker_position_map.items():
            internal = internal_positions.get(key)
            if internal is None or internal.quantity != broker_row.quantity:
                mismatches.append(
                    ReconciliationMismatch(
                        mismatch_type=ReconciliationMismatchType.QUANTITY_DIFF,
                        ticker=broker_row.ticker,
                        detail=f"broker={broker_row.quantity}, internal={0 if internal is None else internal.quantity}",
                    )
                )

        internal_open_orders = list(
            session.scalars(
                select(Order).where(Order.status.in_([OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value]))
            )
        )
        broker_order_ids = {self._extract_order_no(item) for item in open_orders if self._extract_order_no(item)}
        for order in internal_open_orders:
            if order.kis_order_no and order.kis_order_no not in broker_order_ids and not missing_fills:
                mismatches.append(
                    ReconciliationMismatch(
                        mismatch_type=ReconciliationMismatchType.ORDER_STATUS_DIFF,
                        ticker=order.ticker,
                        detail=f"missing broker order {order.kis_order_no}",
                    )
                )

        for fill in missing_fills:
            mismatches.append(
                ReconciliationMismatch(
                    mismatch_type=ReconciliationMismatchType.MISSING_FILL,
                    ticker=None,
                    detail=fill.execution_no,
                )
            )

        for snapshot in broker_positions:
            session.add(
                BrokerPosition(
                    ticker=snapshot.ticker,
                    market=snapshot.market,
                    quantity=snapshot.quantity,
                    avg_cost=snapshot.avg_cost,
                    currency=snapshot.currency,
                    snapshot_at=snapshot.snapshot_at,
                    source_env=snapshot.source_env,
                )
            )

        status = ReconciliationStatus.RECONCILED if not mismatches else ReconciliationStatus.MISMATCH_DETECTED
        session.add(
            ReconciliationRun(
                run_type="scheduled_poll",
                source_env=self.settings.env.value,
                started_at=utc_now(),
                completed_at=utc_now(),
                mismatch_count=len(mismatches),
                status="ok" if not mismatches else "warning",
                summary_json=json.dumps(
                    {
                        "cash_available": cash_available,
                        "mismatches": [mismatch.detail for mismatch in mismatches],
                    }
                ),
                created_at=utc_now(),
            )
        )

        return ReconciliationResult(
            status=status,
            mismatches=mismatches,
            missing_fills=missing_fills,
            summary={"cash_available": cash_available, "mismatch_count": len(mismatches)},
        )

    @staticmethod
    def _extract_order_no(item: BrokerOrderSnapshot | dict) -> str:
        if isinstance(item, BrokerOrderSnapshot):
            return item.order_no
        return str(item.get("order_no") or item.get("ODNO") or "")

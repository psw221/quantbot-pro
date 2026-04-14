from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import func, select

from core.models import (
    OrderIntent,
    OrderStatus,
    ReconciliationStatus,
    RiskDecision,
    Signal,
    SignalStatus,
)
from core.settings import Settings, get_settings
from data.database import Order, ReconciliationRun, Signal as SignalRow, utc_now
from execution.kis_api import KISApiClient
from execution.writer_queue import WriterQueue


@dataclass(slots=True)
class OrderSubmission:
    order_id: int
    client_order_id: str
    status: OrderStatus
    broker_order_no: str | None = None


class OrderManager:
    def __init__(
        self,
        writer_queue: WriterQueue,
        api_client: KISApiClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.writer_queue = writer_queue
        self.api_client = api_client
        self.reconciliation_status = ReconciliationStatus.IDLE
        self.trading_blocked = False

    def create_order_intent(
        self,
        signal: Signal,
        *,
        signal_id: int,
        quantity: int,
        order_type: str = "market",
        price: float | None = None,
        risk_decision: RiskDecision | None = None,
    ) -> OrderIntent:
        tags = [] if risk_decision is None else list(risk_decision.tags)
        side = "buy" if signal.action == "buy" else "sell"
        return OrderIntent(
            client_order_id=f"{signal.market}-{signal.strategy}-{uuid4().hex[:12]}",
            signal_id=signal_id,
            ticker=signal.ticker,
            market=signal.market,
            strategy=signal.strategy,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
            risk_tags=tags,
            metadata={"reason": signal.reason},
        )

    def persist_signal(self, signal: Signal) -> int:
        future = self.writer_queue.submit(lambda session: self._insert_signal(session, signal), description="insert signal")
        return future.result()

    def reject_signal(self, signal_id: int, reject_reason: str) -> None:
        future = self.writer_queue.submit(
            lambda session: self._reject_signal(session, signal_id, reject_reason),
            description="reject signal",
        )
        future.result()

    def persist_validated_order(self, intent: OrderIntent) -> OrderSubmission:
        future = self.writer_queue.submit(
            lambda session: self._insert_validated_order(session, intent),
            description="insert validated order",
        )
        return future.result()

    def mark_submission_result(self, order_id: int, *, broker_order_no: str | None, accepted: bool, error_message: str | None = None) -> None:
        future = self.writer_queue.submit(
            lambda session: self._mark_submission_result(session, order_id, broker_order_no, accepted, error_message),
            description="mark submission result",
        )
        future.result()

    def place_order(self, order_id: int, broker_payload: dict, *, access_token: str | None = None) -> None:
        if self.trading_blocked:
            future = self.writer_queue.submit(
                lambda session: self._mark_order_failed(session, order_id, "trading_blocked"),
                description="mark trading blocked",
            )
            future.result()
            return

        if self.api_client is None:
            self.mark_submission_result(
                order_id,
                broker_order_no=f"mock-{order_id}",
                accepted=True,
            )
            return

        response = self.api_client.submit_order(broker_payload, access_token=access_token)
        broker_order_no = response.get("output", {}).get("ODNO") or response.get("order_no")
        self.mark_submission_result(order_id, broker_order_no=broker_order_no, accepted=True)

    def start_scheduled_poll(self) -> None:
        self.reconciliation_status = ReconciliationStatus.SCHEDULED_POLLING

    def flag_reconciliation_hold(self, ticker: str | None, summary: dict | None = None) -> None:
        self.trading_blocked = True
        self.reconciliation_status = ReconciliationStatus.MISMATCH_DETECTED
        future = self.writer_queue.submit(
            lambda session: self._record_reconciliation_hold(session, ticker, summary or {}),
            description="record reconciliation hold",
        )
        future.result()

    @staticmethod
    def _insert_signal(session, signal: Signal) -> int:
        row = SignalRow(
            ticker=signal.ticker,
            market=signal.market,
            strategy=signal.strategy,
            action=signal.action,
            strength=signal.strength,
            reason=signal.reason,
            status=SignalStatus.PENDING.value,
            generated_at=signal.timestamp,
        )
        session.add(row)
        session.flush()
        return row.id

    @staticmethod
    def _reject_signal(session, signal_id: int, reject_reason: str) -> None:
        row = session.get(SignalRow, signal_id)
        if row is None:
            return
        row.status = SignalStatus.REJECTED.value
        row.reject_reason = reject_reason
        row.processed_at = utc_now()

    @staticmethod
    def _insert_validated_order(session, intent: OrderIntent) -> OrderSubmission:
        duplicate = session.scalar(select(func.count()).select_from(Order).where(Order.client_order_id == intent.client_order_id))
        if duplicate:
            raise ValueError(f"duplicate client_order_id: {intent.client_order_id}")

        row = Order(
            client_order_id=intent.client_order_id,
            signal_id=intent.signal_id,
            ticker=intent.ticker,
            market=intent.market,
            strategy=intent.strategy,
            side=intent.side,
            order_type=intent.order_type,
            quantity=intent.quantity,
            price=intent.price,
            status=OrderStatus.VALIDATED.value,
            submitted_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(row)

        signal_row = session.get(SignalRow, intent.signal_id)
        if signal_row is not None:
            signal_row.status = SignalStatus.RESOLVED.value
            signal_row.processed_at = utc_now()

        session.flush()
        return OrderSubmission(order_id=row.id, client_order_id=row.client_order_id, status=OrderStatus.VALIDATED)

    @staticmethod
    def _mark_submission_result(session, order_id: int, broker_order_no: str | None, accepted: bool, error_message: str | None) -> None:
        row = session.get(Order, order_id)
        if row is None:
            return
        row.updated_at = utc_now()
        if accepted:
            row.kis_order_no = broker_order_no
            row.status = OrderStatus.SUBMITTED.value
        else:
            row.status = OrderStatus.FAILED.value
            row.error_message = error_message

    @staticmethod
    def _mark_order_failed(session, order_id: int, reason: str) -> None:
        row = session.get(Order, order_id)
        if row is None:
            return
        row.status = OrderStatus.FAILED.value
        row.error_message = reason
        row.updated_at = utc_now()

    def _record_reconciliation_hold(self, session, ticker: str | None, summary: dict) -> None:
        if ticker is not None:
            stmt = select(Order).where(
                Order.ticker == ticker,
                Order.status.in_([OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value]),
            )
            for row in session.scalars(stmt):
                row.status = OrderStatus.RECONCILE_HOLD.value
                row.updated_at = utc_now()

        session.add(
            ReconciliationRun(
                run_type="scheduled_poll",
                source_env=self.settings.env.value,
                started_at=utc_now(),
                completed_at=utc_now(),
                mismatch_count=summary.get("mismatch_count", 1),
                status="warning",
                summary_json=str(summary),
                created_at=utc_now(),
            )
        )

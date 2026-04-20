from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select

from core.models import BrokerFillSnapshot, ExecutionFill, OrderStatus
from core.settings import Settings, get_settings
from data.database import Order, OrderExecution, get_read_session
from execution.kis_api import KISApiClient


@dataclass(slots=True)
class _InternalFillProgress:
    filled_quantity: int = 0
    filled_amount: float = 0.0
    fill_count: int = 0


class BrokerFillIngestionService:
    def __init__(
        self,
        *,
        api_client: KISApiClient,
        settings: Settings | None = None,
    ) -> None:
        self.api_client = api_client
        self.settings = settings or get_settings()

    def collect_execution_fills(
        self,
        access_token: str,
        *,
        market: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[ExecutionFill]:
        market_code = market.upper()
        if market_code != "KR":
            return []

        with get_read_session() as session:
            candidates = list(
                session.query(Order)
                .filter(
                    Order.market == market_code,
                    Order.status.in_([OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value]),
                )
                .order_by(Order.id)
                .all()
            )
            if not candidates:
                return []
            progress = self._load_internal_progress(session, [order.id for order in candidates])

        payload = self.api_client.list_daily_order_fills(
            access_token,
            market=market_code,
            start_date=start_date,
            end_date=end_date,
            side_code="00",
        )
        snapshots = self.api_client.normalize_daily_order_fills(payload, default_market=market_code)
        latest_by_order = self._latest_snapshots_by_order_no(snapshots)

        fills: list[ExecutionFill] = []
        for order in candidates:
            if not order.kis_order_no:
                continue
            snapshot = latest_by_order.get(order.kis_order_no)
            if snapshot is None:
                continue
            if snapshot.ticker and snapshot.ticker != order.ticker:
                continue
            if snapshot.side != order.side:
                continue

            internal = progress.get(order.id, _InternalFillProgress())
            delta_quantity = snapshot.cumulative_filled_quantity - internal.filled_quantity
            if delta_quantity <= 0:
                continue
            if snapshot.average_filled_price is None:
                continue

            fill_seq = internal.fill_count + 1
            delta_price = self._derive_delta_price(snapshot, internal, delta_quantity)
            fills.append(
                ExecutionFill(
                    order_id=order.id,
                    execution_no=self._build_execution_no(snapshot, fill_seq=fill_seq),
                    fill_seq=fill_seq,
                    filled_quantity=delta_quantity,
                    filled_price=delta_price,
                    fee=0.0,
                    tax=0.0,
                    executed_at=snapshot.occurred_at,
                    currency="KRW",
                    trade_fx_rate=None,
                    settlement_date=None,
                    settlement_fx_rate=None,
                    fx_rate_source=None,
                )
            )

        return fills

    @staticmethod
    def _load_internal_progress(session, order_ids: list[int]) -> dict[int, _InternalFillProgress]:
        progress = {order_id: _InternalFillProgress() for order_id in order_ids}
        if not order_ids:
            return progress

        stmt = (
            select(
                OrderExecution.order_id,
                func.coalesce(func.sum(OrderExecution.filled_quantity), 0),
                func.coalesce(func.sum(OrderExecution.filled_quantity * OrderExecution.filled_price), 0.0),
                func.count(OrderExecution.id),
            )
            .where(OrderExecution.order_id.in_(order_ids))
            .group_by(OrderExecution.order_id)
        )
        for order_id, filled_quantity, filled_amount, fill_count in session.execute(stmt):
            progress[int(order_id)] = _InternalFillProgress(
                filled_quantity=int(filled_quantity or 0),
                filled_amount=float(filled_amount or 0.0),
                fill_count=int(fill_count or 0),
            )
        return progress

    @staticmethod
    def _latest_snapshots_by_order_no(snapshots: list[BrokerFillSnapshot]) -> dict[str, BrokerFillSnapshot]:
        latest: dict[str, BrokerFillSnapshot] = {}
        for snapshot in snapshots:
            current = latest.get(snapshot.order_no)
            if current is None:
                latest[snapshot.order_no] = snapshot
                continue
            current_key = (current.cumulative_filled_quantity, current.occurred_at)
            candidate_key = (snapshot.cumulative_filled_quantity, snapshot.occurred_at)
            if candidate_key >= current_key:
                latest[snapshot.order_no] = snapshot
        return latest

    @staticmethod
    def _derive_delta_price(
        snapshot: BrokerFillSnapshot,
        internal: _InternalFillProgress,
        delta_quantity: int,
    ) -> float:
        assert snapshot.average_filled_price is not None
        cumulative_amount = snapshot.average_filled_price * snapshot.cumulative_filled_quantity
        delta_amount = cumulative_amount - internal.filled_amount
        if delta_quantity > 0 and delta_amount > 0:
            return delta_amount / delta_quantity
        return snapshot.average_filled_price

    @staticmethod
    def _build_execution_no(snapshot: BrokerFillSnapshot, *, fill_seq: int) -> str:
        if snapshot.execution_hint:
            return str(snapshot.execution_hint)
        occurred = snapshot.occurred_at.astimezone(UTC).strftime("%Y%m%d%H%M%S")
        return f"{snapshot.market}-SYNC-{snapshot.order_no}-{fill_seq}-{snapshot.cumulative_filled_quantity}-{occurred}"

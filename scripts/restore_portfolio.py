from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.models import BrokerOrderSnapshot, BrokerPollingSnapshot, BrokerPositionSnapshot, OrderStatus, ReconciliationStatus
from core.settings import Settings, get_settings
from data.database import Order, Position, get_read_session, init_db
from execution.order_manager import OrderManager
from execution.reconciliation import ReconciliationService
from execution.writer_queue import WriterQueue
from monitor.operations import OperationsRecorder, PortfolioSnapshotPayload


OPEN_ORDER_STATUSES = {
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
    OrderStatus.CANCEL_PENDING.value,
    OrderStatus.RECONCILE_HOLD.value,
}


@dataclass(slots=True)
class RestorePortfolioSummary:
    mode: str
    market: str
    trading_blocked_confirmed: bool
    broker_position_count: int
    broker_open_order_count: int
    position_mismatches: list[dict[str, Any]]
    order_mismatches: list[dict[str, Any]]
    cash_available: float
    mismatch_count: int
    reconciliation_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RestorePortfolioService:
    def __init__(
        self,
        *,
        writer_queue: WriterQueue,
        reconciliation_service: ReconciliationService,
        order_manager: OrderManager | None = None,
        operations_recorder: OperationsRecorder | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.writer_queue = writer_queue
        self.reconciliation_service = reconciliation_service
        self.order_manager = order_manager
        self.operations_recorder = operations_recorder
        self.settings = settings or get_settings()

    def preview(self, snapshot: BrokerPollingSnapshot, *, market: str = "ALL") -> RestorePortfolioSummary:
        filtered = _filter_snapshot(snapshot, market)
        position_mismatches, order_mismatches = self._collect_mismatches(filtered)
        return RestorePortfolioSummary(
            mode="dry-run",
            market=market.upper(),
            trading_blocked_confirmed=bool(self.order_manager and self.order_manager.trading_blocked),
            broker_position_count=len(filtered.positions),
            broker_open_order_count=len(filtered.open_orders),
            position_mismatches=position_mismatches,
            order_mismatches=order_mismatches,
            cash_available=filtered.cash_available,
            mismatch_count=len(position_mismatches) + len(order_mismatches),
        )

    def restore(self, snapshot: BrokerPollingSnapshot, *, market: str = "ALL", apply: bool = False) -> RestorePortfolioSummary:
        summary = self.preview(snapshot, market=market)
        if not apply:
            return summary

        if self.order_manager is not None and not self.order_manager.trading_blocked:
            raise RuntimeError("restore apply mode requires trading_blocked confirmation")

        filtered = _filter_snapshot(snapshot, market)
        started_extra = {
            "market": summary.market,
            "mode": "apply",
            "broker_position_count": summary.broker_position_count,
            "broker_open_order_count": summary.broker_open_order_count,
            "mismatch_count": summary.mismatch_count,
        }
        if self.operations_recorder is not None:
            self.operations_recorder.record_system_log(
                level="WARNING",
                module="scripts.restore_portfolio",
                message="restore apply started",
                extra=started_extra,
            )

        try:
            result = self.reconciliation_service.reconcile_snapshot(filtered)
            if result.status == ReconciliationStatus.MISMATCH_DETECTED and self.order_manager is not None:
                self.order_manager.flag_reconciliation_hold(None, summary={"mismatch_count": summary.mismatch_count, "market": summary.market})
            self._record_optional_portfolio_snapshot(filtered)
            if self.operations_recorder is not None:
                self.operations_recorder.record_system_log(
                    level="INFO",
                    module="scripts.restore_portfolio",
                    message="restore apply completed",
                    extra={
                        "market": summary.market,
                        "status": result.status.value,
                        "mismatch_count": summary.mismatch_count,
                    },
                )
            summary.mode = "apply"
            summary.reconciliation_status = result.status.value
            return summary
        except Exception as exc:
            if self.operations_recorder is not None:
                self.operations_recorder.record_system_log(
                    level="ERROR",
                    module="scripts.restore_portfolio",
                    message="restore apply failed",
                    extra={
                        "market": summary.market,
                        "error": str(exc),
                    },
                )
            raise

    def _record_optional_portfolio_snapshot(self, snapshot: BrokerPollingSnapshot) -> None:
        if self.operations_recorder is None:
            return
        payload = snapshot.raw_payloads.get("portfolio_snapshot")
        if not isinstance(payload, dict):
            return
        self.operations_recorder.record_portfolio_snapshot(
            PortfolioSnapshotPayload(
                snapshot_date=_parse_datetime(payload["snapshot_date"]),
                total_value_krw=float(payload["total_value_krw"]),
                cash_krw=float(payload["cash_krw"]),
                domestic_value_krw=float(payload["domestic_value_krw"]),
                overseas_value_krw=float(payload["overseas_value_krw"]),
                usd_krw_rate=float(payload["usd_krw_rate"]),
                daily_return=float(payload.get("daily_return", 0.0)),
                cumulative_return=float(payload.get("cumulative_return", 0.0)),
                drawdown=float(payload.get("drawdown", 0.0)),
                max_drawdown=float(payload.get("max_drawdown", 0.0)),
                position_count=int(payload.get("position_count", len(snapshot.positions))),
            )
        )

    @staticmethod
    def _collect_mismatches(snapshot: BrokerPollingSnapshot) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with get_read_session() as session:
            internal_positions = {
                (row.ticker, row.market): row.quantity
                for row in session.query(Position).all()
            }
            internal_orders = {
                row.kis_order_no: row
                for row in session.query(Order).filter(Order.status.in_(sorted(OPEN_ORDER_STATUSES))).all()
                if row.kis_order_no
            }

        broker_positions = {(row.ticker, row.market): row.quantity for row in snapshot.positions}
        broker_order_ids = {row.order_no for row in snapshot.open_orders}

        position_mismatches: list[dict[str, Any]] = []
        for ticker, market in sorted(set(internal_positions) | set(broker_positions)):
            internal_quantity = internal_positions.get((ticker, market), 0)
            broker_quantity = broker_positions.get((ticker, market), 0)
            if internal_quantity != broker_quantity:
                position_mismatches.append(
                    {
                        "ticker": ticker,
                        "market": market,
                        "internal_quantity": internal_quantity,
                        "broker_quantity": broker_quantity,
                    }
                )

        order_mismatches: list[dict[str, Any]] = []
        for broker_order in snapshot.open_orders:
            if broker_order.order_no not in internal_orders:
                order_mismatches.append(
                    {
                        "order_no": broker_order.order_no,
                        "ticker": broker_order.ticker,
                        "reason": "broker_only",
                    }
                )
        for order_no, row in internal_orders.items():
            if order_no not in broker_order_ids:
                order_mismatches.append(
                    {
                        "order_no": order_no,
                        "ticker": row.ticker,
                        "reason": "internal_only",
                    }
                )

        return position_mismatches, order_mismatches


def load_snapshot_file(path: Path) -> BrokerPollingSnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    positions = [
        BrokerPositionSnapshot(
            ticker=str(item["ticker"]),
            market=str(item["market"]).upper(),
            quantity=int(item["quantity"]),
            avg_cost=float(item["avg_cost"]),
            currency=str(item.get("currency", "KRW")).upper(),
            snapshot_at=_parse_datetime(item["snapshot_at"]),
            source_env=str(item.get("source_env", "vts")).lower(),
        )
        for item in payload.get("positions", [])
    ]
    open_orders = [
        BrokerOrderSnapshot(
            order_no=str(item["order_no"]),
            ticker=str(item["ticker"]),
            market=str(item["market"]).upper(),
            side=str(item["side"]).lower(),
            quantity=int(item["quantity"]),
            remaining_quantity=int(item["remaining_quantity"]),
            status=str(item.get("status", "submitted")),
            price=None if item.get("price") is None else float(item["price"]),
        )
        for item in payload.get("open_orders", [])
    ]
    raw_payloads = payload.get("raw_payloads", {})
    portfolio_snapshot = payload.get("portfolio_snapshot")
    if portfolio_snapshot is not None:
        raw_payloads = {**raw_payloads, "portfolio_snapshot": portfolio_snapshot}
    return BrokerPollingSnapshot(
        positions=positions,
        open_orders=open_orders,
        cash_available=float(payload.get("cash_available", 0.0)),
        raw_payloads=raw_payloads,
    )


def _filter_snapshot(snapshot: BrokerPollingSnapshot, market: str) -> BrokerPollingSnapshot:
    market_code = market.upper()
    if market_code == "ALL":
        return snapshot
    if market_code not in {"KR", "US"}:
        raise ValueError("market must be one of ALL, KR, US")
    return BrokerPollingSnapshot(
        positions=[row for row in snapshot.positions if row.market == market_code],
        open_orders=[row for row in snapshot.open_orders if row.market == market_code],
        cash_available=snapshot.cash_available,
        raw_payloads=dict(snapshot.raw_payloads),
    )


def _parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restore portfolio state from a broker snapshot file")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Preview mismatches without writing to the database")
    mode.add_argument("--apply", action="store_true", help="Persist reconciliation results and optional recovery artifacts")
    parser.add_argument("--market", choices=["ALL", "KR", "US"], default="ALL")
    parser.add_argument("--snapshot-file", required=True, help="Path to a JSON file containing broker positions/open orders/cash")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    init_db(settings)

    writer_queue = WriterQueue.from_settings(settings)
    writer_queue.start()
    try:
        snapshot = load_snapshot_file(Path(args.snapshot_file))
        order_manager = OrderManager(writer_queue=writer_queue, settings=settings)
        if args.apply:
            order_manager.trading_blocked = True
        service = RestorePortfolioService(
            writer_queue=writer_queue,
            reconciliation_service=ReconciliationService(writer_queue=writer_queue, settings=settings),
            order_manager=order_manager,
            operations_recorder=OperationsRecorder(writer_queue),
            settings=settings,
        )
        summary = service.restore(snapshot, market=args.market, apply=args.apply)
        print(json.dumps(summary.to_dict(), default=str, indent=2))
    finally:
        writer_queue.stop()


if __name__ == "__main__":
    main()

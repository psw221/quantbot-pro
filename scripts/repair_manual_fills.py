from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from auth.token_manager import TokenManager
from core.exceptions import BrokerApiError
from core.models import (
    BrokerFillSnapshot,
    BrokerPollingSnapshot,
    ExecutionFill,
    OrderStatus,
    ReconciliationStatus,
    SignalStatus,
)
from core.settings import Settings, get_settings
from data.database import Order, Position, Signal as SignalRow, get_read_session, init_db, utc_now
from execution.fill_processor import FillProcessor
from execution.kis_api import KISApiClient
from execution.reconciliation import ReconciliationService
from execution.writer_queue import WriterQueue
from monitor.operations import OperationsRecorder
from scripts.restore_portfolio import load_snapshot_file


ACTIVE_ORDER_STATUSES = {
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
    OrderStatus.CANCEL_PENDING.value,
    OrderStatus.RECONCILE_HOLD.value,
}
MANUAL_REPAIR_REASON = "manual_broker_sell_repair"
TOKEN_RETRY_MARKERS = (
    "EGW00133",
    "접근토큰 발급 잠시 후 다시 시도하세요",
)
REQUEST_RETRY_MARKERS = (
    "EGW00201",
    "초당 거래건수를 초과하였습니다",
)


@dataclass(slots=True)
class ManualFillCandidate:
    order_no: str
    order_orgno: str | None
    quantity: int
    average_filled_price: float
    occurred_at: datetime
    execution_hint: str | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["occurred_at"] = self.occurred_at.isoformat()
        return payload


@dataclass(slots=True)
class ManualFillRepairSummary:
    mode: str
    market: str
    ticker: str
    strategy: str
    runtime_stopped_confirmed: bool
    broker_quantity: int
    internal_quantity_before: int
    internal_quantity_after: int | None
    open_order_count: int
    missing_sell_quantity: int
    candidate_fill_count: int
    candidate_sell_quantity: int
    candidate_fills: list[dict[str, Any]] = field(default_factory=list)
    reconciliation_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ManualFillRepairService:
    def __init__(
        self,
        *,
        writer_queue: WriterQueue,
        reconciliation_service: ReconciliationService,
        fill_processor: FillProcessor | None = None,
        operations_recorder: OperationsRecorder | None = None,
        runtime_pid_path: Path | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.writer_queue = writer_queue
        self.reconciliation_service = reconciliation_service
        self.fill_processor = fill_processor or FillProcessor(writer_queue)
        self.operations_recorder = operations_recorder
        self.runtime_pid_path = runtime_pid_path or Path("data/auto_trading.pid.json")
        self.settings = settings or get_settings()

    def preview(
        self,
        snapshot: BrokerPollingSnapshot,
        *,
        market: str,
        ticker: str,
        strategy: str,
        fill_snapshots: list[BrokerFillSnapshot],
        order_no: str | None = None,
    ) -> ManualFillRepairSummary:
        plan = self._build_plan(
            snapshot,
            market=market,
            ticker=ticker,
            strategy=strategy,
            fill_snapshots=fill_snapshots,
            order_no=order_no,
        )
        return plan["summary"]

    def repair(
        self,
        snapshot: BrokerPollingSnapshot,
        *,
        market: str,
        ticker: str,
        strategy: str,
        fill_snapshots: list[BrokerFillSnapshot],
        order_no: str | None = None,
        apply: bool = False,
    ) -> ManualFillRepairSummary:
        plan = self._build_plan(
            snapshot,
            market=market,
            ticker=ticker,
            strategy=strategy,
            fill_snapshots=fill_snapshots,
            order_no=order_no,
        )
        summary: ManualFillRepairSummary = plan["summary"]
        if not apply:
            return summary

        started_extra = {
            "market": summary.market,
            "ticker": summary.ticker,
            "strategy": summary.strategy,
            "missing_sell_quantity": summary.missing_sell_quantity,
            "candidate_fill_count": summary.candidate_fill_count,
            "mode": "apply",
        }
        if self.operations_recorder is not None:
            self.operations_recorder.record_system_log(
                level="WARNING",
                module="scripts.repair_manual_fills",
                message="manual fill repair started",
                extra=started_extra,
            )

        try:
            future = self.writer_queue.submit(
                lambda session: self._apply_manual_fill_repairs(
                    session,
                    market=summary.market,
                    ticker=summary.ticker,
                    strategy=summary.strategy,
                    candidates=plan["candidates"],
                ),
                description=f"manual fill repair:{summary.market}:{summary.ticker}:{summary.strategy}",
            )
            internal_quantity_after = future.result()
            summary.mode = "apply"
            summary.internal_quantity_after = internal_quantity_after

            if internal_quantity_after != summary.broker_quantity:
                raise RuntimeError(
                    "manual fill repair completed but internal quantity still differs from broker quantity"
                )

            reconciliation_result = self.reconciliation_service.reconcile_snapshot(
                snapshot,
                run_type="manual_restore",
            )
            summary.reconciliation_status = reconciliation_result.status.value
            if reconciliation_result.status != ReconciliationStatus.RECONCILED:
                raise RuntimeError(
                    f"post-repair reconciliation did not clear mismatch: {reconciliation_result.status.value}"
                )

            if self.operations_recorder is not None:
                self.operations_recorder.record_system_log(
                    level="INFO",
                    module="scripts.repair_manual_fills",
                    message="manual fill repair completed",
                    extra={
                        "market": summary.market,
                        "ticker": summary.ticker,
                        "strategy": summary.strategy,
                        "reconciliation_status": summary.reconciliation_status,
                        "internal_quantity_after": summary.internal_quantity_after,
                    },
                )
            return summary
        except Exception as exc:
            if self.operations_recorder is not None:
                self.operations_recorder.record_system_log(
                    level="ERROR",
                    module="scripts.repair_manual_fills",
                    message="manual fill repair failed",
                    extra={
                        "market": summary.market,
                        "ticker": summary.ticker,
                        "strategy": summary.strategy,
                        "error": str(exc),
                    },
                )
            raise

    def _build_plan(
        self,
        snapshot: BrokerPollingSnapshot,
        *,
        market: str,
        ticker: str,
        strategy: str,
        fill_snapshots: list[BrokerFillSnapshot],
        order_no: str | None,
    ) -> dict[str, Any]:
        market_code = market.upper()
        normalized_ticker = ticker.strip().upper()
        normalized_strategy = strategy.strip()
        runtime_stopped_confirmed = not self.runtime_pid_path.exists()
        if not runtime_stopped_confirmed:
            raise RuntimeError("manual fill repair requires the auto-trading runtime to be stopped first")

        broker_quantity = _broker_quantity(snapshot, ticker=normalized_ticker, market=market_code)
        internal_state = self._load_internal_state(
            ticker=normalized_ticker,
            market=market_code,
            strategy=normalized_strategy,
        )
        if internal_state["internal_quantity"] <= broker_quantity:
            raise RuntimeError("manual fill repair only supports internal quantity greater than broker quantity")
        if internal_state["open_order_count"] > 0:
            raise RuntimeError("manual fill repair requires zero active internal orders for the target position")

        missing_sell_quantity = internal_state["internal_quantity"] - broker_quantity
        candidates = self._select_candidates(
            ticker=normalized_ticker,
            market=market_code,
            strategy=normalized_strategy,
            fill_snapshots=fill_snapshots,
            order_no=order_no,
        )
        candidate_sell_quantity = sum(candidate.quantity for candidate in candidates)
        if not candidates:
            raise RuntimeError("manual fill repair found no broker sell fills to replay")
        if candidate_sell_quantity != missing_sell_quantity:
            raise RuntimeError(
                "manual fill repair requires broker sell fill quantity to exactly match the internal-broker quantity gap"
            )

        summary = ManualFillRepairSummary(
            mode="dry-run",
            market=market_code,
            ticker=normalized_ticker,
            strategy=normalized_strategy,
            runtime_stopped_confirmed=runtime_stopped_confirmed,
            broker_quantity=broker_quantity,
            internal_quantity_before=internal_state["internal_quantity"],
            internal_quantity_after=None,
            open_order_count=internal_state["open_order_count"],
            missing_sell_quantity=missing_sell_quantity,
            candidate_fill_count=len(candidates),
            candidate_sell_quantity=candidate_sell_quantity,
            candidate_fills=[candidate.to_dict() for candidate in candidates],
        )
        return {
            "summary": summary,
            "candidates": candidates,
        }

    def _load_internal_state(
        self,
        *,
        ticker: str,
        market: str,
        strategy: str,
    ) -> dict[str, int]:
        with get_read_session() as session:
            position = session.scalar(
                select(Position).where(
                    Position.ticker == ticker,
                    Position.market == market,
                    Position.strategy == strategy,
                )
            )
            open_order_count = int(
                session.query(Order)
                .filter(
                    Order.ticker == ticker,
                    Order.market == market,
                    Order.strategy == strategy,
                    Order.status.in_(sorted(ACTIVE_ORDER_STATUSES)),
                )
                .count()
            )
        internal_quantity = 0 if position is None else int(position.quantity)
        return {
            "internal_quantity": internal_quantity,
            "open_order_count": open_order_count,
        }

    def _select_candidates(
        self,
        *,
        ticker: str,
        market: str,
        strategy: str,
        fill_snapshots: list[BrokerFillSnapshot],
        order_no: str | None,
    ) -> list[ManualFillCandidate]:
        latest_by_order: dict[tuple[str | None, str], BrokerFillSnapshot] = {}
        for snapshot in fill_snapshots:
            if snapshot.market != market:
                continue
            if snapshot.ticker != ticker:
                continue
            if snapshot.side != "sell":
                continue
            if order_no is not None and snapshot.order_no != order_no:
                continue
            if snapshot.cumulative_filled_quantity <= 0 or snapshot.average_filled_price is None:
                continue
            key = (snapshot.order_orgno, snapshot.order_no)
            current = latest_by_order.get(key)
            if current is None:
                latest_by_order[key] = snapshot
                continue
            current_key = (current.cumulative_filled_quantity, current.occurred_at)
            candidate_key = (snapshot.cumulative_filled_quantity, snapshot.occurred_at)
            if candidate_key >= current_key:
                latest_by_order[key] = snapshot

        mirrored_order_keys = self._load_mirrored_order_keys(
            ticker=ticker,
            market=market,
            strategy=strategy,
        )
        candidates: list[ManualFillCandidate] = []
        for key, snapshot in latest_by_order.items():
            if key in mirrored_order_keys:
                continue
            candidates.append(
                ManualFillCandidate(
                    order_no=snapshot.order_no,
                    order_orgno=snapshot.order_orgno,
                    quantity=snapshot.cumulative_filled_quantity,
                    average_filled_price=float(snapshot.average_filled_price),
                    occurred_at=snapshot.occurred_at,
                    execution_hint=snapshot.execution_hint,
                )
            )
        candidates.sort(key=lambda candidate: (candidate.occurred_at, candidate.order_no))
        return candidates

    def _load_mirrored_order_keys(
        self,
        *,
        ticker: str,
        market: str,
        strategy: str,
    ) -> set[tuple[str | None, str]]:
        with get_read_session() as session:
            rows = session.execute(
                select(Order.kis_order_orgno, Order.kis_order_no).where(
                    Order.ticker == ticker,
                    Order.market == market,
                    Order.strategy == strategy,
                    Order.side == "sell",
                    Order.kis_order_no.is_not(None),
                )
            ).all()
        return {(row[0], row[1]) for row in rows if row[1]}

    def _apply_manual_fill_repairs(
        self,
        session,
        *,
        market: str,
        ticker: str,
        strategy: str,
        candidates: list[ManualFillCandidate],
    ) -> int:
        for candidate in candidates:
            order = self._load_or_create_manual_order(
                session,
                market=market,
                ticker=ticker,
                strategy=strategy,
                candidate=candidate,
            )
            fill = ExecutionFill(
                order_id=order.id,
                execution_no=_build_execution_no(candidate, market=market),
                fill_seq=1,
                filled_quantity=candidate.quantity,
                filled_price=candidate.average_filled_price,
                fee=0.0,
                tax=0.0,
                executed_at=candidate.occurred_at,
                currency="KRW" if market == "KR" else "USD",
                trade_fx_rate=None,
                settlement_date=None,
                settlement_fx_rate=None,
                fx_rate_source=None,
            )
            self.fill_processor._process_fill(session, fill)

        position = session.scalar(
            select(Position).where(
                Position.ticker == ticker,
                Position.market == market,
                Position.strategy == strategy,
            )
        )
        return 0 if position is None else int(position.quantity)

    @staticmethod
    def _load_or_create_manual_order(
        session,
        *,
        market: str,
        ticker: str,
        strategy: str,
        candidate: ManualFillCandidate,
    ) -> Order:
        existing = session.scalar(
            select(Order).where(
                Order.ticker == ticker,
                Order.market == market,
                Order.strategy == strategy,
                Order.side == "sell",
                Order.kis_order_no == candidate.order_no,
                Order.kis_order_orgno == candidate.order_orgno,
            )
        )
        if existing is not None:
            return existing

        signal_row = SignalRow(
            ticker=ticker,
            market=market,
            strategy=strategy,
            action="sell",
            strength=1.0,
            reason=MANUAL_REPAIR_REASON,
            status=SignalStatus.RESOLVED.value,
            generated_at=candidate.occurred_at,
        )
        session.add(signal_row)
        session.flush()

        order_row = Order(
            client_order_id=_build_client_order_id(
                ticker=ticker,
                market=market,
                strategy=strategy,
                candidate=candidate,
            ),
            kis_order_no=candidate.order_no,
            kis_order_orgno=candidate.order_orgno,
            signal_id=signal_row.id,
            ticker=ticker,
            market=market,
            strategy=strategy,
            side="sell",
            order_type="market",
            quantity=candidate.quantity,
            price=candidate.average_filled_price,
            status=OrderStatus.SUBMITTED.value,
            submitted_at=candidate.occurred_at,
            updated_at=utc_now(),
        )
        session.add(order_row)
        session.flush()
        return order_row


def _build_client_order_id(
    *,
    ticker: str,
    market: str,
    strategy: str,
    candidate: ManualFillCandidate,
) -> str:
    normalized_orgno = candidate.order_orgno or "na"
    return f"manual-repair-{market.lower()}-{ticker}-{strategy}-{normalized_orgno}-{candidate.order_no}"


def _build_execution_no(candidate: ManualFillCandidate, *, market: str) -> str:
    occurred = candidate.occurred_at.astimezone(UTC).strftime("%Y%m%d%H%M%S")
    if candidate.execution_hint:
        return f"{market}-MANUAL-{candidate.execution_hint}"
    normalized_orgno = candidate.order_orgno or "na"
    return f"{market}-MANUAL-{normalized_orgno}-{candidate.order_no}-1-{candidate.quantity}-{occurred}"


def _broker_quantity(snapshot: BrokerPollingSnapshot, *, ticker: str, market: str) -> int:
    quantity = sum(row.quantity for row in snapshot.positions if row.ticker == ticker and row.market == market)
    return int(quantity)


def collect_broker_fill_snapshots(
    *,
    market: str,
    ticker: str,
    trade_date: str,
    settings: Settings | None = None,
) -> list[BrokerFillSnapshot]:
    runtime_settings = settings or get_settings()
    writer_queue = WriterQueue.from_settings(runtime_settings)
    writer_queue.start()
    try:
        api_client = KISApiClient(settings=runtime_settings)
        token_manager = TokenManager(writer_queue=writer_queue, api_client=api_client, settings=runtime_settings)
        access_token = _call_with_broker_retry(
            lambda: token_manager.get_valid_token(runtime_settings.env),
            retries=3,
            default_sleep_seconds=65.0,
        )
        payload = _call_with_broker_retry(
            lambda: api_client.list_daily_order_fills(
                access_token,
                market=market.upper(),
                start_date=trade_date,
                end_date=trade_date,
                ticker=ticker,
                side_code="00",
            ),
            retries=3,
            default_sleep_seconds=2.0,
        )
        return api_client.normalize_daily_order_fills(payload, default_market=market.upper())
    finally:
        writer_queue.stop()


def _call_with_broker_retry(fn, *, retries: int, default_sleep_seconds: float):
    for attempt in range(retries):
        try:
            return fn()
        except BrokerApiError as exc:
            if attempt >= retries - 1:
                raise
            message = str(exc)
            sleep_seconds = _broker_retry_sleep_seconds(message, default_sleep_seconds)
            if sleep_seconds is None:
                raise
            time.sleep(sleep_seconds)


def _broker_retry_sleep_seconds(message: str, default_sleep_seconds: float) -> float | None:
    normalized = message.lower()
    if any(marker.lower() in normalized for marker in TOKEN_RETRY_MARKERS):
        return 65.0
    if any(marker.lower() in normalized for marker in REQUEST_RETRY_MARKERS):
        return default_sleep_seconds
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay manual broker sell fills into the internal ledger")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Preview repair candidates without writing to the database")
    mode.add_argument("--apply", action="store_true", help="Replay broker sell fills into the internal ledger")
    parser.add_argument("--market", choices=["KR", "US"], required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--snapshot-file", required=True, help="Current broker snapshot JSON file")
    parser.add_argument("--trade-date", help="Broker fill trade date in YYYYMMDD format; defaults to today")
    parser.add_argument("--order-no", help="Optional broker order number filter for the repair candidate")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    init_db(settings)
    trade_date = args.trade_date or datetime.now(UTC).strftime("%Y%m%d")
    snapshot = load_snapshot_file(Path(args.snapshot_file))
    fill_snapshots = collect_broker_fill_snapshots(
        market=args.market,
        ticker=args.ticker.strip().upper(),
        trade_date=trade_date,
        settings=settings,
    )

    writer_queue = WriterQueue.from_settings(settings)
    writer_queue.start()
    try:
        service = ManualFillRepairService(
            writer_queue=writer_queue,
            reconciliation_service=ReconciliationService(writer_queue=writer_queue, settings=settings),
            fill_processor=FillProcessor(writer_queue),
            operations_recorder=OperationsRecorder(writer_queue),
            settings=settings,
        )
        summary = service.repair(
            snapshot,
            market=args.market,
            ticker=args.ticker,
            strategy=args.strategy,
            fill_snapshots=fill_snapshots,
            order_no=args.order_no,
            apply=args.apply,
        )
        print(json.dumps(summary.to_dict(), default=str, indent=2))
    finally:
        writer_queue.stop()


if __name__ == "__main__":
    main()

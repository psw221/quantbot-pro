from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from data.database import Order, PortfolioSnapshot, ReconciliationRun, SystemLog, Trade, get_read_session
from monitor.healthcheck import HealthSnapshot, build_health_snapshot


UTC = timezone.utc
OPEN_ORDER_STATUSES = {"validated", "submitted", "partially_filled", "cancel_pending", "reconcile_hold"}


@dataclass(slots=True)
class DashboardSnapshot:
    generated_at: datetime
    health: HealthSnapshot
    open_orders: list[dict[str, Any]]
    recent_trades: list[dict[str, Any]]
    latest_portfolio_snapshot: dict[str, Any] | None
    reconciliation_summary: dict[str, Any]
    recent_logs: list[dict[str, Any]]


def build_dashboard_snapshot(
    runtime,
    *,
    session_provider=get_read_session,
    now: datetime | None = None,
    max_open_orders: int = 20,
    max_recent_trades: int = 20,
    max_recent_logs: int = 20,
    recent_reconciliation_window: timedelta = timedelta(days=1),
) -> DashboardSnapshot:
    reference_now = now or datetime.now(UTC)
    health = build_health_snapshot(runtime, now=reference_now)

    with session_provider() as session:
        open_orders = _load_open_orders(session, limit=max_open_orders)
        recent_trades = _load_recent_trades(session, limit=max_recent_trades)
        latest_portfolio_snapshot = _load_latest_portfolio_snapshot(session)
        reconciliation_summary = _load_reconciliation_summary(
            session,
            since=reference_now - recent_reconciliation_window,
        )
        recent_logs = _load_recent_logs(session, limit=max_recent_logs)

    return DashboardSnapshot(
        generated_at=reference_now,
        health=health,
        open_orders=open_orders,
        recent_trades=recent_trades,
        latest_portfolio_snapshot=latest_portfolio_snapshot,
        reconciliation_summary=reconciliation_summary,
        recent_logs=recent_logs,
    )


def _load_open_orders(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(Order)
        .where(Order.status.in_(sorted(OPEN_ORDER_STATUSES)))
        .order_by(desc(Order.updated_at), desc(Order.id))
        .limit(limit)
    )
    return [
        {
            "order_id": row.id,
            "client_order_id": row.client_order_id,
            "broker_order_no": row.kis_order_no,
            "ticker": row.ticker,
            "market": row.market,
            "strategy": row.strategy,
            "side": row.side,
            "quantity": row.quantity,
            "price": row.price,
            "status": row.status,
            "updated_at": row.updated_at,
            "error_code": row.error_code,
        }
        for row in rows
    ]


def _load_recent_trades(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = session.scalars(select(Trade).order_by(desc(Trade.executed_at), desc(Trade.id)).limit(limit))
    return [
        {
            "trade_id": row.id,
            "ticker": row.ticker,
            "market": row.market,
            "strategy": row.strategy,
            "side": row.side,
            "quantity": row.quantity,
            "price": row.price,
            "net_amount": row.net_amount,
            "currency": row.currency,
            "executed_at": row.executed_at,
        }
        for row in rows
    ]


def _load_latest_portfolio_snapshot(session: Session) -> dict[str, Any] | None:
    row = session.scalar(
        select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.snapshot_date), desc(PortfolioSnapshot.id)).limit(1)
    )
    if row is None:
        return None
    return {
        "snapshot_date": row.snapshot_date,
        "total_value_krw": row.total_value_krw,
        "cash_krw": row.cash_krw,
        "domestic_value_krw": row.domestic_value_krw,
        "overseas_value_krw": row.overseas_value_krw,
        "usd_krw_rate": row.usd_krw_rate,
        "daily_return": row.daily_return,
        "cumulative_return": row.cumulative_return,
        "drawdown": row.drawdown,
        "max_drawdown": row.max_drawdown,
        "position_count": row.position_count,
    }


def _load_reconciliation_summary(session: Session, *, since: datetime) -> dict[str, Any]:
    rows = list(
        session.scalars(
            select(ReconciliationRun)
            .where(ReconciliationRun.started_at >= since)
            .order_by(desc(ReconciliationRun.started_at), desc(ReconciliationRun.id))
        )
    )
    warning_count = sum(1 for row in rows if row.status == "warning")
    failed_count = sum(1 for row in rows if row.status == "failed")
    mismatch_total = sum(row.mismatch_count for row in rows)
    latest_run = rows[0] if rows else None
    return {
        "run_count": len(rows),
        "warning_count": warning_count,
        "failed_count": failed_count,
        "mismatch_total": mismatch_total,
        "latest_status": None if latest_run is None else latest_run.status,
        "latest_started_at": None if latest_run is None else latest_run.started_at,
    }


def _load_recent_logs(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = session.scalars(select(SystemLog).order_by(desc(SystemLog.created_at), desc(SystemLog.id)).limit(limit))
    return [
        {
            "log_id": row.id,
            "level": row.level,
            "module": row.module,
            "message": row.message,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def dashboard_snapshot_to_dict(snapshot: DashboardSnapshot) -> dict[str, Any]:
    return {
        "generated_at": snapshot.generated_at,
        "health": asdict(snapshot.health),
        "open_orders": snapshot.open_orders,
        "recent_trades": snapshot.recent_trades,
        "latest_portfolio_snapshot": snapshot.latest_portfolio_snapshot,
        "reconciliation_summary": snapshot.reconciliation_summary,
        "recent_logs": snapshot.recent_logs,
    }

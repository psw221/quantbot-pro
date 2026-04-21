from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from data.database import (
    BacktestResult,
    Order,
    PortfolioSnapshot,
    ReconciliationRun,
    SystemLog,
    TokenStore,
    Trade,
    get_read_session,
)
from monitor.healthcheck import (
    HealthSnapshot,
    build_health_snapshot,
    evaluate_health_snapshot,
    health_input_from_runtime_snapshot,
)


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
    recent_manual_restores: list[dict[str, Any]]
    recent_backtests: list[dict[str, Any]]
    operational_summary: dict[str, Any]
    recent_logs: list[dict[str, Any]]


def build_dashboard_snapshot(
    runtime,
    *,
    session_provider=get_read_session,
    now: datetime | None = None,
    max_open_orders: int = 20,
    max_recent_trades: int = 20,
    max_recent_manual_restores: int = 10,
    max_recent_backtests: int = 10,
    max_recent_logs: int = 20,
    recent_reconciliation_window: timedelta = timedelta(days=1),
) -> DashboardSnapshot:
    reference_now = now or datetime.now(UTC)
    health = _build_dashboard_health(runtime=runtime, now=reference_now)

    with session_provider() as session:
        open_orders = _load_open_orders(session, limit=max_open_orders)
        recent_trades = _load_recent_trades(session, limit=max_recent_trades)
        latest_portfolio_snapshot = _load_latest_portfolio_snapshot(session)
        reconciliation_summary = _load_reconciliation_summary(
            session,
            since=reference_now - recent_reconciliation_window,
        )
        recent_manual_restores = _load_recent_manual_restores(session, limit=max_recent_manual_restores)
        recent_backtests = _load_recent_backtests(session, limit=max_recent_backtests)
        recent_logs = _load_recent_logs(session, limit=max_recent_logs)

    return DashboardSnapshot(
        generated_at=reference_now,
        health=health,
        open_orders=open_orders,
        recent_trades=recent_trades,
        latest_portfolio_snapshot=latest_portfolio_snapshot,
        reconciliation_summary=reconciliation_summary,
        recent_manual_restores=recent_manual_restores,
        recent_backtests=recent_backtests,
        operational_summary=_build_operational_summary(
            health=health,
            reconciliation_summary=reconciliation_summary,
            recent_manual_restores=recent_manual_restores,
            recent_backtests=recent_backtests,
            latest_portfolio_snapshot=latest_portfolio_snapshot,
        ),
        recent_logs=recent_logs,
    )


def build_read_only_dashboard_snapshot(
    *,
    env: str,
    session_provider=get_read_session,
    now: datetime | None = None,
    max_open_orders: int = 20,
    max_recent_trades: int = 20,
    max_recent_manual_restores: int = 10,
    max_recent_backtests: int = 10,
    max_recent_logs: int = 20,
    recent_reconciliation_window: timedelta = timedelta(days=1),
    recent_error_window: timedelta = timedelta(days=1),
) -> DashboardSnapshot:
    reference_now = now or datetime.now(UTC)

    with session_provider() as session:
        runtime_snapshot = _load_read_only_runtime_snapshot(
            session,
            env=env,
            now=reference_now,
            recent_error_window=recent_error_window,
        )
        health = _build_dashboard_health(runtime_snapshot=runtime_snapshot, now=reference_now)
        open_orders = _load_open_orders(session, limit=max_open_orders)
        recent_trades = _load_recent_trades(session, limit=max_recent_trades)
        latest_portfolio_snapshot = _load_latest_portfolio_snapshot(session)
        reconciliation_summary = _load_reconciliation_summary(
            session,
            since=reference_now - recent_reconciliation_window,
        )
        recent_manual_restores = _load_recent_manual_restores(session, limit=max_recent_manual_restores)
        recent_backtests = _load_recent_backtests(session, limit=max_recent_backtests)
        recent_logs = _load_recent_logs(session, limit=max_recent_logs)

    return DashboardSnapshot(
        generated_at=reference_now,
        health=health,
        open_orders=open_orders,
        recent_trades=recent_trades,
        latest_portfolio_snapshot=latest_portfolio_snapshot,
        reconciliation_summary=reconciliation_summary,
        recent_manual_restores=recent_manual_restores,
        recent_backtests=recent_backtests,
        operational_summary=_build_operational_summary(
            health=health,
            reconciliation_summary=reconciliation_summary,
            recent_manual_restores=recent_manual_restores,
            recent_backtests=recent_backtests,
            latest_portfolio_snapshot=latest_portfolio_snapshot,
        ),
        recent_logs=recent_logs,
    )


def _build_dashboard_health(
    *,
    runtime=None,
    runtime_snapshot: dict[str, Any] | None = None,
    now: datetime,
) -> HealthSnapshot:
    if runtime_snapshot is not None:
        return evaluate_health_snapshot(health_input_from_runtime_snapshot(runtime_snapshot), now=now)
    return build_health_snapshot(runtime, now=now)


def _load_read_only_runtime_snapshot(
    session: Session,
    *,
    env: str,
    now: datetime,
    recent_error_window: timedelta,
) -> dict[str, Any]:
    token_row = session.scalar(
        select(TokenStore)
        .where(TokenStore.env == env)
        .order_by(desc(TokenStore.issued_at), desc(TokenStore.id))
        .limit(1)
    )
    latest_reconciliation = session.scalar(
        select(ReconciliationRun)
        .where(ReconciliationRun.source_env == env)
        .order_by(desc(ReconciliationRun.completed_at), desc(ReconciliationRun.started_at), desc(ReconciliationRun.id))
        .limit(1)
    )
    recent_error = session.scalar(
        select(SystemLog)
        .where(SystemLog.level.in_(("ERROR", "CRITICAL")))
        .where(SystemLog.created_at >= now - recent_error_window)
        .order_by(desc(SystemLog.created_at), desc(SystemLog.id))
        .limit(1)
    )

    last_poll_success_at = None
    trading_blocked = False
    if latest_reconciliation is not None:
        last_poll_success_at = latest_reconciliation.completed_at or latest_reconciliation.started_at
        trading_blocked = latest_reconciliation.status in {"warning", "failed"} and latest_reconciliation.mismatch_count > 0

    last_token_refresh_at = None
    if token_row is not None and token_row.is_valid:
        last_token_refresh_at = token_row.issued_at

    return {
        "scheduler_running": False,
        "trading_blocked": trading_blocked,
        "last_token_refresh_at": last_token_refresh_at,
        "last_poll_success_at": last_poll_success_at,
        "consecutive_poll_failures": 0,
        "last_error": None if recent_error is None else recent_error.message,
        "health_status": None,
        "writer_queue": {
            "running": False,
            "degraded": False,
            "queue_depth": 0,
            "last_error": None,
        },
    }


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
        "latest_run_type": None if latest_run is None else latest_run.run_type,
    }


def _load_recent_manual_restores(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(ReconciliationRun)
        .where(ReconciliationRun.run_type == "manual_restore")
        .order_by(desc(ReconciliationRun.started_at), desc(ReconciliationRun.id))
        .limit(limit)
    )
    return [
        {
            "reconciliation_run_id": row.id,
            "status": row.status,
            "mismatch_count": row.mismatch_count,
            "started_at": row.started_at,
            "completed_at": row.completed_at,
            "source_env": row.source_env,
        }
        for row in rows
    ]


def _load_recent_backtests(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = session.scalars(select(BacktestResult).order_by(desc(BacktestResult.created_at), desc(BacktestResult.id)).limit(limit))
    return [
        {
            "backtest_result_id": row.id,
            "strategy": row.strategy,
            "market": row.market,
            "start_date": row.start_date,
            "end_date": row.end_date,
            "annual_return": row.annual_return,
            "sharpe_ratio": row.sharpe_ratio,
            "max_drawdown": row.max_drawdown,
            "win_rate": row.win_rate,
            "total_trades": row.total_trades,
            "profit_factor": row.profit_factor,
            "notes": row.notes,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def _build_operational_summary(
    *,
    health: HealthSnapshot,
    reconciliation_summary: dict[str, Any],
    recent_manual_restores: list[dict[str, Any]],
    recent_backtests: list[dict[str, Any]],
    latest_portfolio_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    latest_restore = recent_manual_restores[0] if recent_manual_restores else None
    latest_backtest = recent_backtests[0] if recent_backtests else None
    latest_reconciliation_status = reconciliation_summary.get("latest_status")
    mismatch_total = int(reconciliation_summary.get("mismatch_total", 0) or 0)

    return {
        "health_status": health.status.value,
        "trading_blocked": health.trading_blocked,
        "token_stale": health.token_stale,
        "poll_stale": health.poll_stale,
        "writer_queue_degraded": health.writer_queue_degraded,
        "has_recent_mismatch": mismatch_total > 0,
        "latest_reconciliation_status": latest_reconciliation_status,
        "latest_reconciliation_run_type": reconciliation_summary.get("latest_run_type"),
        "reconciliation_warning_count": reconciliation_summary.get("warning_count", 0),
        "reconciliation_failed_count": reconciliation_summary.get("failed_count", 0),
        "recent_manual_restore_count": len(recent_manual_restores),
        "latest_manual_restore_status": None if latest_restore is None else latest_restore["status"],
        "latest_manual_restore_started_at": None if latest_restore is None else latest_restore["started_at"],
        "recent_backtest_count": len(recent_backtests),
        "latest_backtest_strategy": None if latest_backtest is None else latest_backtest["strategy"],
        "latest_backtest_market": None if latest_backtest is None else latest_backtest["market"],
        "latest_backtest_created_at": None if latest_backtest is None else latest_backtest["created_at"],
        "latest_portfolio_snapshot_date": None if latest_portfolio_snapshot is None else latest_portfolio_snapshot["snapshot_date"],
    }


def _load_recent_logs(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = session.scalars(select(SystemLog).order_by(desc(SystemLog.created_at), desc(SystemLog.id)).limit(limit))
    return [
        {
            "log_id": row.id,
            "level": row.level,
            "module": row.module,
            "message": row.message,
            "extra": _coerce_log_extra(row.extra_json),
            "created_at": row.created_at,
        }
        for row in rows
    ]


def _coerce_log_extra(extra_json: str | None) -> dict[str, Any] | None:
    if not extra_json:
        return None
    try:
        parsed = json.loads(extra_json)
    except json.JSONDecodeError:
        return {"raw": extra_json}
    return parsed if isinstance(parsed, dict) else {"raw": extra_json}


def dashboard_snapshot_to_dict(snapshot: DashboardSnapshot) -> dict[str, Any]:
    return {
        "generated_at": snapshot.generated_at,
        "health": asdict(snapshot.health),
        "open_orders": snapshot.open_orders,
        "recent_trades": snapshot.recent_trades,
        "latest_portfolio_snapshot": snapshot.latest_portfolio_snapshot,
        "reconciliation_summary": snapshot.reconciliation_summary,
        "recent_manual_restores": snapshot.recent_manual_restores,
        "recent_backtests": snapshot.recent_backtests,
        "operational_summary": snapshot.operational_summary,
        "recent_logs": snapshot.recent_logs,
    }

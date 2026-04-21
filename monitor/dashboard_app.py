from __future__ import annotations

from datetime import datetime
from typing import Any

from core.settings import get_settings
from monitor.dashboard import DashboardSnapshot, build_read_only_dashboard_snapshot


def render_dashboard(snapshot: DashboardSnapshot, *, st_module: Any) -> None:
    st_module.title("QuantBot Pro Dashboard")
    st_module.caption(f"generated_at={_format_value(snapshot.generated_at)}")
    render_operations_summary_panel(snapshot, st_module=st_module)
    render_auto_trading_diagnostics_panel(snapshot, st_module=st_module)

    st_module.subheader("Health")
    st_module.json(
        {
            "status": snapshot.health.status.value,
            "trading_blocked": snapshot.health.trading_blocked,
            "token_stale": snapshot.health.token_stale,
            "poll_stale": snapshot.health.poll_stale,
            "last_token_refresh_at": _format_value(snapshot.health.last_token_refresh_at),
            "last_poll_success_at": _format_value(snapshot.health.last_poll_success_at),
            "last_error": snapshot.health.last_error,
            "details": _normalize_mapping(snapshot.health.details),
        }
    )

    st_module.subheader("Open Orders")
    _render_rows(
        st_module,
        rows=snapshot.open_orders,
        empty_message="No open orders",
    )

    st_module.subheader("Recent Trades")
    _render_rows(
        st_module,
        rows=snapshot.recent_trades,
        empty_message="No recent trades",
    )

    st_module.subheader("Reconciliation")
    st_module.json(_normalize_mapping(snapshot.reconciliation_summary))

    st_module.subheader("Recent Logs")
    _render_rows(
        st_module,
        rows=snapshot.recent_logs,
        empty_message="No recent system logs",
    )


def render_operations_summary_panel(snapshot: DashboardSnapshot, *, st_module: Any) -> None:
    st_module.subheader("Operations Summary")
    cards = [
        ("Health", snapshot.operational_summary.get("health_status", "unknown")),
        ("Trading Blocked", _format_bool(snapshot.operational_summary.get("trading_blocked"))),
        ("Poll Stale", _format_bool(snapshot.operational_summary.get("poll_stale"))),
        ("Writer Queue", _format_writer_queue(snapshot.operational_summary.get("writer_queue_degraded"))),
        ("Recent Mismatch", _format_bool(snapshot.operational_summary.get("has_recent_mismatch"))),
        ("Latest Reconciliation", snapshot.operational_summary.get("latest_reconciliation_status", "n/a")),
        ("Latest Restore", snapshot.operational_summary.get("latest_manual_restore_status", "n/a")),
        ("Latest Backtest", _format_backtest_summary(snapshot.operational_summary)),
    ]

    columns = st_module.columns(4)
    for index, (label, value) in enumerate(cards):
        column = columns[index % len(columns)]
        column.metric(label=label, value=value)


def render_auto_trading_diagnostics_panel(snapshot: DashboardSnapshot, *, st_module: Any) -> None:
    st_module.subheader("Auto-Trading Diagnostics")
    diagnostics = build_auto_trading_diagnostics(snapshot)
    if diagnostics is None:
        st_module.info("No recent auto-trading cycle logs")
        return

    cards = [
        ("Cycle Status", diagnostics["cycle_status"]),
        ("Market", diagnostics["market"]),
        ("Signals", diagnostics["signals_generated"]),
        ("Resolved", diagnostics["signals_resolved"]),
        ("Candidates", diagnostics["order_candidate_count"]),
        ("Rejected", diagnostics["rejected_signal_count"]),
        ("Submitted", diagnostics["orders_submitted"]),
        ("Top Rejections", diagnostics["rejection_reason_summary"]),
    ]
    columns = st_module.columns(4)
    for index, (label, value) in enumerate(cards):
        columns[index % len(columns)].metric(label=label, value=value)

    st_module.json(
        {
            "message": diagnostics["message"],
            "created_at": diagnostics["created_at"],
            "reason": diagnostics["reason"],
            "error_message": diagnostics["error_message"],
            "submitted_notional_krw": diagnostics["submitted_notional_krw"],
        }
    )


def build_auto_trading_diagnostics(snapshot: DashboardSnapshot) -> dict[str, Any] | None:
    for row in snapshot.recent_logs:
        message = row.get("message")
        if not isinstance(message, str) or not message.startswith("auto-trading cycle"):
            continue
        extra = row.get("extra")
        extra_fields = extra if isinstance(extra, dict) else {}
        return {
            "message": message,
            "cycle_status": _extract_cycle_status(message),
            "market": str(extra_fields.get("market") or "n/a"),
            "signals_generated": _format_metric_value(extra_fields.get("signals_generated")),
            "signals_resolved": _format_metric_value(extra_fields.get("signals_resolved")),
            "order_candidate_count": _format_metric_value(extra_fields.get("order_candidate_count")),
            "rejected_signal_count": _format_metric_value(extra_fields.get("rejected_signal_count")),
            "orders_submitted": _format_metric_value(extra_fields.get("orders_submitted")),
            "rejection_reason_summary": str(extra_fields.get("rejection_reason_summary") or "n/a"),
            "reason": str(extra_fields.get("reason") or "n/a"),
            "error_message": str(extra_fields.get("error_message") or "n/a"),
            "submitted_notional_krw": _format_metric_value(extra_fields.get("submitted_notional_krw")),
            "created_at": _format_value(row.get("created_at")),
        }
    return None


def _render_rows(st_module: Any, *, rows: list[dict[str, Any]], empty_message: str) -> None:
    if not rows:
        st_module.info(empty_message)
        return
    st_module.dataframe(_normalize_rows(rows), use_container_width=True)


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_normalize_mapping(row) for row in rows]


def _normalize_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: _format_value(value) for key, value in payload.items()}


def _format_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _format_bool(value: Any) -> str:
    return "Yes" if bool(value) else "No"


def _format_writer_queue(degraded: Any) -> str:
    return "Degraded" if bool(degraded) else "Healthy"


def _format_backtest_summary(summary: dict[str, Any]) -> str:
    strategy = summary.get("latest_backtest_strategy")
    market = summary.get("latest_backtest_market")
    if not strategy:
        return "n/a"
    if not market:
        return str(strategy)
    return f"{strategy} ({market})"


def _extract_cycle_status(message: str) -> str:
    suffix = message.removeprefix("auto-trading cycle").strip()
    return suffix or "unknown"


def _format_metric_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    if value is None:
        return "n/a"
    return str(value)


def main() -> None:
    import streamlit as st

    settings = get_settings()
    st.set_page_config(page_title="QuantBot Pro Dashboard", layout="wide")
    snapshot = build_read_only_dashboard_snapshot(env=settings.env)
    render_dashboard(snapshot, st_module=st)


if __name__ == "__main__":
    main()

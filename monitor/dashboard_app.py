from __future__ import annotations

from datetime import datetime
from typing import Any

from core.settings import Settings, get_settings
from monitor.dashboard import DashboardSnapshot, build_read_only_dashboard_snapshot


def render_dashboard(snapshot: DashboardSnapshot, *, st_module: Any, settings: Settings | None = None) -> None:
    resolved_settings = settings or get_settings()
    st_module.title("QuantBot Pro Dashboard")
    st_module.caption(f"generated_at={_format_value(snapshot.generated_at)}")
    render_operations_summary_panel(snapshot, st_module=st_module)
    render_auto_trading_diagnostics_panel(snapshot, st_module=st_module)
    render_strategy_budget_panel(snapshot, st_module=st_module, settings=resolved_settings)

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


def render_strategy_budget_panel(snapshot: DashboardSnapshot, *, st_module: Any, settings: Settings) -> None:
    st_module.subheader("Strategy Budget")
    summary = build_strategy_budget_summary(snapshot, settings=settings)

    cards = [
        ("Cash KRW", _format_currency(summary["cash_available_krw"])),
        ("Gross Budget", _format_currency(summary["gross_budget_krw"])),
        ("KR Budget", _format_currency(summary["kr_market_budget_krw"])),
        ("Single-Stock Cap", _format_currency(summary["single_stock_cap_krw"])),
        ("Cycle Cap", _format_currency(summary["cycle_notional_cap_krw"])),
        ("Active Strategies", summary["active_strategy_labels"]),
    ]
    columns = st_module.columns(3)
    for index, (label, value) in enumerate(cards):
        columns[index % len(columns)].metric(label=label, value=value)

    if not summary["snapshot_available"]:
        st_module.info("No latest portfolio snapshot available; strategy budget uses 0 KRW cash until snapshot data exists.")

    st_module.dataframe(_normalize_rows(summary["strategy_rows"]), use_container_width=True)


def build_strategy_budget_summary(snapshot: DashboardSnapshot, *, settings: Settings) -> dict[str, Any]:
    latest_snapshot = snapshot.latest_portfolio_snapshot or {}
    cash_available = float(latest_snapshot.get("cash_krw") or 0.0)
    gross_budget = cash_available * (1 - settings.allocation.cash_buffer)
    kr_market_budget = gross_budget * settings.allocation.domestic
    single_stock_cap = cash_available * settings.risk.max_single_stock_domestic
    cycle_notional_cap = float(settings.auto_trading.max_order_notional_per_cycle)
    strategy_weights = settings.strategy_weights.model_dump()
    active_strategies = set(settings.auto_trading.strategies)

    strategy_rows: list[dict[str, Any]] = []
    for strategy_name, weight in strategy_weights.items():
        target_notional = kr_market_budget * float(weight)
        candidate_cap = min(target_notional, single_stock_cap)
        strategy_rows.append(
            {
                "strategy": strategy_name,
                "active_in_auto_trading": strategy_name in active_strategies,
                "strategy_weight_pct": round(float(weight) * 100, 2),
                "target_notional_krw": round(target_notional, 2),
                "candidate_cap_krw": round(candidate_cap, 2),
            }
        )

    return {
        "snapshot_available": snapshot.latest_portfolio_snapshot is not None,
        "snapshot_date": None if snapshot.latest_portfolio_snapshot is None else snapshot.latest_portfolio_snapshot.get("snapshot_date"),
        "cash_available_krw": round(cash_available, 2),
        "gross_budget_krw": round(gross_budget, 2),
        "kr_market_budget_krw": round(kr_market_budget, 2),
        "single_stock_cap_krw": round(single_stock_cap, 2),
        "cycle_notional_cap_krw": round(cycle_notional_cap, 2),
        "active_strategy_labels": ", ".join(settings.auto_trading.strategies),
        "strategy_rows": strategy_rows,
    }


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


def _format_currency(value: float) -> str:
    return f"{value:,.0f} KRW"


def main() -> None:
    import streamlit as st

    settings = get_settings()
    st.set_page_config(page_title="QuantBot Pro Dashboard", layout="wide")
    snapshot = build_read_only_dashboard_snapshot(env=settings.env)
    render_dashboard(snapshot, st_module=st, settings=settings)


if __name__ == "__main__":
    main()

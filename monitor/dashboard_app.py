from __future__ import annotations

from datetime import datetime
from typing import Any

from core.settings import get_settings
from monitor.dashboard import DashboardSnapshot, build_read_only_dashboard_snapshot


def render_dashboard(snapshot: DashboardSnapshot, *, st_module: Any) -> None:
    st_module.title("QuantBot Pro Dashboard")
    st_module.caption(f"generated_at={_format_value(snapshot.generated_at)}")

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


def main() -> None:
    import streamlit as st

    settings = get_settings()
    st.set_page_config(page_title="QuantBot Pro Dashboard", layout="wide")
    snapshot = build_read_only_dashboard_snapshot(env=settings.env)
    render_dashboard(snapshot, st_module=st)


if __name__ == "__main__":
    main()

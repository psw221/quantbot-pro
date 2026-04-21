from __future__ import annotations

from datetime import datetime, timezone

from core.models import RuntimeHealthStatus
from monitor.dashboard import DashboardSnapshot
from monitor.healthcheck import HealthSnapshot
from monitor.dashboard_app import render_dashboard


class FakeStreamlit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def title(self, value: str) -> None:
        self.calls.append(("title", value))

    def caption(self, value: str) -> None:
        self.calls.append(("caption", value))

    def subheader(self, value: str) -> None:
        self.calls.append(("subheader", value))

    def json(self, value: object) -> None:
        self.calls.append(("json", value))

    def info(self, value: str) -> None:
        self.calls.append(("info", value))

    def dataframe(self, value: object, **kwargs: object) -> None:
        self.calls.append(("dataframe", {"value": value, "kwargs": kwargs}))


def test_render_dashboard_outputs_layer5_skeleton_sections() -> None:
    snapshot = DashboardSnapshot(
        generated_at=datetime(2026, 4, 21, 13, 30, tzinfo=timezone.utc),
        health=HealthSnapshot(
            status=RuntimeHealthStatus.WARNING,
            trading_blocked=False,
            scheduler_running=False,
            writer_queue_running=False,
            writer_queue_degraded=False,
            queue_depth=0,
            token_stale=False,
            poll_stale=True,
            last_token_refresh_at=datetime(2026, 4, 21, 11, 30, tzinfo=timezone.utc),
            last_poll_success_at=datetime(2026, 4, 21, 13, 0, tzinfo=timezone.utc),
            consecutive_poll_failures=0,
            last_error=None,
            details={"status_source": "external_canonical"},
        ),
        open_orders=[
            {
                "order_id": 1,
                "ticker": "005930",
                "status": "submitted",
                "updated_at": datetime(2026, 4, 21, 13, 15, tzinfo=timezone.utc),
            }
        ],
        recent_trades=[
            {
                "trade_id": 11,
                "ticker": "005930",
                "price": 217500.0,
                "executed_at": datetime(2026, 4, 21, 12, 45, tzinfo=timezone.utc),
            }
        ],
        latest_portfolio_snapshot=None,
        reconciliation_summary={
            "run_count": 2,
            "warning_count": 0,
            "failed_count": 0,
            "mismatch_total": 0,
            "latest_status": "ok",
            "latest_started_at": datetime(2026, 4, 21, 13, 10, tzinfo=timezone.utc),
            "latest_run_type": "scheduled_poll",
        },
        recent_manual_restores=[],
        recent_backtests=[],
        operational_summary={},
        recent_logs=[
            {
                "log_id": 91,
                "level": "INFO",
                "module": "execution.runtime",
                "message": "auto-trading cycle completed",
                "created_at": datetime(2026, 4, 21, 13, 15, tzinfo=timezone.utc),
            }
        ],
    )
    fake_st = FakeStreamlit()

    render_dashboard(snapshot, st_module=fake_st)

    headers = [value for kind, value in fake_st.calls if kind == "subheader"]
    assert headers == [
        "Health",
        "Open Orders",
        "Recent Trades",
        "Reconciliation",
        "Recent Logs",
    ]
    assert any(kind == "dataframe" for kind, _ in fake_st.calls)
    assert any(kind == "json" for kind, _ in fake_st.calls)

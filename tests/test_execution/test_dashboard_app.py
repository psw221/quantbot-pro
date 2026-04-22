from __future__ import annotations

from datetime import datetime, timezone

from core.models import RuntimeHealthStatus
from monitor.dashboard import DashboardSnapshot
from monitor.healthcheck import HealthSnapshot
from monitor.dashboard_app import (
    build_auto_trading_diagnostics,
    build_strategy_budget_summary,
    build_tax_dashboard_summary,
    render_dashboard,
)
from tests.test_execution.test_bootstrap import build_settings


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

    def columns(self, count: int) -> list["FakeColumn"]:
        self.calls.append(("columns", count))
        return [FakeColumn(self.calls, index=i) for i in range(count)]


class FakeColumn:
    def __init__(self, calls: list[tuple[str, object]], *, index: int) -> None:
        self._calls = calls
        self._index = index

    def metric(self, *, label: str, value: object, delta: object | None = None) -> None:
        self._calls.append(
            (
                "metric",
                {
                    "column_index": self._index,
                    "label": label,
                    "value": value,
                    "delta": delta,
                },
            )
        )


class FakeTaxCalculator:
    def calculate_yearly_summary(self, year: int, market: str | None = None) -> dict[str, object]:
        return {
            "year": year,
            "market": market,
            "sell_trade_count": 2,
            "total_quantity": 4,
            "realized_gain_loss_krw": 180000.0,
            "taxable_gain_krw": 210000.0,
            "total_fees_krw": 5000.0,
            "total_taxes_krw": 1200.0,
            "by_market": {
                "KR": {
                    "sell_trade_count": 1,
                    "total_quantity": 1,
                    "realized_gain_loss_krw": 20000.0,
                    "taxable_gain_krw": 25000.0,
                    "total_fees_krw": 1000.0,
                    "total_taxes_krw": 0.0,
                },
                "US": {
                    "sell_trade_count": 1,
                    "total_quantity": 3,
                    "realized_gain_loss_krw": 160000.0,
                    "taxable_gain_krw": 185000.0,
                    "total_fees_krw": 4000.0,
                    "total_taxes_krw": 1200.0,
                },
            },
        }


def test_render_dashboard_outputs_layer5_skeleton_sections(tmp_path) -> None:
    settings = build_settings(tmp_path)
    tax_calculator = FakeTaxCalculator()
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
        latest_portfolio_snapshot={
            "snapshot_date": datetime(2026, 4, 21, 13, 0, tzinfo=timezone.utc),
            "cash_krw": 8_651_886.0,
        },
        reconciliation_summary={
            "run_count": 2,
            "warning_count": 0,
            "failed_count": 0,
            "mismatch_total": 0,
            "latest_status": "ok",
            "latest_started_at": datetime(2026, 4, 21, 13, 10, tzinfo=timezone.utc),
            "latest_run_type": "scheduled_poll",
        },
        recent_manual_restores=[
            {
                "reconciliation_run_id": 41,
                "status": "ok",
                "mismatch_count": 0,
                "started_at": datetime(2026, 4, 21, 11, 0, tzinfo=timezone.utc),
                "completed_at": datetime(2026, 4, 21, 11, 1, tzinfo=timezone.utc),
                "source_env": "vts",
            }
        ],
        recent_backtests=[
            {
                "backtest_result_id": 17,
                "strategy": "trend_following",
                "market": "KR",
                "start_date": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "end_date": datetime(2026, 4, 1, tzinfo=timezone.utc),
                "annual_return": 0.12,
                "sharpe_ratio": 1.3,
                "max_drawdown": -0.08,
                "win_rate": 0.56,
                "total_trades": 24,
                "profit_factor": 1.4,
                "notes": "engine=fallback",
                "created_at": datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc),
            }
        ],
        operational_summary={
            "health_status": "warning",
            "trading_blocked": False,
            "poll_stale": True,
            "writer_queue_degraded": False,
            "has_recent_mismatch": False,
            "latest_reconciliation_status": "ok",
            "latest_manual_restore_status": None,
            "latest_backtest_strategy": "trend_following",
            "latest_backtest_market": "KR",
        },
        recent_logs=[
            {
                "log_id": 91,
                "level": "INFO",
                "module": "execution.runtime",
                "message": "auto-trading cycle completed",
                "extra": {
                    "market": "KR",
                    "signals_generated": 1,
                    "signals_resolved": 1,
                    "order_candidate_count": 1,
                    "rejected_signal_count": 2,
                    "orders_submitted": 1,
                    "rejection_reason_summary": "existing_position_reentry_blocked:1,no_position_to_sell:1",
                    "submitted_notional_krw": 217500.0,
                },
                "created_at": datetime(2026, 4, 21, 13, 15, tzinfo=timezone.utc),
            }
        ],
    )
    fake_st = FakeStreamlit()

    render_dashboard(snapshot, st_module=fake_st, settings=settings, tax_calculator=tax_calculator)

    headers = [value for kind, value in fake_st.calls if kind == "subheader"]
    assert headers == [
        "Operations Summary",
        "Auto-Trading Diagnostics",
        "Strategy Budget",
        "Tax Summary",
        "Health",
        "Open Orders",
        "Recent Trades",
        "Reconciliation",
        "Recent Manual Restores",
        "Recent Backtests",
        "Recent Logs",
    ]
    metrics = [value for kind, value in fake_st.calls if kind == "metric"]
    assert len(metrics) == 28
    assert any(metric["label"] == "Trading Blocked" and metric["value"] == "No" for metric in metrics)
    assert any(metric["label"] == "Poll Stale" and metric["value"] == "Yes" for metric in metrics)
    assert any(metric["label"] == "Latest Backtest" and metric["value"] == "trend_following (KR)" for metric in metrics)
    assert any(metric["label"] == "Top Rejections" and metric["value"] == "existing_position_reentry_blocked:1,no_position_to_sell:1" for metric in metrics)
    assert any(metric["label"] == "Single-Stock Cap" and metric["value"] == "432,594 KRW" for metric in metrics)
    assert any(metric["label"] == "Taxes" and metric["value"] == "1,200 KRW" for metric in metrics)
    assert any(kind == "dataframe" for kind, _ in fake_st.calls)
    assert any(kind == "json" for kind, _ in fake_st.calls)


def test_render_dashboard_shows_empty_restore_and_backtest_messages(tmp_path) -> None:
    settings = build_settings(tmp_path)
    snapshot = DashboardSnapshot(
        generated_at=datetime(2026, 4, 21, 13, 30, tzinfo=timezone.utc),
        health=HealthSnapshot(
            status=RuntimeHealthStatus.NORMAL,
            trading_blocked=False,
            scheduler_running=False,
            writer_queue_running=False,
            writer_queue_degraded=False,
            queue_depth=0,
            token_stale=False,
            poll_stale=False,
            last_token_refresh_at=None,
            last_poll_success_at=None,
            consecutive_poll_failures=0,
            last_error=None,
            details={"status_source": "external_canonical"},
        ),
        open_orders=[],
        recent_trades=[],
        latest_portfolio_snapshot=None,
        reconciliation_summary={},
        recent_manual_restores=[],
        recent_backtests=[],
        operational_summary={},
        recent_logs=[],
    )
    fake_st = FakeStreamlit()

    render_dashboard(snapshot, st_module=fake_st, settings=settings, tax_calculator=FakeTaxCalculator())

    info_messages = [value for kind, value in fake_st.calls if kind == "info"]
    assert "No recent manual restores" in info_messages
    assert "No recent backtests" in info_messages


def test_build_auto_trading_diagnostics_returns_latest_cycle_summary() -> None:
    snapshot = DashboardSnapshot(
        generated_at=datetime(2026, 4, 21, 14, 0, tzinfo=timezone.utc),
        health=HealthSnapshot(
            status=RuntimeHealthStatus.NORMAL,
            trading_blocked=False,
            scheduler_running=False,
            writer_queue_running=False,
            writer_queue_degraded=False,
            queue_depth=0,
            token_stale=False,
            poll_stale=False,
            last_token_refresh_at=None,
            last_poll_success_at=None,
            consecutive_poll_failures=0,
            last_error=None,
            details={"status_source": "external_canonical"},
        ),
        open_orders=[],
        recent_trades=[],
        latest_portfolio_snapshot=None,
        reconciliation_summary={},
        recent_manual_restores=[],
        recent_backtests=[],
        operational_summary={},
        recent_logs=[
            {
                "log_id": 1,
                "level": "INFO",
                "module": "execution.runtime",
                "message": "auto-trading cycle skipped",
                "extra": {
                    "market": "KR",
                    "reason": "trading_blocked",
                },
                "created_at": datetime(2026, 4, 21, 13, 45, tzinfo=timezone.utc),
            }
        ],
    )

    diagnostics = build_auto_trading_diagnostics(snapshot)

    assert diagnostics is not None
    assert diagnostics["cycle_status"] == "skipped"
    assert diagnostics["reason"] == "trading_blocked"
    assert diagnostics["orders_submitted"] == "n/a"


def test_build_strategy_budget_summary_uses_latest_snapshot_cash(tmp_path) -> None:
    settings = build_settings(tmp_path)
    snapshot = DashboardSnapshot(
        generated_at=datetime(2026, 4, 21, 14, 0, tzinfo=timezone.utc),
        health=HealthSnapshot(
            status=RuntimeHealthStatus.NORMAL,
            trading_blocked=False,
            scheduler_running=False,
            writer_queue_running=False,
            writer_queue_degraded=False,
            queue_depth=0,
            token_stale=False,
            poll_stale=False,
            last_token_refresh_at=None,
            last_poll_success_at=None,
            consecutive_poll_failures=0,
            last_error=None,
            details={"status_source": "external_canonical"},
        ),
        open_orders=[],
        recent_trades=[],
        latest_portfolio_snapshot={
            "snapshot_date": datetime(2026, 4, 21, 13, 0, tzinfo=timezone.utc),
            "cash_krw": 8_651_886.0,
        },
        reconciliation_summary={},
        recent_manual_restores=[],
        recent_backtests=[],
        operational_summary={},
        recent_logs=[],
    )

    summary = build_strategy_budget_summary(snapshot, settings=settings)

    assert summary["snapshot_available"] is True
    assert summary["cash_available_krw"] == 8_651_886.0
    assert summary["gross_budget_krw"] == 7_786_697.4
    assert summary["kr_market_budget_krw"] == 4_672_018.44
    assert summary["single_stock_cap_krw"] == 432_594.3
    assert summary["cycle_notional_cap_krw"] == 500_000.0
    rows = {row["strategy"]: row for row in summary["strategy_rows"]}
    assert rows["dual_momentum"]["target_notional_krw"] == 1_401_605.53
    assert rows["dual_momentum"]["candidate_cap_krw"] == 432_594.3
    assert rows["factor_investing"]["active_in_auto_trading"] is False


def test_build_tax_dashboard_summary_returns_total_and_by_market_rows() -> None:
    snapshot = DashboardSnapshot(
        generated_at=datetime(2026, 4, 21, 14, 0, tzinfo=timezone.utc),
        health=HealthSnapshot(
            status=RuntimeHealthStatus.NORMAL,
            trading_blocked=False,
            scheduler_running=False,
            writer_queue_running=False,
            writer_queue_degraded=False,
            queue_depth=0,
            token_stale=False,
            poll_stale=False,
            last_token_refresh_at=None,
            last_poll_success_at=None,
            consecutive_poll_failures=0,
            last_error=None,
            details={"status_source": "external_canonical"},
        ),
        open_orders=[],
        recent_trades=[],
        latest_portfolio_snapshot=None,
        reconciliation_summary={},
        recent_manual_restores=[],
        recent_backtests=[],
        operational_summary={},
        recent_logs=[],
    )

    summary = build_tax_dashboard_summary(snapshot, tax_calculator=FakeTaxCalculator())

    assert summary["year"] == 2026
    assert summary["sell_trade_count"] == 2
    assert summary["realized_gain_loss_krw"] == 180000.0
    assert summary["total_taxes_krw"] == 1200.0
    assert len(summary["by_market_rows"]) == 2
    assert summary["by_market_rows"][0]["market"] == "KR"

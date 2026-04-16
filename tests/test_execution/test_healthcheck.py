from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.models import RuntimeHealthStatus
from monitor.healthcheck import HealthEvaluationInput, evaluate_health_snapshot, health_input_from_runtime_snapshot


def test_evaluate_health_snapshot_reports_critical_when_trading_is_blocked() -> None:
    now = datetime(2026, 4, 16, 1, 0, tzinfo=timezone.utc)

    snapshot = evaluate_health_snapshot(
        HealthEvaluationInput(
            scheduler_running=True,
            writer_queue_running=True,
            writer_queue_degraded=False,
            queue_depth=0,
            last_token_refresh_at=now - timedelta(hours=1),
            last_poll_success_at=now - timedelta(minutes=5),
            consecutive_poll_failures=0,
            trading_blocked=True,
            last_error="polling_mismatch_detected",
            runtime_health_status=RuntimeHealthStatus.WARNING.value,
        ),
        now=now,
    )

    assert snapshot.status == RuntimeHealthStatus.CRITICAL
    assert snapshot.details["status_source"] == "external_canonical"
    assert snapshot.details["runtime_health_status"] == RuntimeHealthStatus.WARNING.value


def test_evaluate_health_snapshot_reports_warning_for_stale_inputs_or_last_error() -> None:
    now = datetime(2026, 4, 16, 1, 0, tzinfo=timezone.utc)

    token_stale = evaluate_health_snapshot(
        HealthEvaluationInput(
            scheduler_running=True,
            writer_queue_running=True,
            writer_queue_degraded=False,
            queue_depth=0,
            last_token_refresh_at=now - timedelta(days=2),
            last_poll_success_at=now - timedelta(minutes=5),
            consecutive_poll_failures=0,
            trading_blocked=False,
        ),
        now=now,
    )
    poll_stale = evaluate_health_snapshot(
        HealthEvaluationInput(
            scheduler_running=True,
            writer_queue_running=True,
            writer_queue_degraded=False,
            queue_depth=0,
            last_token_refresh_at=now - timedelta(hours=1),
            last_poll_success_at=now - timedelta(minutes=30),
            consecutive_poll_failures=2,
            trading_blocked=False,
        ),
        now=now,
    )
    last_error = evaluate_health_snapshot(
        HealthEvaluationInput(
            scheduler_running=True,
            writer_queue_running=True,
            writer_queue_degraded=False,
            queue_depth=0,
            last_token_refresh_at=now - timedelta(hours=1),
            last_poll_success_at=now - timedelta(minutes=5),
            consecutive_poll_failures=0,
            trading_blocked=False,
            last_error="token_refresh_failed",
        ),
        now=now,
    )

    assert token_stale.status == RuntimeHealthStatus.WARNING
    assert token_stale.token_stale is True
    assert poll_stale.status == RuntimeHealthStatus.WARNING
    assert poll_stale.poll_stale is True
    assert last_error.status == RuntimeHealthStatus.WARNING
    assert last_error.last_error == "token_refresh_failed"


def test_evaluate_health_snapshot_reports_normal_when_inputs_are_healthy() -> None:
    now = datetime(2026, 4, 16, 1, 0, tzinfo=timezone.utc)

    snapshot = evaluate_health_snapshot(
        HealthEvaluationInput(
            scheduler_running=True,
            writer_queue_running=True,
            writer_queue_degraded=False,
            queue_depth=1,
            last_token_refresh_at=now - timedelta(hours=1),
            last_poll_success_at=now - timedelta(minutes=5),
            consecutive_poll_failures=0,
            trading_blocked=False,
        ),
        now=now,
    )

    assert snapshot.status == RuntimeHealthStatus.NORMAL
    assert snapshot.token_stale is False
    assert snapshot.poll_stale is False


def test_health_input_from_runtime_snapshot_extracts_external_contract_fields() -> None:
    now = datetime(2026, 4, 16, 1, 0, tzinfo=timezone.utc)
    runtime_snapshot = {
        "scheduler_running": True,
        "health_status": RuntimeHealthStatus.WARNING.value,
        "trading_blocked": True,
        "last_token_refresh_at": now - timedelta(hours=1),
        "last_poll_success_at": now - timedelta(minutes=5),
        "consecutive_poll_failures": 3,
        "last_error": "polling_mismatch_detected",
        "writer_queue": {
            "running": True,
            "degraded": False,
            "queue_depth": 2,
            "last_error": None,
        },
    }

    health_input = health_input_from_runtime_snapshot(runtime_snapshot)

    assert health_input.runtime_health_status == RuntimeHealthStatus.WARNING.value
    assert health_input.trading_blocked is True
    assert health_input.queue_depth == 2

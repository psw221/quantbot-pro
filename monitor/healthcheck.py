from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from core.models import RuntimeHealthStatus
from execution.runtime import TradingRuntime


UTC = timezone.utc


@dataclass(slots=True)
class HealthSnapshot:
    status: RuntimeHealthStatus
    trading_blocked: bool
    scheduler_running: bool
    writer_queue_running: bool
    writer_queue_degraded: bool
    queue_depth: int
    token_stale: bool
    poll_stale: bool
    last_token_refresh_at: datetime | None
    last_poll_success_at: datetime | None
    consecutive_poll_failures: int
    last_error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HealthEvaluationInput:
    scheduler_running: bool
    writer_queue_running: bool
    writer_queue_degraded: bool
    queue_depth: int
    last_token_refresh_at: datetime | None
    last_poll_success_at: datetime | None
    consecutive_poll_failures: int
    trading_blocked: bool
    last_error: str | None = None
    runtime_health_status: str | None = None
    writer_queue_last_error: str | None = None


def build_health_snapshot(
    runtime: TradingRuntime,
    *,
    now: datetime | None = None,
    token_stale_after: timedelta = timedelta(days=1),
    poll_stale_after: timedelta = timedelta(minutes=20),
) -> HealthSnapshot:
    return evaluate_health_snapshot(
        health_input_from_runtime_snapshot(runtime.health_snapshot()),
        now=now,
        token_stale_after=token_stale_after,
        poll_stale_after=poll_stale_after,
    )


def _is_stale(last_seen: datetime | None, now: datetime, threshold: timedelta) -> bool:
    if last_seen is None:
        return True
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    return now.astimezone(UTC) - last_seen.astimezone(UTC) > threshold


def health_input_from_runtime_snapshot(runtime_snapshot: Mapping[str, Any]) -> HealthEvaluationInput:
    writer_queue = runtime_snapshot.get("writer_queue", {})
    if not isinstance(writer_queue, Mapping):
        writer_queue = {}

    return HealthEvaluationInput(
        scheduler_running=bool(runtime_snapshot.get("scheduler_running", False)),
        writer_queue_running=bool(writer_queue.get("running", False)),
        writer_queue_degraded=bool(writer_queue.get("degraded", False)),
        queue_depth=int(writer_queue.get("queue_depth", 0)),
        last_token_refresh_at=_coerce_datetime(runtime_snapshot.get("last_token_refresh_at")),
        last_poll_success_at=_coerce_datetime(runtime_snapshot.get("last_poll_success_at")),
        consecutive_poll_failures=int(runtime_snapshot.get("consecutive_poll_failures", 0)),
        trading_blocked=bool(runtime_snapshot.get("trading_blocked", False)),
        last_error=_coerce_optional_str(runtime_snapshot.get("last_error")),
        runtime_health_status=_coerce_optional_str(runtime_snapshot.get("health_status")),
        writer_queue_last_error=_coerce_optional_str(writer_queue.get("last_error")),
    )


def evaluate_health_snapshot(
    health_input: HealthEvaluationInput,
    *,
    now: datetime | None = None,
    token_stale_after: timedelta = timedelta(days=1),
    poll_stale_after: timedelta = timedelta(minutes=20),
) -> HealthSnapshot:
    reference_now = now or datetime.now(UTC)
    token_stale = _is_stale(health_input.last_token_refresh_at, reference_now, token_stale_after)
    poll_stale = _is_stale(health_input.last_poll_success_at, reference_now, poll_stale_after)

    status = RuntimeHealthStatus.NORMAL
    if health_input.writer_queue_degraded or health_input.trading_blocked:
        status = RuntimeHealthStatus.CRITICAL
    elif token_stale or poll_stale or bool(health_input.last_error):
        status = RuntimeHealthStatus.WARNING

    return HealthSnapshot(
        status=status,
        trading_blocked=health_input.trading_blocked,
        scheduler_running=health_input.scheduler_running,
        writer_queue_running=health_input.writer_queue_running,
        writer_queue_degraded=health_input.writer_queue_degraded,
        queue_depth=health_input.queue_depth,
        token_stale=token_stale,
        poll_stale=poll_stale,
        last_token_refresh_at=health_input.last_token_refresh_at,
        last_poll_success_at=health_input.last_poll_success_at,
        consecutive_poll_failures=health_input.consecutive_poll_failures,
        last_error=health_input.last_error,
        details={
            "status_source": "external_canonical",
            "runtime_health_status": health_input.runtime_health_status,
            "writer_queue_last_error": health_input.writer_queue_last_error,
        },
    )


def _coerce_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _coerce_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None

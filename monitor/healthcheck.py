from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

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


def build_health_snapshot(
    runtime: TradingRuntime,
    *,
    now: datetime | None = None,
    token_stale_after: timedelta = timedelta(days=1),
    poll_stale_after: timedelta = timedelta(minutes=20),
) -> HealthSnapshot:
    runtime_snapshot = runtime.health_snapshot()
    reference_now = now or datetime.now(UTC)

    last_token_refresh_at = runtime_snapshot.get("last_token_refresh_at")
    last_poll_success_at = runtime_snapshot.get("last_poll_success_at")
    writer_queue = runtime_snapshot.get("writer_queue", {})

    token_stale = _is_stale(last_token_refresh_at, reference_now, token_stale_after)
    poll_stale = _is_stale(last_poll_success_at, reference_now, poll_stale_after)
    writer_queue_degraded = bool(writer_queue.get("degraded", False))
    trading_blocked = bool(runtime_snapshot.get("trading_blocked", False))

    status = RuntimeHealthStatus.NORMAL
    if writer_queue_degraded or trading_blocked:
        status = RuntimeHealthStatus.CRITICAL
    elif token_stale or poll_stale or bool(runtime_snapshot.get("last_error")):
        status = RuntimeHealthStatus.WARNING

    return HealthSnapshot(
        status=status,
        trading_blocked=trading_blocked,
        scheduler_running=bool(runtime_snapshot.get("scheduler_running", False)),
        writer_queue_running=bool(writer_queue.get("running", False)),
        writer_queue_degraded=writer_queue_degraded,
        queue_depth=int(writer_queue.get("queue_depth", 0)),
        token_stale=token_stale,
        poll_stale=poll_stale,
        last_token_refresh_at=last_token_refresh_at,
        last_poll_success_at=last_poll_success_at,
        consecutive_poll_failures=int(runtime_snapshot.get("consecutive_poll_failures", 0)),
        last_error=runtime_snapshot.get("last_error"),
        details={"writer_queue_last_error": writer_queue.get("last_error")},
    )


def _is_stale(last_seen: datetime | None, now: datetime, threshold: timedelta) -> bool:
    if last_seen is None:
        return True
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    return now.astimezone(UTC) - last_seen.astimezone(UTC) > threshold

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from auth.token_manager import AccessToken
from core.exceptions import AuthenticationError, BrokerApiError
from core.models import ExecutionFill
from core.models import ReconciliationResult, ReconciliationStatus, RuntimeHealthStatus, RuntimeState
from core.settings import RuntimeEnv
from data.database import Order, get_read_session, init_db
from execution.fill_processor import FillProcessor
from execution.runtime import (
    BROKER_POLL_JOB_ID,
    HEALTHCHECK_JOB_ID,
    PRE_CLOSE_CANCEL_KR_JOB_ID,
    PRE_CLOSE_CANCEL_US_JOB_ID,
    STRATEGY_CYCLE_KR_DUAL_MOMENTUM_JOB_ID,
    STRATEGY_CYCLE_KR_FACTOR_INVESTING_JOB_ID,
    STRATEGY_CYCLE_KR_TREND_FOLLOWING_JOB_ID,
    TOKEN_REFRESH_JOB_ID,
    TradingRuntime,
    get_market_session_window,
    is_market_session_open,
    is_pre_close_window,
    mark_writer_queue_degraded,
)
from execution.writer_queue import WriterQueue
from monitor.healthcheck import build_health_snapshot
from tests.test_execution.test_bootstrap import build_settings
from execution.order_manager import OrderManager
from core.models import Signal


KST = timezone(timedelta(hours=9))


class RecordingOperationsRecorder:
    def __init__(self) -> None:
        self.logs: list[dict[str, object]] = []

    def record_system_log(
        self,
        payload=None,
        /,
        *,
        level=None,
        module=None,
        message=None,
        extra=None,
        created_at=None,
    ) -> int:
        self.logs.append(
            {
                "level": level,
                "module": module,
                "message": message,
                "extra": dict(extra or {}),
                "created_at": created_at,
            }
        )
        return len(self.logs)


class RecordingTelegramNotifier:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def send_event(
        self,
        event_type,
        message,
        context=None,
        *,
        severity="warning",
        title=None,
        created_at=None,
        source_env=None,
    ):
        self.events.append(
            {
                "event_type": event_type,
                "message": message,
                "context": dict(context or {}),
                "severity": severity,
                "title": title,
                "created_at": created_at,
                "source_env": source_env,
            }
        )
        return None


class QueueHealthStub:
    def __init__(self, *, running: bool = True, degraded: bool = False, queue_depth: int = 0, last_error: str | None = None) -> None:
        self.running = running
        self.degraded = degraded
        self.queue_depth = queue_depth
        self.last_error = last_error


def _mark_strategy_cycle_ready(runtime: TradingRuntime, now: datetime) -> None:
    now_utc = now.astimezone(timezone.utc)
    runtime.state.last_token_refresh_at = now_utc - timedelta(hours=1)
    runtime.state.last_poll_success_at = now_utc - timedelta(minutes=5)


def test_kr_market_session_boundaries() -> None:
    assert is_market_session_open("KR", datetime(2026, 4, 15, 9, 0, tzinfo=KST)) is True
    assert is_market_session_open("KR", datetime(2026, 4, 15, 15, 29, tzinfo=KST)) is True
    assert is_market_session_open("KR", datetime(2026, 4, 15, 8, 59, tzinfo=KST)) is False
    assert is_market_session_open("KR", datetime(2026, 4, 15, 15, 30, tzinfo=KST)) is False


def test_us_market_session_boundaries_cross_midnight() -> None:
    assert is_market_session_open("US", datetime(2026, 4, 15, 23, 30, tzinfo=KST)) is True
    assert is_market_session_open("US", datetime(2026, 4, 16, 5, 59, tzinfo=KST)) is True
    assert is_market_session_open("US", datetime(2026, 4, 15, 23, 29, tzinfo=KST)) is False
    assert is_market_session_open("US", datetime(2026, 4, 16, 6, 0, tzinfo=KST)) is False


def test_pre_close_window_defaults_to_last_five_minutes() -> None:
    assert is_pre_close_window("KR", datetime(2026, 4, 15, 15, 25, tzinfo=KST)) is True
    assert is_pre_close_window("KR", datetime(2026, 4, 15, 15, 24, tzinfo=KST)) is False
    assert is_pre_close_window("US", datetime(2026, 4, 16, 5, 55, tzinfo=KST)) is True
    assert is_pre_close_window("US", datetime(2026, 4, 16, 5, 54, tzinfo=KST)) is False


def test_get_market_session_window_returns_expected_us_session_dates() -> None:
    session_open, session_close = get_market_session_window("US", datetime(2026, 4, 16, 1, 0, tzinfo=KST))

    assert session_open == datetime(2026, 4, 15, 23, 30, tzinfo=KST)
    assert session_close == datetime(2026, 4, 16, 6, 0, tzinfo=KST)


def test_mark_writer_queue_degraded_updates_runtime_state() -> None:
    state = RuntimeState()
    degraded = mark_writer_queue_degraded(state, "writer queue degraded")

    assert degraded.writer_queue_degraded is True
    assert degraded.trading_blocked is True
    assert degraded.health_status == RuntimeHealthStatus.CRITICAL
    assert degraded.last_error == "writer queue degraded"


def test_trading_runtime_registers_expected_jobs(tmp_path) -> None:
    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    runtime = TradingRuntime(writer_queue=writer_queue, settings=settings)

    try:
        runtime.start()
        jobs = {job.id for job in runtime.scheduler.get_jobs()}
    finally:
        runtime.stop()

    assert jobs == {
        TOKEN_REFRESH_JOB_ID,
        BROKER_POLL_JOB_ID,
        PRE_CLOSE_CANCEL_KR_JOB_ID,
        PRE_CLOSE_CANCEL_US_JOB_ID,
        HEALTHCHECK_JOB_ID,
    }


def test_trading_runtime_registers_strategy_cycle_jobs_when_auto_trading_enabled(tmp_path) -> None:
    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["trend_following", "dual_momentum", "factor_investing"]},
    )
    writer_queue = WriterQueue()
    runtime = TradingRuntime(writer_queue=writer_queue, settings=settings)

    try:
        runtime.start()
        jobs = {job.id for job in runtime.scheduler.get_jobs()}
    finally:
        runtime.stop()

    assert {
        STRATEGY_CYCLE_KR_TREND_FOLLOWING_JOB_ID,
        STRATEGY_CYCLE_KR_DUAL_MOMENTUM_JOB_ID,
        STRATEGY_CYCLE_KR_FACTOR_INVESTING_JOB_ID,
    }.issubset(jobs)


def test_trading_runtime_runs_strategy_cycle_runner_when_enabled(tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    writer_queue = WriterQueue()
    now = datetime(2026, 4, 15, 9, 15, tzinfo=KST)
    calls: list[tuple[str, datetime, list[str] | None]] = []
    recorder = RecordingOperationsRecorder()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        settings=settings,
        time_provider=lambda: now,
        strategy_cycle_runner=lambda market, as_of, strategies: calls.append((market, as_of, strategies)),
        operations_recorder=recorder,
    )

    try:
        runtime.start()
        _mark_strategy_cycle_ready(runtime, now)
        runtime._run_strategy_cycle_job("KR", strategies=["trend_following"])
    finally:
        runtime.stop()

    assert calls == [("KR", now, ["trend_following"])]
    assert recorder.logs[-1]["message"] == "auto-trading cycle completed"
    assert recorder.logs[-1]["extra"]["market"] == "KR"
    assert recorder.logs[-1]["extra"]["strategy_name"] == "trend_following"
    assert recorder.logs[-1]["extra"]["strategy_cycle_status"] == "completed"
    assert recorder.logs[-1]["extra"]["strategy_skip_reason"] is None
    assert recorder.logs[-1]["extra"]["factor_input_available"] is None


def test_trading_runtime_registered_strategy_jobs_forward_requested_subset(tmp_path) -> None:
    settings = build_settings(
        tmp_path,
        auto_trading={"enabled": True, "strategies": ["trend_following", "dual_momentum", "factor_investing"]},
    )
    writer_queue = WriterQueue()
    now = datetime(2026, 4, 15, 9, 15, tzinfo=KST)
    calls: list[tuple[str, datetime, list[str] | None]] = []
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        settings=settings,
        time_provider=lambda: now,
        strategy_cycle_runner=lambda market, as_of, strategies: calls.append((market, as_of, strategies)),
    )

    try:
        runtime.start()
        _mark_strategy_cycle_ready(runtime, now)
        runtime.scheduler.get_job(STRATEGY_CYCLE_KR_TREND_FOLLOWING_JOB_ID).func()
        runtime.scheduler.get_job(STRATEGY_CYCLE_KR_DUAL_MOMENTUM_JOB_ID).func()
        runtime.scheduler.get_job(STRATEGY_CYCLE_KR_FACTOR_INVESTING_JOB_ID).func()
    finally:
        runtime.stop()

    assert calls == [
        ("KR", now, ["trend_following"]),
        ("KR", now, ["dual_momentum"]),
        ("KR", now, ["factor_investing"]),
    ]


def test_trading_runtime_records_rejection_reason_summary_in_strategy_cycle_log(tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    writer_queue = WriterQueue()
    now = datetime(2026, 4, 15, 10, 30, tzinfo=KST)
    recorder = RecordingOperationsRecorder()
    result = SimpleNamespace(
        signals_generated=1,
        signals_resolved=1,
        orders_submitted=0,
        order_candidates=[],
        rejected_signals=[
            SimpleNamespace(reason="existing_position_reentry_blocked"),
            SimpleNamespace(reason="existing_position_reentry_blocked"),
            SimpleNamespace(reason="no_position_to_sell"),
        ],
        strategy_diagnostics=[
            {
                "strategy_name": "factor_investing",
                "status": "skipped",
                "skip_reason": "factor_input_unavailable",
                "factor_input_available": False,
            }
        ],
        details={"submitted_order_count": 0, "submitted_notional_krw": 0.0},
    )
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        settings=settings,
        time_provider=lambda: now,
        strategy_cycle_runner=lambda market, as_of, strategies: result,
        operations_recorder=recorder,
    )

    try:
        runtime.start()
        _mark_strategy_cycle_ready(runtime, now)
        runtime._run_strategy_cycle_job("KR", strategies=["factor_investing"])
    finally:
        runtime.stop()

    assert recorder.logs[-1]["message"] == "auto-trading cycle completed"
    assert recorder.logs[-1]["extra"]["strategy_name"] == "factor_investing"
    assert recorder.logs[-1]["extra"]["strategy_cycle_status"] == "skipped"
    assert recorder.logs[-1]["extra"]["strategy_skip_reason"] == "factor_input_unavailable"
    assert recorder.logs[-1]["extra"]["factor_input_available"] is False
    assert recorder.logs[-1]["extra"]["rejection_reason_summary"] == "existing_position_reentry_blocked:2,no_position_to_sell:1"
    assert recorder.logs[-1]["extra"]["strategy_diagnostics"][0]["skip_reason"] == "factor_input_unavailable"


def test_trading_runtime_skips_strategy_cycle_when_market_is_closed_and_logs_reason(tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    writer_queue = WriterQueue()
    now = datetime(2026, 4, 15, 8, 30, tzinfo=KST)
    calls: list[tuple[str, datetime, list[str] | None]] = []
    recorder = RecordingOperationsRecorder()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        settings=settings,
        time_provider=lambda: now,
        strategy_cycle_runner=lambda market, as_of, strategies: calls.append((market, as_of, strategies)),
        operations_recorder=recorder,
    )

    try:
        runtime.start()
        _mark_strategy_cycle_ready(runtime, now)
        runtime._run_strategy_cycle_job("KR", strategies=["trend_following"])
    finally:
        runtime.stop()

    assert calls == []
    assert recorder.logs[-1]["message"] == "auto-trading cycle skipped"
    assert recorder.logs[-1]["extra"]["strategy_name"] == "trend_following"
    assert recorder.logs[-1]["extra"]["strategy_cycle_status"] == "skipped"
    assert recorder.logs[-1]["extra"]["strategy_skip_reason"] == "market_closed"
    assert recorder.logs[-1]["extra"]["factor_input_available"] is None
    assert recorder.logs[-1]["extra"]["reason"] == "market_closed"


def test_trading_runtime_skips_strategy_cycle_when_trading_is_blocked_and_logs_reason(tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    writer_queue = WriterQueue()
    now = datetime(2026, 4, 15, 9, 15, tzinfo=KST)
    calls: list[tuple[str, datetime, list[str] | None]] = []
    recorder = RecordingOperationsRecorder()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        settings=settings,
        time_provider=lambda: now,
        strategy_cycle_runner=lambda market, as_of, strategies: calls.append((market, as_of, strategies)),
        operations_recorder=recorder,
    )

    try:
        runtime.start()
        _mark_strategy_cycle_ready(runtime, now)
        runtime.state.trading_blocked = True
        runtime._run_strategy_cycle_job("KR", strategies=["trend_following"])
    finally:
        runtime.stop()

    assert calls == []
    assert recorder.logs[-1]["message"] == "auto-trading cycle skipped"
    assert recorder.logs[-1]["extra"]["reason"] == "trading_blocked"
    assert recorder.logs[-1]["extra"]["health_status"] == RuntimeHealthStatus.CRITICAL.value


def test_trading_runtime_skips_strategy_cycle_when_polling_is_stale_and_logs_reason(tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    writer_queue = WriterQueue()
    now = datetime(2026, 4, 15, 9, 15, tzinfo=KST)
    calls: list[tuple[str, datetime, list[str] | None]] = []
    recorder = RecordingOperationsRecorder()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        settings=settings,
        time_provider=lambda: now,
        strategy_cycle_runner=lambda market, as_of, strategies: calls.append((market, as_of, strategies)),
        operations_recorder=recorder,
    )

    try:
        runtime.start()
        runtime.state.last_token_refresh_at = now.astimezone(timezone.utc) - timedelta(hours=1)
        runtime.state.last_poll_success_at = now.astimezone(timezone.utc) - timedelta(minutes=30)
        runtime._run_strategy_cycle_job("KR", strategies=["trend_following"])
    finally:
        runtime.stop()

    assert calls == []
    assert recorder.logs[-1]["message"] == "auto-trading cycle skipped"
    assert recorder.logs[-1]["extra"]["reason"] == "polling_stale"
    assert recorder.logs[-1]["extra"]["health_status"] == RuntimeHealthStatus.WARNING.value


def test_trading_runtime_skips_strategy_cycle_when_writer_queue_is_degraded_and_logs_reason(tmp_path, monkeypatch) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    writer_queue = WriterQueue()
    now = datetime(2026, 4, 15, 9, 15, tzinfo=KST)
    calls: list[tuple[str, datetime, list[str] | None]] = []
    recorder = RecordingOperationsRecorder()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        settings=settings,
        time_provider=lambda: now,
        strategy_cycle_runner=lambda market, as_of, strategies: calls.append((market, as_of, strategies)),
        operations_recorder=recorder,
    )

    try:
        runtime.start()
        _mark_strategy_cycle_ready(runtime, now)
        monkeypatch.setattr(
            runtime.writer_queue,
            "health",
            lambda: QueueHealthStub(running=True, degraded=True, queue_depth=0, last_error="queue degraded"),
        )
        runtime._run_strategy_cycle_job("KR", strategies=["trend_following"])
    finally:
        runtime.stop()

    assert calls == []
    assert recorder.logs[-1]["message"] == "auto-trading cycle skipped"
    assert recorder.logs[-1]["extra"]["reason"] == "writer_queue_degraded"
    assert recorder.logs[-1]["extra"]["health_status"] == RuntimeHealthStatus.CRITICAL.value


def test_trading_runtime_skips_strategy_cycle_outside_vts_environment(tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True}).model_copy(update={"env": RuntimeEnv.PROD})
    writer_queue = WriterQueue()
    now = datetime(2026, 4, 15, 9, 15, tzinfo=KST)
    calls: list[tuple[str, datetime, list[str] | None]] = []
    recorder = RecordingOperationsRecorder()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        settings=settings,
        time_provider=lambda: now,
        strategy_cycle_runner=lambda market, as_of, strategies: calls.append((market, as_of, strategies)),
        operations_recorder=recorder,
    )

    try:
        runtime.start()
        _mark_strategy_cycle_ready(runtime, now)
        runtime._run_strategy_cycle_job("KR", strategies=["trend_following"])
    finally:
        runtime.stop()

    assert calls == []
    assert recorder.logs[-1]["message"] == "auto-trading cycle skipped"
    assert recorder.logs[-1]["extra"]["reason"] == "non_vts_environment"


def test_trading_runtime_records_strategy_cycle_failure_without_crashing(tmp_path) -> None:
    settings = build_settings(tmp_path, auto_trading={"enabled": True})
    writer_queue = WriterQueue()
    now = datetime(2026, 4, 15, 9, 15, tzinfo=KST)
    recorder = RecordingOperationsRecorder()

    def failing_runner(market: str, as_of: datetime, strategies: list[str] | None) -> None:
        raise RuntimeError("strategy cycle exploded")

    runtime = TradingRuntime(
        writer_queue=writer_queue,
        settings=settings,
        time_provider=lambda: now,
        strategy_cycle_runner=failing_runner,
        operations_recorder=recorder,
    )
    snapshot_error = None

    try:
        runtime.start()
        _mark_strategy_cycle_ready(runtime, now)
        runtime._run_strategy_cycle_job("KR", strategies=["trend_following"])
        snapshot_error = runtime.state.last_error
    finally:
        runtime.stop()

    assert snapshot_error == "strategy cycle exploded"
    assert recorder.logs[-1]["message"] == "auto-trading cycle failed"
    assert recorder.logs[-1]["extra"]["strategy_name"] == "trend_following"
    assert recorder.logs[-1]["extra"]["strategy_cycle_status"] == "failed"
    assert recorder.logs[-1]["extra"]["strategy_skip_reason"] is None
    assert recorder.logs[-1]["extra"]["factor_input_available"] is None
    assert recorder.logs[-1]["extra"]["error_type"] == "RuntimeError"


def test_trading_runtime_skips_strategy_cycle_runner_when_auto_trading_is_disabled(tmp_path) -> None:
    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    calls: list[tuple[str, datetime, list[str] | None]] = []
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        settings=settings,
        time_provider=lambda: datetime(2026, 4, 15, 9, 15, tzinfo=KST),
        strategy_cycle_runner=lambda market, as_of, strategies: calls.append((market, as_of, strategies)),
    )

    try:
        runtime.start()
        runtime._run_strategy_cycle_job("KR", strategies=["trend_following"])
    finally:
        runtime.stop()

    assert calls == []


def test_trading_runtime_start_and_stop_are_idempotent(tmp_path) -> None:
    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    runtime = TradingRuntime(writer_queue=writer_queue, settings=settings)

    runtime.start()
    runtime.start()
    running_snapshot = runtime.health_snapshot()

    runtime.stop()
    runtime.stop()
    stopped_snapshot = runtime.health_snapshot()

    assert running_snapshot["scheduler_running"] is True
    assert running_snapshot["writer_queue"]["running"] is True
    assert stopped_snapshot["scheduler_running"] is False
    assert stopped_snapshot["writer_queue"]["running"] is False


def test_trading_runtime_runs_startup_token_warmup(tmp_path) -> None:
    class DummyTokenManager:
        def __init__(self) -> None:
            self.calls = 0

        def refresh_token(self, env):
            self.calls += 1
            issued_at = datetime(2026, 4, 15, 8, 0, tzinfo=KST)
            return AccessToken(token="token", issued_at=issued_at, expires_at=issued_at + timedelta(hours=1))

    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    token_manager = DummyTokenManager()
    runtime = TradingRuntime(writer_queue=writer_queue, token_manager=token_manager, settings=settings)

    try:
        runtime.start()
        snapshot = runtime.health_snapshot()
    finally:
        runtime.stop()

    assert token_manager.calls == 1
    assert snapshot["trading_blocked"] is False
    assert snapshot["last_token_refresh_at"] == datetime(2026, 4, 15, 8, 0, tzinfo=KST)


def test_trading_runtime_retries_token_refresh_before_success(tmp_path) -> None:
    class FlakyTokenManager:
        def __init__(self) -> None:
            self.calls = 0

        def refresh_token(self, env):
            self.calls += 1
            if self.calls < 3:
                raise AuthenticationError("temporary token issue")
            issued_at = datetime(2026, 4, 15, 8, 5, tzinfo=KST)
            return AccessToken(token="token", issued_at=issued_at, expires_at=issued_at + timedelta(hours=1))

    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    token_manager = FlakyTokenManager()
    runtime = TradingRuntime(writer_queue=writer_queue, token_manager=token_manager, settings=settings)

    try:
        runtime.start()
        snapshot = runtime.health_snapshot()
    finally:
        runtime.stop()

    assert token_manager.calls == 3
    assert snapshot["trading_blocked"] is False
    assert snapshot["last_token_refresh_at"] == datetime(2026, 4, 15, 8, 5, tzinfo=KST)


def test_trading_runtime_blocks_after_token_refresh_retries_exhausted(tmp_path) -> None:
    class FailingTokenManager:
        def __init__(self) -> None:
            self.calls = 0

        def refresh_token(self, env):
            self.calls += 1
            raise AuthenticationError("token refresh failed")

    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    token_manager = FailingTokenManager()
    notifier = RecordingTelegramNotifier()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        token_manager=token_manager,
        settings=settings,
        telegram_notifier=notifier,
    )

    try:
        runtime.start()
        snapshot = runtime.health_snapshot()
    finally:
        runtime.stop()

    assert token_manager.calls == 3
    assert snapshot["trading_blocked"] is True
    assert snapshot["health_status"] == RuntimeHealthStatus.WARNING.value
    assert snapshot["last_error"] == "token refresh failed"
    assert [event["event_type"] for event in notifier.events] == [
        "token_refresh_failure",
        "trading_blocked",
    ]


def test_trading_runtime_skips_polling_when_market_closed(tmp_path) -> None:
    class DummyTokenManager:
        def __init__(self) -> None:
            self.calls = 0

        def get_valid_token(self, env):
            self.calls += 1
            return "token"

    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    token_manager = DummyTokenManager()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        token_manager=token_manager,
        api_client=object(),
        reconciliation_service=object(),
        settings=settings,
        time_provider=lambda: datetime(2026, 4, 15, 8, 0, tzinfo=KST),
    )

    try:
        runtime.start()
        token_manager.calls = 0
        runtime._run_broker_poll_job()
        snapshot = runtime.health_snapshot()
    finally:
        runtime.stop()

    assert token_manager.calls == 0
    assert snapshot["consecutive_poll_failures"] == 0


def test_trading_runtime_runs_polling_and_records_success(tmp_path) -> None:
    class DummyTokenManager:
        def __init__(self) -> None:
            self.poll_calls = 0

        def refresh_token(self, env):
            issued_at = datetime(2026, 4, 15, 8, 0, tzinfo=KST)
            return AccessToken(token="token", issued_at=issued_at, expires_at=issued_at + timedelta(hours=1))

        def get_valid_token(self, env):
            self.poll_calls += 1
            return "token"

    class DummyApiClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_account_snapshot(self, access_token):
            self.calls.append("account")
            return {"output1": []}

        def list_open_orders(self, access_token):
            self.calls.append("open_orders")
            return {"output": []}

        def get_cash_balance(self, access_token):
            self.calls.append("cash")
            return {"output": {"ord_psbl_cash": "1000"}}

        def build_polling_snapshot(self, **kwargs):
            self.calls.append("build")
            from core.models import BrokerPollingSnapshot

            return BrokerPollingSnapshot(positions=[], open_orders=[], cash_available=1000)

    class DummyReconciliationService:
        def __init__(self) -> None:
            self.snapshots = []

        def reconcile_snapshot(self, snapshot, *, missing_fills=None):
            self.snapshots.append(snapshot)
            return ReconciliationResult(status=ReconciliationStatus.RECONCILED, summary={"mismatch_count": 0})

    class DummyOrderManager:
        def __init__(self) -> None:
            self.started = 0

        def start_scheduled_poll(self):
            self.started += 1

    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    token_manager = DummyTokenManager()
    api_client = DummyApiClient()
    reconciliation_service = DummyReconciliationService()
    order_manager = DummyOrderManager()
    now = datetime(2026, 4, 15, 9, 30, tzinfo=KST)
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        token_manager=token_manager,
        api_client=api_client,
        order_manager=order_manager,
        reconciliation_service=reconciliation_service,
        settings=settings,
        time_provider=lambda: now,
    )

    try:
        runtime.start()
        runtime._run_broker_poll_job()
        snapshot = runtime.health_snapshot()
    finally:
        runtime.stop()

    assert token_manager.poll_calls == 1
    assert order_manager.started == 1
    assert api_client.calls == ["account", "open_orders", "cash", "build"]
    assert len(reconciliation_service.snapshots) == 1
    assert snapshot["last_poll_success_at"] == now
    assert snapshot["consecutive_poll_failures"] == 0


def test_trading_runtime_retries_retryable_poll_query_errors(tmp_path, monkeypatch) -> None:
    class DummyTokenManager:
        def refresh_token(self, env):
            issued_at = datetime(2026, 4, 15, 8, 0, tzinfo=KST)
            return AccessToken(token="token", issued_at=issued_at, expires_at=issued_at + timedelta(hours=1))

        def get_valid_token(self, env):
            return "token"

    class FlakyApiClient:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.open_order_attempts = 0

        def get_account_snapshot(self, access_token):
            self.calls.append("account")
            return {"output1": []}

        def list_open_orders(self, access_token):
            self.calls.append("open_orders")
            self.open_order_attempts += 1
            if self.open_order_attempts == 1:
                raise BrokerApiError("초당 거래건수를 초과하였습니다.", status_code=500)
            return {"output": []}

        def get_cash_balance(self, access_token):
            self.calls.append("cash")
            return {"output": {"ord_psbl_cash": "1000"}}

        def build_polling_snapshot(self, **kwargs):
            self.calls.append("build")
            from core.models import BrokerPollingSnapshot

            return BrokerPollingSnapshot(positions=[], open_orders=[], cash_available=1000)

    class DummyReconciliationService:
        def __init__(self) -> None:
            self.calls = 0

        def reconcile_snapshot(self, snapshot, *, missing_fills=None):
            self.calls += 1
            return ReconciliationResult(status=ReconciliationStatus.RECONCILED, summary={"mismatch_count": 0})

    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    api_client = FlakyApiClient()
    reconciliation_service = DummyReconciliationService()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        token_manager=DummyTokenManager(),
        api_client=api_client,
        reconciliation_service=reconciliation_service,
        settings=settings,
        time_provider=lambda: datetime(2026, 4, 15, 9, 30, tzinfo=KST),
    )
    monkeypatch.setattr("execution.runtime.time_module.sleep", lambda _: None)

    try:
        runtime.start()
        runtime._run_broker_poll_job()
        snapshot = runtime.health_snapshot()
    finally:
        runtime.stop()

    assert api_client.calls == ["account", "open_orders", "open_orders", "cash", "build"]
    assert reconciliation_service.calls == 1
    assert snapshot["consecutive_poll_failures"] == 0
    assert snapshot["last_error"] is None


def test_trading_runtime_absorbs_vts_open_orders_unsupported_response(tmp_path) -> None:
    from tests.test_execution.test_bootstrap import DummySession
    from execution.kis_api import KISApiClient

    class DummyTokenManager:
        def refresh_token(self, env):
            issued_at = datetime(2026, 4, 15, 8, 0, tzinfo=KST)
            return AccessToken(token="token", issued_at=issued_at, expires_at=issued_at + timedelta(hours=1))

        def get_valid_token(self, env):
            return "token"

    class DummyReconciliationService:
        def __init__(self) -> None:
            self.snapshots = []

        def reconcile_snapshot(self, snapshot, *, missing_fills=None):
            self.snapshots.append(snapshot)
            return ReconciliationResult(status=ReconciliationStatus.RECONCILED, summary={"mismatch_count": 0})

    settings = build_settings(tmp_path)
    init_db(settings)
    api_client = KISApiClient(
        settings=settings,
        session=DummySession(
            [
                {"output1": []},
                {
                    "rt_cd": "1",
                    "msg_cd": "OPSQ0001",
                    "msg1": "모의투자에서는 해당업무가 제공되지 않습니다.",
                },
                {"output": {"ord_psbl_cash": "1000"}},
            ]
        ),
    )
    writer_queue = WriterQueue()
    reconciliation_service = DummyReconciliationService()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        token_manager=DummyTokenManager(),
        api_client=api_client,
        reconciliation_service=reconciliation_service,
        settings=settings,
        time_provider=lambda: datetime(2026, 4, 15, 9, 30, tzinfo=KST),
    )

    try:
        runtime.start()
        runtime._run_broker_poll_job()
        snapshot = runtime.health_snapshot()
    finally:
        runtime.stop()

    assert len(reconciliation_service.snapshots) == 1
    assert reconciliation_service.snapshots[0].open_orders == []
    assert snapshot["consecutive_poll_failures"] == 0
    assert snapshot["last_error"] is None


def test_trading_runtime_processes_broker_fills_before_reconciliation(tmp_path) -> None:
    class DummyTokenManager:
        def refresh_token(self, env):
            issued_at = datetime(2026, 4, 15, 8, 0, tzinfo=KST)
            return AccessToken(token="token", issued_at=issued_at, expires_at=issued_at + timedelta(hours=1))

        def get_valid_token(self, env):
            return "token"

    class DummyApiClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_account_snapshot(self, access_token):
            self.calls.append("account")
            return {"output1": []}

        def list_open_orders(self, access_token):
            self.calls.append("open_orders")
            return {"output": []}

        def get_cash_balance(self, access_token):
            self.calls.append("cash")
            return {"output": {"ord_psbl_cash": "1000"}}

        def build_polling_snapshot(self, **kwargs):
            self.calls.append("build")
            from core.models import BrokerPollingSnapshot

            return BrokerPollingSnapshot(positions=[], open_orders=[], cash_available=1000)

    class DummyFillIngestionService:
        def __init__(self, fill: ExecutionFill) -> None:
            self.fill = fill
            self.calls = 0

        def collect_execution_fills(self, access_token, *, market, start_date=None, end_date=None):
            self.calls += 1
            return [self.fill]

    class DummyReconciliationService:
        def __init__(self) -> None:
            self.calls = 0

        def reconcile_snapshot(self, snapshot, *, missing_fills=None):
            self.calls += 1
            return ReconciliationResult(status=ReconciliationStatus.RECONCILED, summary={"mismatch_count": 0})

    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)
    runtime = None

    try:
        signal = Signal(ticker="005930", market="KR", action="buy", strategy="dual_momentum", strength=1.0, reason="entry")
        signal_id = manager.persist_signal(signal)
        intent = manager.create_order_intent(signal, signal_id=signal_id, quantity=1, price=70000)
        submission = manager.persist_validated_order(intent)
        manager.mark_submission_result(submission.order_id, broker_order_no="KR-OPEN-1", broker_order_orgno="06010", accepted=True)

        fill = ExecutionFill(
            order_id=submission.order_id,
            execution_no="KR-SYNC-1",
            fill_seq=1,
            filled_quantity=1,
            filled_price=70000,
            fee=0.0,
            tax=0.0,
            executed_at=datetime(2026, 4, 15, 9, 1, tzinfo=timezone.utc),
        )
        api_client = DummyApiClient()
        fill_service = DummyFillIngestionService(fill)
        reconciliation_service = DummyReconciliationService()
        runtime = TradingRuntime(
            writer_queue=writer_queue,
            token_manager=DummyTokenManager(),
            api_client=api_client,
            order_manager=manager,
            fill_processor=FillProcessor(writer_queue),
            fill_ingestion_service=fill_service,
            reconciliation_service=reconciliation_service,
            settings=settings,
            time_provider=lambda: datetime(2026, 4, 15, 9, 30, tzinfo=KST),
        )

        runtime.start()
        runtime._run_broker_poll_job()
    finally:
        if runtime is not None:
            runtime.stop()
        writer_queue.stop()

    assert fill_service.calls == 1
    assert reconciliation_service.calls == 1
    assert api_client.calls == ["account", "open_orders", "cash", "build"]
    with get_read_session() as session:
        order = session.get(Order, submission.order_id)

    assert order is not None
    assert order.status == "filled"


def test_trading_runtime_blocks_on_polling_mismatch(tmp_path) -> None:
    class DummyTokenManager:
        def refresh_token(self, env):
            issued_at = datetime(2026, 4, 15, 8, 0, tzinfo=KST)
            return AccessToken(token="token", issued_at=issued_at, expires_at=issued_at + timedelta(hours=1))

        def get_valid_token(self, env):
            return "token"

    class DummyApiClient:
        def get_account_snapshot(self, access_token):
            return {"output1": []}

        def list_open_orders(self, access_token):
            return {"output": []}

        def get_cash_balance(self, access_token):
            return {"output": {"ord_psbl_cash": "1000"}}

        def build_polling_snapshot(self, **kwargs):
            from core.models import BrokerPollingSnapshot

            return BrokerPollingSnapshot(positions=[], open_orders=[], cash_available=1000)

    class DummyReconciliationService:
        def reconcile_snapshot(self, snapshot, *, missing_fills=None):
            return ReconciliationResult(status=ReconciliationStatus.MISMATCH_DETECTED, summary={"mismatch_count": 1})

    class DummyOrderManager:
        def __init__(self) -> None:
            self.holds = 0

        def start_scheduled_poll(self):
            return None

        def flag_reconciliation_hold(self, ticker, summary=None):
            self.holds += 1

    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    order_manager = DummyOrderManager()
    notifier = RecordingTelegramNotifier()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        token_manager=DummyTokenManager(),
        api_client=DummyApiClient(),
        order_manager=order_manager,
        reconciliation_service=DummyReconciliationService(),
        settings=settings,
        time_provider=lambda: datetime(2026, 4, 15, 9, 30, tzinfo=KST),
        telegram_notifier=notifier,
    )

    try:
        runtime.start()
        runtime._run_broker_poll_job()
        snapshot = runtime.health_snapshot()
    finally:
        runtime.stop()

    assert order_manager.holds == 1
    assert snapshot["trading_blocked"] is True
    assert snapshot["last_error"] == "polling_mismatch_detected"
    assert [event["event_type"] for event in notifier.events] == [
        "polling_mismatch",
        "trading_blocked",
    ]


def test_trading_runtime_notifies_writer_queue_degraded_once(tmp_path, monkeypatch) -> None:
    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    notifier = RecordingTelegramNotifier()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        settings=settings,
        telegram_notifier=notifier,
    )

    try:
        runtime.start()
        monkeypatch.setattr(
            runtime.writer_queue,
            "health",
            lambda: QueueHealthStub(running=True, degraded=True, queue_depth=0, last_error="queue degraded"),
        )
        runtime._refresh_health_status()
        runtime._refresh_health_status()
    finally:
        runtime.stop()

    assert [event["event_type"] for event in notifier.events] == ["writer_queue_degraded"]


def test_trading_runtime_blocks_after_three_poll_failures(tmp_path) -> None:
    class DummyTokenManager:
        def refresh_token(self, env):
            issued_at = datetime(2026, 4, 15, 8, 0, tzinfo=KST)
            return AccessToken(token="token", issued_at=issued_at, expires_at=issued_at + timedelta(hours=1))

        def get_valid_token(self, env):
            return "token"

    class FailingApiClient:
        def build_polling_snapshot(self, **kwargs):
            raise AssertionError("should not build snapshot on account API failure")

        def get_account_snapshot(self, access_token):
            raise RuntimeError("poll failed")

    class DummyReconciliationService:
        def reconcile_snapshot(self, snapshot, *, missing_fills=None):
            raise AssertionError("should not reconcile on API failure")

    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        token_manager=DummyTokenManager(),
        api_client=FailingApiClient(),
        reconciliation_service=DummyReconciliationService(),
        settings=settings,
        time_provider=lambda: datetime(2026, 4, 15, 9, 30, tzinfo=KST),
    )

    try:
        runtime.start()
        runtime._run_broker_poll_job()
        runtime._run_broker_poll_job()
        runtime._run_broker_poll_job()
        snapshot = runtime.health_snapshot()
    finally:
        runtime.stop()

    assert snapshot["consecutive_poll_failures"] == 3
    assert snapshot["trading_blocked"] is True
    assert snapshot["last_error"] == "poll failed"


def test_trading_runtime_skips_pre_close_cancel_outside_window(tmp_path) -> None:
    class DummyTokenManager:
        def __init__(self) -> None:
            self.calls = 0

        def refresh_token(self, env):
            issued_at = datetime(2026, 4, 15, 8, 0, tzinfo=KST)
            return AccessToken(token="token", issued_at=issued_at, expires_at=issued_at + timedelta(hours=1))

        def get_valid_token(self, env):
            self.calls += 1
            return "token"

    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        token_manager=DummyTokenManager(),
        api_client=object(),
        order_manager=object(),
        settings=settings,
        time_provider=lambda: datetime(2026, 4, 15, 15, 0, tzinfo=KST),
    )

    try:
        runtime.start()
        runtime._run_pre_close_cancel_job("KR")
        snapshot = runtime.health_snapshot()
    finally:
        runtime.stop()

    assert snapshot["last_error"] is None


def test_trading_runtime_cancels_only_open_orders_in_pre_close_window(tmp_path) -> None:
    class DummyTokenManager:
        def refresh_token(self, env):
            issued_at = datetime(2026, 4, 15, 8, 0, tzinfo=KST)
            return AccessToken(token="token", issued_at=issued_at, expires_at=issued_at + timedelta(hours=1))

        def get_valid_token(self, env):
            return "token"

    class DummyApiClient:
        def __init__(self) -> None:
            self.cancelled_orders: list[dict] = []

        def cancel_order(self, payload, access_token=None):
            self.cancelled_orders.append(payload)
            return {"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "ok", "output": {"ODNO": payload["order_no"]}}

        def normalize_cancel_result(self, payload):
            from core.models import BrokerOrderResult

            return BrokerOrderResult(accepted=True, broker_order_no=payload["output"]["ODNO"], raw_payload=payload)

    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    api_client = DummyApiClient()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)
    runtime = None

    try:
        submitted_signal = Signal(ticker="005930", market="KR", action="buy", strategy="dual_momentum", strength=1.0, reason="entry")
        submitted_signal_id = manager.persist_signal(submitted_signal)
        submitted_intent = manager.create_order_intent(submitted_signal, signal_id=submitted_signal_id, quantity=1, price=70000)
        submitted_order = manager.persist_validated_order(submitted_intent)
        manager.mark_submission_result(
            submitted_order.order_id,
            broker_order_no="KR-OPEN-1",
            broker_order_orgno="06010",
            accepted=True,
        )

        partial_signal = Signal(ticker="000660", market="KR", action="buy", strategy="dual_momentum", strength=1.0, reason="entry")
        partial_signal_id = manager.persist_signal(partial_signal)
        partial_intent = manager.create_order_intent(partial_signal, signal_id=partial_signal_id, quantity=1, price=120000)
        partial_order = manager.persist_validated_order(partial_intent)
        manager.mark_submission_result(
            partial_order.order_id,
            broker_order_no="KR-OPEN-2",
            broker_order_orgno="06011",
            accepted=True,
        )
        future = writer_queue.submit(
            lambda session: setattr(session.get(Order, partial_order.order_id), "status", "partially_filled"),
            description="mark partial",
        )
        future.result()

        filled_signal = Signal(ticker="035420", market="KR", action="buy", strategy="dual_momentum", strength=1.0, reason="entry")
        filled_signal_id = manager.persist_signal(filled_signal)
        filled_intent = manager.create_order_intent(filled_signal, signal_id=filled_signal_id, quantity=1, price=200000)
        filled_order = manager.persist_validated_order(filled_intent)
        manager.mark_submission_result(filled_order.order_id, broker_order_no="KR-CLOSED-1", accepted=True)
        future = writer_queue.submit(
            lambda session: setattr(session.get(Order, filled_order.order_id), "status", "filled"),
            description="mark filled",
        )
        future.result()

        runtime = TradingRuntime(
            writer_queue=writer_queue,
            token_manager=DummyTokenManager(),
            api_client=api_client,
            order_manager=manager,
            settings=settings,
            time_provider=lambda: datetime(2026, 4, 15, 15, 25, tzinfo=KST),
        )

        runtime.start()
        runtime._run_pre_close_cancel_job("KR")
        snapshot = runtime.health_snapshot()
    finally:
        if runtime is not None:
            runtime.stop()
        writer_queue.stop()

    assert [payload["order_no"] for payload in api_client.cancelled_orders] == ["KR-OPEN-1", "KR-OPEN-2"]
    assert [payload["order_orgno"] for payload in api_client.cancelled_orders] == ["06010", "06011"]
    with get_read_session() as session:
        submitted_row = session.get(Order, submitted_order.order_id)
        partial_row = session.get(Order, partial_order.order_id)
        filled_row = session.get(Order, filled_order.order_id)

    assert submitted_row is not None and submitted_row.status == "cancelled"
    assert partial_row is not None and partial_row.status == "cancelled"
    assert filled_row is not None and filled_row.status == "filled"
    assert snapshot["last_error"] is None


def test_trading_runtime_retries_retryable_pre_close_cancel_error(tmp_path, monkeypatch) -> None:
    class DummyTokenManager:
        def refresh_token(self, env):
            issued_at = datetime(2026, 4, 15, 8, 0, tzinfo=KST)
            return AccessToken(token="token", issued_at=issued_at, expires_at=issued_at + timedelta(hours=1))

        def get_valid_token(self, env):
            return "token"

    class FlakyCancelApiClient:
        def __init__(self) -> None:
            self.cancel_attempts = 0
            self.cancelled_orders: list[dict] = []

        def cancel_order(self, payload, access_token=None):
            self.cancel_attempts += 1
            self.cancelled_orders.append(payload)
            if self.cancel_attempts == 1:
                raise BrokerApiError("초당 거래건수를 초과하였습니다.", status_code=500)
            return {"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "ok", "output": {"ODNO": payload["order_no"]}}

        def normalize_cancel_result(self, payload):
            from core.models import BrokerOrderResult

            return BrokerOrderResult(accepted=True, broker_order_no=payload["output"]["ODNO"], raw_payload=payload)

    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    api_client = FlakyCancelApiClient()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)
    runtime = None
    monkeypatch.setattr("execution.runtime.time_module.sleep", lambda _: None)

    try:
        signal = Signal(ticker="005930", market="KR", action="buy", strategy="dual_momentum", strength=1.0, reason="entry")
        signal_id = manager.persist_signal(signal)
        intent = manager.create_order_intent(signal, signal_id=signal_id, quantity=1, price=70000)
        submission = manager.persist_validated_order(intent)
        manager.mark_submission_result(
            submission.order_id,
            broker_order_no="KR-OPEN-1",
            broker_order_orgno="06010",
            accepted=True,
        )

        runtime = TradingRuntime(
            writer_queue=writer_queue,
            token_manager=DummyTokenManager(),
            api_client=api_client,
            order_manager=manager,
            settings=settings,
            time_provider=lambda: datetime(2026, 4, 15, 15, 25, tzinfo=KST),
        )

        runtime.start()
        runtime._run_pre_close_cancel_job("KR")
        snapshot = runtime.health_snapshot()
    finally:
        if runtime is not None:
            runtime.stop()
        writer_queue.stop()

    assert api_client.cancel_attempts == 2
    assert [payload["order_no"] for payload in api_client.cancelled_orders] == ["KR-OPEN-1", "KR-OPEN-1"]
    with get_read_session() as session:
        order = session.get(Order, submission.order_id)

    assert order is not None
    assert order.status == "cancelled"
    assert snapshot["last_error"] is None


def test_trading_runtime_reports_missing_kr_cancel_metadata(tmp_path) -> None:
    class DummyTokenManager:
        def get_valid_token(self, env):
            return "token"

    class DummyApiClient:
        def __init__(self) -> None:
            self.cancel_calls = 0

        def cancel_order(self, payload, access_token=None):
            self.cancel_calls += 1
            return {"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "ok", "output": {"ODNO": payload["order_no"]}}

        def normalize_cancel_result(self, payload):
            from core.models import BrokerOrderResult

            return BrokerOrderResult(accepted=True, broker_order_no=payload["output"]["ODNO"], raw_payload=payload)

    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    api_client = DummyApiClient()
    manager = OrderManager(writer_queue=writer_queue, settings=settings)
    runtime = None

    try:
        signal = Signal(ticker="005930", market="KR", action="buy", strategy="dual_momentum", strength=1.0, reason="entry")
        signal_id = manager.persist_signal(signal)
        intent = manager.create_order_intent(signal, signal_id=signal_id, quantity=1, price=70000)
        submission = manager.persist_validated_order(intent)
        manager.mark_submission_result(submission.order_id, broker_order_no="KR-OPEN-1", accepted=True)

        runtime = TradingRuntime(
            writer_queue=writer_queue,
            token_manager=DummyTokenManager(),
            api_client=api_client,
            order_manager=manager,
            settings=settings,
            time_provider=lambda: datetime(2026, 4, 15, 15, 25, tzinfo=KST),
        )

        runtime.start()
        runtime._run_pre_close_cancel_job("KR")
        snapshot = runtime.health_snapshot()
    finally:
        if runtime is not None:
            runtime.stop()
        writer_queue.stop()

    assert api_client.cancel_calls == 0
    assert snapshot["last_error"] == "pre_close_cancel_missing_order_orgno:1"
    with get_read_session() as session:
        order = session.get(Order, submission.order_id)

    assert order is not None
    assert order.status == "submitted"


def test_healthcheck_reports_normal_when_runtime_is_healthy(tmp_path) -> None:
    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    now = datetime(2026, 4, 15, 9, 30, tzinfo=timezone.utc)
    runtime = TradingRuntime(writer_queue=writer_queue, settings=settings, time_provider=lambda: datetime(2026, 4, 15, 18, 30, tzinfo=KST))

    try:
        runtime.start()
        runtime.state.last_token_refresh_at = now - timedelta(hours=1)
        runtime.state.last_poll_success_at = now - timedelta(minutes=5)
        snapshot = build_health_snapshot(runtime, now=now)
    finally:
        runtime.stop()

    assert snapshot.status == RuntimeHealthStatus.NORMAL
    assert snapshot.token_stale is False
    assert snapshot.poll_stale is False


def test_healthcheck_reports_warning_for_stale_polling(tmp_path) -> None:
    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    now = datetime(2026, 4, 15, 9, 30, tzinfo=timezone.utc)
    runtime = TradingRuntime(writer_queue=writer_queue, settings=settings)

    try:
        runtime.start()
        runtime.state.last_token_refresh_at = now - timedelta(hours=1)
        runtime.state.last_poll_success_at = now - timedelta(minutes=30)
        snapshot = build_health_snapshot(runtime, now=now)
    finally:
        runtime.stop()

    assert snapshot.status == RuntimeHealthStatus.WARNING
    assert snapshot.poll_stale is True


def test_healthcheck_reports_critical_for_blocked_runtime(tmp_path) -> None:
    settings = build_settings(tmp_path)
    writer_queue = WriterQueue()
    now = datetime(2026, 4, 15, 9, 30, tzinfo=timezone.utc)
    runtime = TradingRuntime(writer_queue=writer_queue, settings=settings)

    try:
        runtime.start()
        runtime.state.trading_blocked = True
        runtime.state.last_error = "polling_mismatch_detected"
        snapshot = build_health_snapshot(runtime, now=now)
    finally:
        runtime.stop()

    assert snapshot.status == RuntimeHealthStatus.CRITICAL
    assert snapshot.trading_blocked is True

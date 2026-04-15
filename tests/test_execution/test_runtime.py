from __future__ import annotations

from datetime import datetime, timedelta, timezone

from auth.token_manager import AccessToken
from core.exceptions import AuthenticationError
from core.models import ReconciliationResult, ReconciliationStatus, RuntimeHealthStatus, RuntimeState
from data.database import Order, get_read_session, init_db
from execution.runtime import (
    BROKER_POLL_JOB_ID,
    HEALTHCHECK_JOB_ID,
    PRE_CLOSE_CANCEL_KR_JOB_ID,
    PRE_CLOSE_CANCEL_US_JOB_ID,
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
    runtime = TradingRuntime(writer_queue=writer_queue, token_manager=token_manager, settings=settings)

    try:
        runtime.start()
        snapshot = runtime.health_snapshot()
    finally:
        runtime.stop()

    assert token_manager.calls == 3
    assert snapshot["trading_blocked"] is True
    assert snapshot["health_status"] == RuntimeHealthStatus.WARNING.value
    assert snapshot["last_error"] == "token refresh failed"


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
    runtime = TradingRuntime(
        writer_queue=writer_queue,
        token_manager=DummyTokenManager(),
        api_client=DummyApiClient(),
        order_manager=order_manager,
        reconciliation_service=DummyReconciliationService(),
        settings=settings,
        time_provider=lambda: datetime(2026, 4, 15, 9, 30, tzinfo=KST),
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
            self.cancelled_orders: list[str] = []

        def cancel_order(self, payload, access_token=None):
            self.cancelled_orders.append(payload["order_no"])
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
        manager.mark_submission_result(submitted_order.order_id, broker_order_no="KR-OPEN-1", accepted=True)

        partial_signal = Signal(ticker="000660", market="KR", action="buy", strategy="dual_momentum", strength=1.0, reason="entry")
        partial_signal_id = manager.persist_signal(partial_signal)
        partial_intent = manager.create_order_intent(partial_signal, signal_id=partial_signal_id, quantity=1, price=120000)
        partial_order = manager.persist_validated_order(partial_intent)
        manager.mark_submission_result(partial_order.order_id, broker_order_no="KR-OPEN-2", accepted=True)
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

    assert api_client.cancelled_orders == ["KR-OPEN-1", "KR-OPEN-2"]
    with get_read_session() as session:
        submitted_row = session.get(Order, submitted_order.order_id)
        partial_row = session.get(Order, partial_order.order_id)
        filled_row = session.get(Order, filled_order.order_id)

    assert submitted_row is not None and submitted_row.status == "cancelled"
    assert partial_row is not None and partial_row.status == "cancelled"
    assert filled_row is not None and filled_row.status == "filled"
    assert snapshot["last_error"] is None


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

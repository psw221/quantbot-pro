from __future__ import annotations

import threading
import time as time_module
from dataclasses import asdict, replace
from datetime import datetime, time, timedelta, timezone
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from auth.token_manager import TokenManager
from core.exceptions import AuthenticationError
from core.models import ReconciliationStatus, RuntimeHealthStatus, RuntimeState
from core.settings import Settings, get_settings
from data.database import Order, get_read_session
from execution.kis_api import KISApiClient
from execution.order_manager import OrderManager
from execution.reconciliation import ReconciliationService
from execution.writer_queue import WriterQueue


KST = timezone(timedelta(hours=9))

TOKEN_REFRESH_JOB_ID = "token_refresh"
BROKER_POLL_JOB_ID = "broker_poll"
PRE_CLOSE_CANCEL_KR_JOB_ID = "pre_close_cancel_kr"
PRE_CLOSE_CANCEL_US_JOB_ID = "pre_close_cancel_us"
HEALTHCHECK_JOB_ID = "healthcheck"
TOKEN_REFRESH_MAX_ATTEMPTS = 3


def to_kst(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=KST)
    return now.astimezone(KST)


def get_market_session_window(market: str, now_kst: datetime) -> tuple[datetime, datetime]:
    now = to_kst(now_kst)
    market_code = market.upper()

    if market_code == "KR":
        session_open = datetime.combine(now.date(), time(hour=9, minute=0), tzinfo=KST)
        session_close = datetime.combine(now.date(), time(hour=15, minute=30), tzinfo=KST)
        return session_open, session_close

    if market_code == "US":
        if now.timetz() < time(hour=6, minute=0, tzinfo=KST):
            session_open_date = now.date() - timedelta(days=1)
            session_close_date = now.date()
        else:
            session_open_date = now.date()
            session_close_date = now.date() + timedelta(days=1)
        session_open = datetime.combine(session_open_date, time(hour=23, minute=30), tzinfo=KST)
        session_close = datetime.combine(session_close_date, time(hour=6, minute=0), tzinfo=KST)
        return session_open, session_close

    raise ValueError(f"unsupported market: {market}")


def is_market_session_open(market: str, now_kst: datetime) -> bool:
    session_open, session_close = get_market_session_window(market, now_kst)
    now = to_kst(now_kst)
    return session_open <= now < session_close


def is_pre_close_window(market: str, now_kst: datetime, minutes_before_close: int = 5) -> bool:
    if minutes_before_close <= 0:
        raise ValueError("minutes_before_close must be positive")
    session_open, session_close = get_market_session_window(market, now_kst)
    now = to_kst(now_kst)
    pre_close_start = session_close - timedelta(minutes=minutes_before_close)
    return session_open <= now < session_close and pre_close_start <= now


def mark_writer_queue_degraded(state: RuntimeState, last_error: str | None = None) -> RuntimeState:
    return replace(
        state,
        writer_queue_degraded=True,
        trading_blocked=True,
        health_status=RuntimeHealthStatus.CRITICAL,
        last_error=last_error or state.last_error,
    )


class TradingRuntime:
    def __init__(
        self,
        *,
        writer_queue: WriterQueue,
        token_manager: TokenManager | None = None,
        api_client: KISApiClient | None = None,
        order_manager: OrderManager | None = None,
        reconciliation_service: ReconciliationService | None = None,
        settings: Settings | None = None,
        scheduler: BackgroundScheduler | None = None,
        time_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.writer_queue = writer_queue
        self.token_manager = token_manager
        self.api_client = api_client or (getattr(token_manager, "api_client", None) if token_manager is not None else None)
        self.order_manager = order_manager
        self.reconciliation_service = reconciliation_service
        self.scheduler = scheduler or BackgroundScheduler(timezone=KST)
        self.time_provider = time_provider or (lambda: datetime.now(KST))
        self.state = RuntimeState()
        self._jobs_registered = False
        self._keep_running = threading.Event()

    def start(self) -> None:
        if not self.writer_queue.health().running:
            self.writer_queue.start()

        if not self._jobs_registered:
            self._register_jobs()
            self._jobs_registered = True

        if not self.scheduler.running:
            self.scheduler.start()

        self._keep_running.set()
        self.state.scheduler_running = True
        self._run_token_refresh_job()
        self._refresh_health_status()

    def stop(self) -> None:
        self._keep_running.clear()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.writer_queue.stop()
        self.state.scheduler_running = False
        self._refresh_health_status()

    def run_forever(self) -> None:
        self.start()
        try:
            while self._keep_running.is_set():
                time_module.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def health_snapshot(self) -> dict[str, object]:
        self._refresh_health_status()
        queue_health = self.writer_queue.health()
        snapshot = asdict(self.state)
        snapshot["health_status"] = self.state.health_status.value
        snapshot["writer_queue"] = {
            "running": queue_health.running,
            "degraded": queue_health.degraded,
            "queue_depth": queue_health.queue_depth,
            "last_error": queue_health.last_error,
        }
        return snapshot

    def _register_jobs(self) -> None:
        self.scheduler.add_job(
            self._run_token_refresh_job,
            trigger=CronTrigger(hour=self.settings.kis.token_refresh_hour_kst, minute=0, timezone=KST),
            id=TOKEN_REFRESH_JOB_ID,
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._run_broker_poll_job,
            trigger=IntervalTrigger(minutes=self.settings.rebalancing.broker_poll_interval_min, timezone=KST),
            id=BROKER_POLL_JOB_ID,
            replace_existing=True,
        )
        self.scheduler.add_job(
            lambda: self._run_pre_close_cancel_job("KR"),
            trigger=CronTrigger(hour=15, minute=25, timezone=KST),
            id=PRE_CLOSE_CANCEL_KR_JOB_ID,
            replace_existing=True,
        )
        self.scheduler.add_job(
            lambda: self._run_pre_close_cancel_job("US"),
            trigger=CronTrigger(hour=5, minute=55, timezone=KST),
            id=PRE_CLOSE_CANCEL_US_JOB_ID,
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._run_healthcheck_job,
            trigger=IntervalTrigger(minutes=1, timezone=KST),
            id=HEALTHCHECK_JOB_ID,
            replace_existing=True,
        )

    def _run_token_refresh_job(self) -> None:
        if self.token_manager is None:
            self._refresh_health_status()
            return

        last_error: str | None = None
        for _ in range(TOKEN_REFRESH_MAX_ATTEMPTS):
            try:
                token = self.token_manager.refresh_token(self.settings.env)
                self.state = replace(
                    self.state,
                    trading_blocked=False,
                    last_token_refresh_at=token.issued_at,
                    last_error=None,
                )
                self._refresh_health_status()
                return
            except AuthenticationError as exc:
                last_error = str(exc) or "token_refresh_failed"
            except Exception as exc:
                last_error = str(exc) or "token_refresh_failed"

        self.state = replace(
            self.state,
            trading_blocked=True,
            last_error=last_error or "token_refresh_failed",
        )
        self._refresh_health_status()

    def _run_broker_poll_job(self) -> None:
        active_market = self._get_active_market()
        if active_market is None:
            self._refresh_health_status()
            return

        if self.token_manager is None or self.api_client is None or self.reconciliation_service is None:
            self.state = replace(
                self.state,
                last_error="polling_dependencies_not_configured",
            )
            self._refresh_health_status()
            return

        try:
            if self.order_manager is not None:
                self.order_manager.start_scheduled_poll()

            access_token = self.token_manager.get_valid_token(self.settings.env)
            snapshot = self.api_client.build_polling_snapshot(
                account_payload=self.api_client.get_account_snapshot(access_token),
                open_orders_payload=self.api_client.list_open_orders(access_token),
                cash_payload=self.api_client.get_cash_balance(access_token),
                default_market=active_market,
                default_currency="KRW" if active_market == "KR" else "USD",
            )
            result = self.reconciliation_service.reconcile_snapshot(snapshot)
            if result.status == ReconciliationStatus.MISMATCH_DETECTED:
                if self.order_manager is not None:
                    self.order_manager.flag_reconciliation_hold(
                        None,
                        summary=result.summary,
                    )
                self.state = replace(
                    self.state,
                    trading_blocked=True,
                    consecutive_poll_failures=0,
                    last_error="polling_mismatch_detected",
                )
            else:
                self.state = replace(
                    self.state,
                    last_poll_success_at=self.time_provider(),
                    consecutive_poll_failures=0,
                    last_error=None if not self.state.trading_blocked else self.state.last_error,
                )
        except Exception as exc:
            failure_count = self.state.consecutive_poll_failures + 1
            self.state = replace(
                self.state,
                consecutive_poll_failures=failure_count,
                trading_blocked=self.state.trading_blocked or failure_count >= 3,
                last_error=str(exc) or "broker_poll_failed",
            )
        self._refresh_health_status()

    def _run_pre_close_cancel_job(self, market: str) -> None:
        if not is_pre_close_window(market, self.time_provider()):
            self._refresh_health_status()
            return

        if self.token_manager is None or self.api_client is None or self.order_manager is None:
            self.state = replace(
                self.state,
                last_error="cancel_dependencies_not_configured",
            )
            self._refresh_health_status()
            return

        try:
            access_token = self.token_manager.get_valid_token(self.settings.env)
            cancelled_count = 0
            with get_read_session() as session:
                candidates = list(
                    session.query(Order)
                    .filter(
                        Order.market == market,
                        Order.status.in_(["submitted", "partially_filled"]),
                    )
                    .order_by(Order.id)
                    .all()
                )

            for order in candidates:
                if not order.kis_order_no:
                    continue
                result = self.api_client.normalize_cancel_result(
                    self.api_client.cancel_order(
                        {
                            "order_no": order.kis_order_no,
                            "ticker": order.ticker,
                            "market": order.market,
                        },
                        access_token=access_token,
                    )
                )
                if result.accepted:
                    self.order_manager.request_cancel(order.id)
                    self.order_manager.confirm_cancel(order.id)
                    cancelled_count += 1

            self.state = replace(
                self.state,
                last_error=None if cancelled_count or not self.state.trading_blocked else self.state.last_error,
            )
        except Exception as exc:
            self.state = replace(
                self.state,
                last_error=str(exc) or "pre_close_cancel_failed",
            )
        self._refresh_health_status()

    def _run_healthcheck_job(self) -> None:
        self._refresh_health_status()

    def _refresh_health_status(self) -> None:
        queue_health = self.writer_queue.health()
        if queue_health.degraded:
            self.state = mark_writer_queue_degraded(self.state, queue_health.last_error)
            return

        health_status = RuntimeHealthStatus.WARNING if self.state.trading_blocked else RuntimeHealthStatus.NORMAL
        self.state = replace(
            self.state,
            writer_queue_degraded=False,
            health_status=health_status,
            last_error=self.state.last_error if self.state.trading_blocked else None,
        )

    def _get_active_market(self) -> str | None:
        now_kst = self.time_provider()
        if is_market_session_open("KR", now_kst):
            return "KR"
        if is_market_session_open("US", now_kst):
            return "US"
        return None

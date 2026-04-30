from __future__ import annotations

import threading
import time as time_module
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, time, timedelta, timezone
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from auth.token_manager import TokenManager
from core.exceptions import AuthenticationError, BrokerApiError
from core.models import ReconciliationStatus, RuntimeHealthStatus, RuntimeState
from core.settings import RuntimeEnv, Settings, get_settings
from data.database import Order, get_read_session
from execution.fill_ingestion import BrokerFillIngestionService
from execution.fill_processor import FillProcessor
from execution.kis_api import KISApiClient
from execution.order_manager import OrderManager
from execution.reconciliation import ReconciliationService
from execution.writer_queue import WriterQueue
from monitor.operations import OperationsRecorder
from monitor.telegram_bot import TelegramNotifier


KST = timezone(timedelta(hours=9))

TOKEN_REFRESH_JOB_ID = "token_refresh"
BROKER_POLL_JOB_ID = "broker_poll"
PRE_CLOSE_CANCEL_KR_JOB_ID = "pre_close_cancel_kr"
PRE_CLOSE_CANCEL_US_JOB_ID = "pre_close_cancel_us"
HEALTHCHECK_JOB_ID = "healthcheck"
STRATEGY_CYCLE_KR_INTRADAY_MOMENTUM_JOB_ID = "strategy_cycle_kr_intraday_momentum"
STRATEGY_CYCLE_KR_TREND_FOLLOWING_JOB_ID = "strategy_cycle_kr_trend_following"
STRATEGY_CYCLE_KR_FACTOR_INVESTING_JOB_ID = "strategy_cycle_kr_factor_investing"
KR_STRATEGY_CYCLE_JOB_IDS = {
    "intraday_momentum": STRATEGY_CYCLE_KR_INTRADAY_MOMENTUM_JOB_ID,
    "trend_following": STRATEGY_CYCLE_KR_TREND_FOLLOWING_JOB_ID,
    "factor_investing": STRATEGY_CYCLE_KR_FACTOR_INVESTING_JOB_ID,
}
TOKEN_REFRESH_MAX_ATTEMPTS = 3
BROKER_POLL_QUERY_MAX_ATTEMPTS = 3
BROKER_POLL_RETRY_DELAY_SEC = 1.0
PRE_CLOSE_CANCEL_MAX_ATTEMPTS = 3
PRE_CLOSE_CANCEL_RETRY_DELAY_SEC = 1.0
AUTO_TRADING_LOG_MODULE = "execution.runtime"
StrategyCycleRunner = Callable[[str, datetime, list[str] | None], object]


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
        fill_processor: FillProcessor | None = None,
        fill_ingestion_service: BrokerFillIngestionService | None = None,
        reconciliation_service: ReconciliationService | None = None,
        operations_recorder: OperationsRecorder | None = None,
        telegram_notifier: TelegramNotifier | None = None,
        settings: Settings | None = None,
        scheduler: BackgroundScheduler | None = None,
        time_provider: Callable[[], datetime] | None = None,
        strategy_cycle_runner: StrategyCycleRunner | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.writer_queue = writer_queue
        self.token_manager = token_manager
        self.api_client = api_client or (getattr(token_manager, "api_client", None) if token_manager is not None else None)
        self.order_manager = order_manager
        can_auto_ingest_fills = self.api_client is not None and all(
            hasattr(self.api_client, attribute)
            for attribute in ("list_daily_order_fills", "normalize_daily_order_fills")
        )
        self.fill_processor = fill_processor or (FillProcessor(writer_queue) if can_auto_ingest_fills else None)
        self.fill_ingestion_service = fill_ingestion_service or (
            BrokerFillIngestionService(api_client=self.api_client, settings=self.settings) if can_auto_ingest_fills else None
        )
        self.reconciliation_service = reconciliation_service
        self.operations_recorder = operations_recorder or OperationsRecorder(writer_queue)
        self.telegram_notifier = telegram_notifier
        self.scheduler = scheduler or BackgroundScheduler(timezone=KST)
        self.time_provider = time_provider or (lambda: datetime.now(KST))
        self.strategy_cycle_runner = strategy_cycle_runner
        self.state = RuntimeState()
        self._jobs_registered = False
        self._keep_running = threading.Event()
        self._active_notification_keys: set[str] = set()

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
        configured_markets = self._configured_auto_trading_markets()
        if "KR" in configured_markets:
            self.scheduler.add_job(
                lambda: self._run_pre_close_cancel_job("KR"),
                trigger=CronTrigger(hour=15, minute=25, timezone=KST),
                id=PRE_CLOSE_CANCEL_KR_JOB_ID,
                replace_existing=True,
            )
        if "US" in configured_markets:
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
        self._register_strategy_cycle_jobs()

    def _register_strategy_cycle_jobs(self) -> None:
        if not self.settings.auto_trading.enabled:
            return

        if "KR" in self.settings.auto_trading.markets:
            for strategy_name in self.settings.auto_trading.strategies:
                self.scheduler.add_job(
                    lambda strategy_name=strategy_name: self._run_strategy_cycle_job("KR", strategies=[strategy_name]),
                    trigger=CronTrigger.from_crontab(
                        self.settings.auto_trading.kr.resolve_schedule_cron(strategy_name),
                        timezone=KST,
                    ),
                    id=KR_STRATEGY_CYCLE_JOB_IDS[strategy_name],
                    replace_existing=True,
                )

    def _run_strategy_cycle_job(self, market: str, *, strategies: list[str] | None = None) -> None:
        if not self.settings.auto_trading.enabled:
            return
        if self.strategy_cycle_runner is None:
            return
        as_of = self.time_provider()
        skip_reason, skip_details = self._evaluate_strategy_cycle_skip(market, as_of)
        if skip_reason is not None:
            self._record_strategy_cycle_log(
                level="INFO" if skip_reason == "market_closed" else "WARNING",
                message="auto-trading cycle skipped",
                created_at=as_of,
                extra={
                    "market": market,
                    "reason": skip_reason,
                    "source_env": self.settings.env.value,
                    **self._build_strategy_cycle_log_scalars(
                        strategies=strategies,
                        default_status="skipped",
                        default_skip_reason=skip_reason,
                    ),
                    **skip_details,
                },
            )
            return

        try:
            result = self.strategy_cycle_runner(market, as_of, strategies)
        except Exception as exc:
            self.state = replace(
                self.state,
                last_error=str(exc) or "auto_trading_cycle_failed",
            )
            self._record_strategy_cycle_log(
                level="ERROR",
                message="auto-trading cycle failed",
                created_at=as_of,
                extra={
                    "market": market,
                    "source_env": self.settings.env.value,
                    **self._build_strategy_cycle_log_scalars(
                        strategies=strategies,
                        default_status="failed",
                    ),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc) or "auto_trading_cycle_failed",
                },
            )
            return

        self._record_strategy_cycle_log(
            level="INFO",
            message="auto-trading cycle completed",
            created_at=as_of,
            extra=self._build_strategy_cycle_result_extra(market, result, strategies=strategies),
        )

    def _evaluate_strategy_cycle_skip(self, market: str, as_of: datetime) -> tuple[str | None, dict[str, object]]:
        if self.settings.env != RuntimeEnv.VTS:
            return "non_vts_environment", {}

        if not is_market_session_open(market, as_of):
            return "market_closed", {}

        from monitor.healthcheck import build_health_snapshot

        health_snapshot = build_health_snapshot(
            self,
            now=as_of.astimezone(timezone.utc),
        )
        details = {
            "health_status": health_snapshot.status.value,
            "trading_blocked": health_snapshot.trading_blocked,
            "writer_queue_degraded": health_snapshot.writer_queue_degraded,
            "token_stale": health_snapshot.token_stale,
            "poll_stale": health_snapshot.poll_stale,
        }
        if health_snapshot.writer_queue_degraded:
            return "writer_queue_degraded", details
        if health_snapshot.trading_blocked:
            return "trading_blocked", details
        if health_snapshot.token_stale:
            return "token_stale", details
        if health_snapshot.poll_stale:
            return "polling_stale", details
        return None, details

    def _build_strategy_cycle_result_extra(
        self,
        market: str,
        result: object,
        *,
        strategies: list[str] | None = None,
    ) -> dict[str, object]:
        strategy_diagnostics = getattr(result, "strategy_diagnostics", None)
        extra: dict[str, object] = {
            "market": market,
            "source_env": self.settings.env.value,
            **self._build_strategy_cycle_log_scalars(
                strategies=strategies,
                strategy_diagnostics=strategy_diagnostics,
                default_status="completed",
            ),
        }
        scalar_fields = {
            "signals_generated": getattr(result, "signals_generated", None),
            "signals_resolved": getattr(result, "signals_resolved", None),
            "orders_submitted": getattr(result, "orders_submitted", None),
        }
        for key, value in scalar_fields.items():
            if isinstance(value, int):
                extra[key] = value

        order_candidates = getattr(result, "order_candidates", None)
        if isinstance(order_candidates, list):
            extra["order_candidate_count"] = len(order_candidates)
        rejected_signals = getattr(result, "rejected_signals", None)
        if isinstance(rejected_signals, list):
            extra["rejected_signal_count"] = len(rejected_signals)
            rejection_reason_summary = self._build_rejection_reason_summary(rejected_signals)
            if rejection_reason_summary is not None:
                extra["rejection_reason_summary"] = rejection_reason_summary

        if isinstance(strategy_diagnostics, list):
            extra["strategy_diagnostics"] = self._serialize_strategy_diagnostics(strategy_diagnostics)

        details = getattr(result, "details", None)
        if isinstance(details, dict):
            submitted_order_count = details.get("submitted_order_count")
            if isinstance(submitted_order_count, int):
                extra["submitted_order_count"] = submitted_order_count
            submitted_notional = details.get("submitted_notional_krw")
            if isinstance(submitted_notional, (int, float)):
                extra["submitted_notional_krw"] = float(submitted_notional)
        return extra

    @classmethod
    def _build_strategy_cycle_log_scalars(
        cls,
        *,
        strategies: list[str] | None = None,
        strategy_diagnostics: list[object] | None = None,
        default_status: str,
        default_skip_reason: str | None = None,
    ) -> dict[str, object]:
        strategy_name = cls._resolve_strategy_cycle_strategy_name(strategies, strategy_diagnostics)
        selected_diagnostic = cls._select_strategy_diagnostic(strategy_name, strategy_diagnostics)
        strategy_cycle_status = default_status
        strategy_skip_reason = default_skip_reason
        factor_input_available: bool | None = None

        if selected_diagnostic is not None:
            diagnostic_status = cls._get_strategy_diagnostic_field(selected_diagnostic, "status")
            if isinstance(diagnostic_status, str) and diagnostic_status:
                strategy_cycle_status = diagnostic_status
            diagnostic_skip_reason = cls._get_strategy_diagnostic_field(selected_diagnostic, "skip_reason")
            if isinstance(diagnostic_skip_reason, str) and diagnostic_skip_reason:
                strategy_skip_reason = diagnostic_skip_reason
            diagnostic_factor_input = cls._get_strategy_diagnostic_field(selected_diagnostic, "factor_input_available")
            if isinstance(diagnostic_factor_input, bool):
                factor_input_available = diagnostic_factor_input

        return {
            "strategy_name": strategy_name,
            "strategy_cycle_status": strategy_cycle_status,
            "strategy_skip_reason": strategy_skip_reason,
            "factor_input_available": factor_input_available,
        }

    @staticmethod
    def _serialize_strategy_diagnostics(strategy_diagnostics: list[object]) -> list[object]:
        return [
            asdict(item) if hasattr(item, "__dataclass_fields__") else item
            for item in strategy_diagnostics
        ]

    @classmethod
    def _resolve_strategy_cycle_strategy_name(
        cls,
        strategies: list[str] | None,
        strategy_diagnostics: list[object] | None,
    ) -> str | None:
        if isinstance(strategies, list):
            strategy_names = [name for name in strategies if isinstance(name, str) and name]
            if len(strategy_names) == 1:
                return strategy_names[0]

        if isinstance(strategy_diagnostics, list) and len(strategy_diagnostics) == 1:
            strategy_name = cls._get_strategy_diagnostic_field(strategy_diagnostics[0], "strategy_name")
            if isinstance(strategy_name, str) and strategy_name:
                return strategy_name
        return None

    @classmethod
    def _select_strategy_diagnostic(
        cls,
        strategy_name: str | None,
        strategy_diagnostics: list[object] | None,
    ) -> object | None:
        if not isinstance(strategy_diagnostics, list) or not strategy_diagnostics:
            return None

        if strategy_name is not None:
            for item in strategy_diagnostics:
                diagnostic_strategy_name = cls._get_strategy_diagnostic_field(item, "strategy_name")
                if diagnostic_strategy_name == strategy_name:
                    return item

        if len(strategy_diagnostics) == 1:
            return strategy_diagnostics[0]
        return None

    @staticmethod
    def _get_strategy_diagnostic_field(item: object, field_name: str) -> object | None:
        if isinstance(item, dict):
            return item.get(field_name)
        return getattr(item, field_name, None)

    @staticmethod
    def _build_rejection_reason_summary(rejected_signals: list[object]) -> str | None:
        reason_counter: Counter[str] = Counter()
        for item in rejected_signals:
            reason = getattr(item, "reason", None)
            if isinstance(reason, str) and reason:
                reason_counter[reason] += 1

        if not reason_counter:
            return None
        return ",".join(f"{reason}:{count}" for reason, count in sorted(reason_counter.items()))

    def _record_strategy_cycle_log(
        self,
        *,
        level: str,
        message: str,
        created_at: datetime,
        extra: dict[str, object],
    ) -> None:
        try:
            self.operations_recorder.record_system_log(
                level=level,
                module=AUTO_TRADING_LOG_MODULE,
                message=message,
                extra=extra,
                created_at=created_at,
            )
        except Exception:
            return

    def _notify_operational_event(
        self,
        event_type: str,
        summary: str,
        *,
        severity: str = "warning",
        detail_fields: dict[str, object] | None = None,
    ) -> None:
        if self.telegram_notifier is None:
            return
        try:
            self.telegram_notifier.send_event(
                event_type,
                summary,
                context={"source_env": self.settings.env.value, **(detail_fields or {})},
                severity=severity,
                source_env=self.settings.env.value,
            )
        except Exception:
            return

    def _activate_notification_key(self, key: str) -> bool:
        if key in self._active_notification_keys:
            return False
        self._active_notification_keys.add(key)
        return True

    def _clear_notification_key(self, key: str) -> None:
        self._active_notification_keys.discard(key)

    def _notify_trading_blocked_if_needed(
        self,
        *,
        previously_blocked: bool,
        reason: str,
        detail_fields: dict[str, object] | None = None,
    ) -> None:
        if self.state.trading_blocked and not previously_blocked and self._activate_notification_key("trading_blocked"):
            self._notify_operational_event(
                "trading_blocked",
                "Trading blocked; new entries are suspended.",
                severity="critical",
                detail_fields={"reason": reason, **(detail_fields or {})},
            )
        if not self.state.trading_blocked:
            self._clear_notification_key("trading_blocked")

    def _run_token_refresh_job(self) -> None:
        if self.token_manager is None:
            self._refresh_health_status()
            return

        previous_blocked = self.state.trading_blocked
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
                self._clear_notification_key("trading_blocked")
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
        self._notify_operational_event(
            "token_refresh_failure",
            "Token refresh failed; trading remains blocked until credentials recover.",
            severity="critical",
            detail_fields={"error": self.state.last_error or "token_refresh_failed"},
        )
        self._notify_trading_blocked_if_needed(
            previously_blocked=previous_blocked,
            reason="token_refresh_failure",
            detail_fields={"error": self.state.last_error or "token_refresh_failed"},
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

        previous_blocked = self.state.trading_blocked
        try:
            if self.order_manager is not None:
                self.order_manager.start_scheduled_poll()

            access_token = self.token_manager.get_valid_token(self.settings.env)
            if self.fill_ingestion_service is not None and self.fill_processor is not None:
                fills = self._call_broker_poll_with_retry(
                    lambda: self.fill_ingestion_service.collect_execution_fills(access_token, market=active_market)
                )
                for fill in fills:
                    self.fill_processor.process_fill(fill)
            account_payload = self._call_broker_poll_with_retry(
                lambda: self.api_client.get_account_snapshot(access_token)
            )
            open_orders_payload = self._call_broker_poll_with_retry(
                lambda: self.api_client.list_open_orders(access_token)
            )
            cash_payload = self._call_broker_poll_with_retry(
                lambda: self.api_client.get_cash_balance(access_token)
            )
            snapshot = self.api_client.build_polling_snapshot(
                account_payload=account_payload,
                open_orders_payload=open_orders_payload,
                cash_payload=cash_payload,
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
                self._notify_operational_event(
                    "polling_mismatch",
                    "Broker polling detected a mismatch between broker and internal state.",
                    severity="critical",
                    detail_fields={
                        "market": active_market,
                        "mismatch_count": int(result.summary.get("mismatch_count", 0)),
                    },
                )
                self._notify_trading_blocked_if_needed(
                    previously_blocked=previous_blocked,
                    reason="polling_mismatch",
                    detail_fields={
                        "market": active_market,
                        "mismatch_count": int(result.summary.get("mismatch_count", 0)),
                    },
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
            self._notify_trading_blocked_if_needed(
                previously_blocked=previous_blocked,
                reason="polling_failures",
                detail_fields={
                    "market": active_market,
                    "consecutive_poll_failures": failure_count,
                    "error": self.state.last_error or "broker_poll_failed",
                },
            )
        self._refresh_health_status()

    def _call_broker_poll_with_retry(self, operation):
        last_error: Exception | None = None
        for attempt in range(1, BROKER_POLL_QUERY_MAX_ATTEMPTS + 1):
            try:
                return operation()
            except Exception as exc:
                last_error = exc
                if not self._is_retryable_broker_poll_error(exc) or attempt >= BROKER_POLL_QUERY_MAX_ATTEMPTS:
                    raise
                time_module.sleep(BROKER_POLL_RETRY_DELAY_SEC * attempt)
        assert last_error is not None
        raise last_error

    def _is_retryable_broker_poll_error(self, exc: Exception) -> bool:
        if self.api_client is not None and hasattr(self.api_client, "is_retryable_broker_error"):
            return bool(self.api_client.is_retryable_broker_error(exc))
        if isinstance(exc, BrokerApiError):
            if exc.status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
                return True
            message = str(exc).lower()
            return any(
                marker in message
                for marker in (
                    "egw00201",
                    "초당 거래건수를 초과하였습니다",
                    "rate limit",
                    "throttle",
                    "timeout",
                    "temporary",
                    "temporarily",
                    "retry later",
                )
            )
        return False

    def _is_retryable_broker_write_error(self, exc: Exception) -> bool:
        if self.api_client is not None and hasattr(self.api_client, "is_retryable_broker_error"):
            return bool(self.api_client.is_retryable_broker_error(exc))
        if self.order_manager is not None and isinstance(exc, BrokerApiError):
            return self.order_manager.classify_submit_exception(exc).retryable
        return False

    def _is_retryable_broker_result(self, result) -> bool:
        if self.order_manager is None:
            return False
        return self.order_manager.classify_submit_result(result).retryable

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
            missing_cancel_metadata = 0
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
                cancel_payload = {
                    "order_no": order.kis_order_no,
                    "ticker": order.ticker,
                    "market": order.market,
                }
                if market == "KR":
                    if not order.kis_order_orgno:
                        missing_cancel_metadata += 1
                        continue
                    cancel_payload["order_orgno"] = order.kis_order_orgno
                result = self._cancel_order_with_retry(cancel_payload, access_token=access_token)
                if result.accepted:
                    self.order_manager.request_cancel(order.id)
                    self.order_manager.confirm_cancel(order.id)
                    cancelled_count += 1

            self.state = replace(
                self.state,
                last_error=(
                    f"pre_close_cancel_missing_order_orgno:{missing_cancel_metadata}"
                    if missing_cancel_metadata
                    else (None if cancelled_count or not self.state.trading_blocked else self.state.last_error)
                ),
            )
            if missing_cancel_metadata:
                self._notify_operational_event(
                    "pre_close_cancel_failure",
                    "Pre-close cancel skipped because required KR cancel metadata was missing.",
                    severity="warning",
                    detail_fields={"market": market, "missing_order_orgno_count": missing_cancel_metadata},
                )
        except Exception as exc:
            self.state = replace(
                self.state,
                last_error=str(exc) or "pre_close_cancel_failed",
            )
            self._notify_operational_event(
                "pre_close_cancel_failure",
                "Pre-close cancel failed.",
                severity="warning",
                detail_fields={"market": market, "error": self.state.last_error or "pre_close_cancel_failed"},
            )
        self._refresh_health_status()

    def _cancel_order_with_retry(self, cancel_payload: dict[str, object], *, access_token: str):
        last_error: Exception | None = None
        for attempt in range(1, PRE_CLOSE_CANCEL_MAX_ATTEMPTS + 1):
            try:
                result = self.api_client.normalize_cancel_result(
                    self.api_client.cancel_order(
                        cancel_payload,
                        access_token=access_token,
                    )
                )
            except Exception as exc:
                last_error = exc
                if not self._is_retryable_broker_write_error(exc) or attempt >= PRE_CLOSE_CANCEL_MAX_ATTEMPTS:
                    raise
                time_module.sleep(PRE_CLOSE_CANCEL_RETRY_DELAY_SEC * attempt)
                continue

            if result.accepted:
                return result
            if self._is_retryable_broker_result(result) and attempt < PRE_CLOSE_CANCEL_MAX_ATTEMPTS:
                time_module.sleep(PRE_CLOSE_CANCEL_RETRY_DELAY_SEC * attempt)
                continue
            raise BrokerApiError(result.error_message or "cancel rejected")

        assert last_error is not None
        raise last_error

    def _run_healthcheck_job(self) -> None:
        self._refresh_health_status()

    def _refresh_health_status(self) -> None:
        queue_health = self.writer_queue.health()
        was_writer_queue_degraded = self.state.writer_queue_degraded
        if queue_health.degraded:
            self.state = mark_writer_queue_degraded(self.state, queue_health.last_error)
            if not was_writer_queue_degraded and self._activate_notification_key("writer_queue_degraded"):
                self._notify_operational_event(
                    "writer_queue_degraded",
                    "Writer queue degraded; trading is blocked until write health recovers.",
                    severity="critical",
                    detail_fields={"error": queue_health.last_error or "writer_queue_degraded"},
                )
            return
        self._clear_notification_key("writer_queue_degraded")
        if not self.state.trading_blocked:
            self._clear_notification_key("trading_blocked")

        health_status = RuntimeHealthStatus.WARNING if self.state.trading_blocked else RuntimeHealthStatus.NORMAL
        self.state = replace(
            self.state,
            writer_queue_degraded=False,
            health_status=health_status,
            last_error=self.state.last_error if self.state.trading_blocked else None,
        )

    def _get_active_market(self) -> str | None:
        now_kst = self.time_provider()
        configured_markets = self._configured_auto_trading_markets()
        if "KR" in configured_markets and is_market_session_open("KR", now_kst):
            return "KR"
        if "US" in configured_markets and is_market_session_open("US", now_kst):
            return "US"
        return None

    def _configured_auto_trading_markets(self) -> set[str]:
        return {market.upper() for market in self.settings.auto_trading.markets}

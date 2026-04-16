from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import requests

from core.settings import Settings, get_settings


UTC = timezone.utc

SUPPORTED_EVENT_TYPES = {
    "token_refresh_failure",
    "trading_blocked",
    "reconcile_hold",
    "writer_queue_degraded",
    "polling_mismatch",
    "pre_close_cancel_failure",
    "dr_restore_started",
    "dr_restore_completed",
    "dr_restore_failed",
    "fx_alert",
}

EVENT_LABELS = {
    "token_refresh_failure": "Token Refresh Failure",
    "trading_blocked": "Trading Blocked",
    "reconcile_hold": "Reconciliation Hold",
    "writer_queue_degraded": "Writer Queue Degraded",
    "polling_mismatch": "Polling Mismatch",
    "pre_close_cancel_failure": "Pre-close Cancel Failure",
    "dr_restore_started": "DR Restore Started",
    "dr_restore_completed": "DR Restore Completed",
    "dr_restore_failed": "DR Restore Failed",
    "fx_alert": "FX Alert",
}

SENSITIVE_FIELD_TOKENS = (
    "token",
    "account",
    "header",
    "authorization",
    "raw_payload",
    "payload",
    "credential",
    "app_key",
    "app_secret",
    "chat_id",
)


@dataclass(slots=True)
class TelegramEvent:
    event_type: str
    severity: str
    title: str
    summary: str
    detail_fields: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    source_env: str = "vts"

    def __post_init__(self) -> None:
        self.event_type = self.event_type.strip()
        if self.event_type not in SUPPORTED_EVENT_TYPES:
            raise ValueError(f"unsupported telegram event type: {self.event_type}")
        self.severity = self.severity.strip().lower() or "warning"
        self.title = self.title.strip() or EVENT_LABELS[self.event_type]
        self.summary = self.summary.strip()
        if self.created_at.tzinfo is None:
            self.created_at = self.created_at.replace(tzinfo=UTC)
        self.source_env = self.source_env.strip().lower() or "vts"


@dataclass(slots=True)
class TelegramMessage:
    event_type: str
    text: str
    chat_id: str


@dataclass(slots=True)
class TelegramDispatchResult:
    delivered: bool
    noop: bool = False
    reason: str | None = None
    error: str | None = None


class TelegramNotifier:
    def __init__(
        self,
        settings: Settings | None = None,
        sender: Callable[[TelegramMessage], None] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.sender = sender
        self.session = session or requests.Session()

    def send(self, event: TelegramEvent) -> TelegramDispatchResult:
        telegram_settings = self.settings.monitor.telegram
        if not telegram_settings.enabled:
            return TelegramDispatchResult(delivered=False, noop=True, reason="disabled")
        if telegram_settings.credentials is None:
            return TelegramDispatchResult(delivered=False, noop=True, reason="missing_credentials")

        telegram_message = TelegramMessage(
            event_type=event.event_type,
            text=self.format_event(event),
            chat_id=telegram_settings.credentials.chat_id,
        )

        try:
            if self.sender is not None:
                self.sender(telegram_message)
            else:
                self._send_via_http(telegram_message)
        except requests.RequestException as exc:
            return TelegramDispatchResult(delivered=False, error=str(exc) or exc.__class__.__name__)
        except Exception as exc:
            return TelegramDispatchResult(delivered=False, error=str(exc) or exc.__class__.__name__)

        return TelegramDispatchResult(delivered=True)

    def send_event(
        self,
        event_type: str,
        message: str,
        context: Mapping[str, Any] | None = None,
        *,
        severity: str = "warning",
        title: str | None = None,
        created_at: datetime | None = None,
        source_env: str | None = None,
    ) -> TelegramDispatchResult:
        return self.send(
            TelegramEvent(
                event_type=event_type,
                severity=severity,
                title=title or EVENT_LABELS.get(event_type, event_type.replace("_", " ").title()),
                summary=message,
                detail_fields=_sanitize_detail_fields(context),
                created_at=created_at or datetime.now(UTC),
                source_env=source_env or self.settings.env.value,
            )
        )

    def format_event(self, event: TelegramEvent) -> str:
        formatter = {
            "token_refresh_failure": self._format_token_refresh_failure,
            "trading_blocked": self._format_trading_blocked,
            "reconcile_hold": self._format_reconcile_hold,
            "writer_queue_degraded": self._format_writer_queue_degraded,
            "polling_mismatch": self._format_polling_mismatch,
            "pre_close_cancel_failure": self._format_pre_close_cancel_failure,
            "dr_restore_started": self._format_dr_restore_started,
            "dr_restore_completed": self._format_dr_restore_completed,
            "dr_restore_failed": self._format_dr_restore_failed,
            "fx_alert": self._format_fx_alert,
        }[event.event_type]
        return formatter(event)

    def _format_token_refresh_failure(self, event: TelegramEvent) -> str:
        return self._format_with_common_layout(event)

    def _format_trading_blocked(self, event: TelegramEvent) -> str:
        return self._format_with_common_layout(event)

    def _format_reconcile_hold(self, event: TelegramEvent) -> str:
        return self._format_with_common_layout(event)

    def _format_writer_queue_degraded(self, event: TelegramEvent) -> str:
        return self._format_with_common_layout(event)

    def _format_polling_mismatch(self, event: TelegramEvent) -> str:
        return self._format_with_common_layout(event)

    def _format_pre_close_cancel_failure(self, event: TelegramEvent) -> str:
        return self._format_with_common_layout(event)

    def _format_dr_restore_started(self, event: TelegramEvent) -> str:
        return self._format_with_common_layout(event)

    def _format_dr_restore_completed(self, event: TelegramEvent) -> str:
        return self._format_with_common_layout(event)

    def _format_dr_restore_failed(self, event: TelegramEvent) -> str:
        return self._format_with_common_layout(event)

    def _format_fx_alert(self, event: TelegramEvent) -> str:
        return self._format_with_common_layout(event)

    def _format_with_common_layout(self, event: TelegramEvent) -> str:
        lines = [
            f"[{event.source_env.upper()}] {event.title}",
            f"severity={event.severity}",
            f"time={event.created_at.astimezone(UTC).isoformat()}",
            event.summary,
        ]
        lines.extend(_detail_lines(event.detail_fields))
        return "\n".join(lines)

    def _send_via_http(self, telegram_message: TelegramMessage) -> None:
        credentials = self.settings.monitor.telegram.credentials
        if credentials is None:
            return

        response = self.session.post(
            f"https://api.telegram.org/bot{credentials.bot_token.get_secret_value()}/sendMessage",
            json={
                "chat_id": telegram_message.chat_id,
                "text": telegram_message.text,
            },
            timeout=self.settings.monitor.telegram.request_timeout_sec,
        )
        response.raise_for_status()


def _detail_lines(detail_fields: Mapping[str, Any]) -> list[str]:
    return [f"{key}={detail_fields[key]}" for key in sorted(detail_fields)]


def _sanitize_detail_fields(detail_fields: Mapping[str, Any] | None) -> dict[str, Any]:
    if not detail_fields:
        return {}

    sanitized: dict[str, Any] = {}
    for key, value in detail_fields.items():
        if _is_sensitive_field(key):
            continue
        if isinstance(value, (dict, list, tuple, set)):
            sanitized[key] = "<omitted>"
            continue
        sanitized[key] = value
    return sanitized


def _is_sensitive_field(key: str) -> bool:
    normalized = key.strip().lower()
    return any(token in normalized for token in SENSITIVE_FIELD_TOKENS)

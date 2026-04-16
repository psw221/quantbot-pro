from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from core.settings import Settings, get_settings


UTC = timezone.utc

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


@dataclass(slots=True)
class TelegramMessage:
    event_type: str
    text: str
    chat_id: str


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

    def send_event(self, event_type: str, message: str, context: dict | None = None) -> None:
        telegram_settings = self.settings.monitor.telegram
        if not telegram_settings.enabled or telegram_settings.credentials is None:
            return

        formatted = self.format_event_message(event_type, message, context=context)
        telegram_message = TelegramMessage(
            event_type=event_type,
            text=formatted,
            chat_id=telegram_settings.credentials.chat_id,
        )

        if self.sender is not None:
            self.sender(telegram_message)
            return

        self._send_via_http(telegram_message)

    def format_event_message(self, event_type: str, message: str, context: dict | None = None) -> str:
        now = datetime.now(UTC).isoformat()
        label = EVENT_LABELS.get(event_type, event_type.replace("_", " ").title())
        lines = [
            f"[{self.settings.env.value.upper()}] {label}",
            f"time={now}",
            message,
        ]
        if context:
            for key in sorted(context):
                lines.append(f"{key}={context[key]}")
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

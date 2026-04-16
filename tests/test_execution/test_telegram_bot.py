from __future__ import annotations

from datetime import datetime, timezone

from core.settings import Settings
from monitor.telegram_bot import TelegramEvent, TelegramMessage, TelegramNotifier
from tests.test_execution.test_bootstrap import build_settings


def _build_monitor_enabled_settings(tmp_path) -> Settings:
    settings = build_settings(tmp_path)
    payload = settings.model_dump()
    payload["monitor"] = {
        "telegram": {
            "enabled": True,
            "request_timeout_sec": 5,
            "credentials": {
                "bot_token": "telegram-token",
                "chat_id": "chat-1",
            },
        }
    }
    return Settings.model_validate(payload)


def test_telegram_notifier_formats_and_sends_event(tmp_path) -> None:
    sent: list[TelegramMessage] = []
    notifier = TelegramNotifier(
        settings=_build_monitor_enabled_settings(tmp_path),
        sender=sent.append,
    )

    result = notifier.send_event(
        "polling_mismatch",
        "broker and internal state diverged",
        context={"mismatch_count": 2, "ticker": "AAPL"},
        severity="critical",
    )

    assert result.delivered is True
    assert len(sent) == 1
    assert sent[0].chat_id == "chat-1"
    assert "[VTS] Polling Mismatch" in sent[0].text
    assert "severity=critical" in sent[0].text
    assert "mismatch_count=2" in sent[0].text
    assert "ticker=AAPL" in sent[0].text


def test_telegram_notifier_is_noop_when_disabled(tmp_path) -> None:
    sent: list[TelegramMessage] = []
    notifier = TelegramNotifier(
        settings=build_settings(tmp_path),
        sender=sent.append,
    )

    result = notifier.send_event("trading_blocked", "blocked", severity="critical")

    assert result.noop is True
    assert result.reason == "disabled"
    assert sent == []


def test_telegram_notifier_is_noop_without_credentials(tmp_path) -> None:
    sent: list[TelegramMessage] = []
    payload = build_settings(tmp_path).model_dump()
    payload["monitor"] = {
        "telegram": {
            "enabled": True,
            "request_timeout_sec": 5,
            "credentials": None,
        }
    }
    settings = Settings.model_validate(payload)
    notifier = TelegramNotifier(settings=settings, sender=sent.append)

    result = notifier.send_event("token_refresh_failure", "refresh failed", severity="critical")

    assert result.noop is True
    assert result.reason == "missing_credentials"
    assert sent == []


def test_telegram_notifier_formats_trading_blocked_event_from_explicit_model(tmp_path) -> None:
    sent: list[TelegramMessage] = []
    notifier = TelegramNotifier(settings=_build_monitor_enabled_settings(tmp_path), sender=sent.append)
    event = TelegramEvent(
        event_type="trading_blocked",
        severity="critical",
        title="Trading Blocked",
        summary="new orders are blocked until reconciliation completes",
        detail_fields={"reason": "polling_mismatch_detected", "health_status": "critical"},
        created_at=datetime(2026, 4, 16, 1, 0, tzinfo=timezone.utc),
        source_env="vts",
    )

    result = notifier.send(event)

    assert result.delivered is True
    assert "Trading Blocked" in sent[0].text
    assert "severity=critical" in sent[0].text
    assert "reason=polling_mismatch_detected" in sent[0].text


def test_telegram_notifier_formats_writer_queue_degraded_event(tmp_path) -> None:
    sent: list[TelegramMessage] = []
    notifier = TelegramNotifier(settings=_build_monitor_enabled_settings(tmp_path), sender=sent.append)

    result = notifier.send_event(
        "writer_queue_degraded",
        "writer queue entered degraded mode",
        context={"queue_depth": 7, "writer_queue_last_error": "sqlite_busy"},
        severity="critical",
    )

    assert result.delivered is True
    assert "Writer Queue Degraded" in sent[0].text
    assert "queue_depth=7" in sent[0].text
    assert "writer_queue_last_error=sqlite_busy" in sent[0].text


def test_telegram_notifier_filters_sensitive_fields_from_message(tmp_path) -> None:
    sent: list[TelegramMessage] = []
    notifier = TelegramNotifier(settings=_build_monitor_enabled_settings(tmp_path), sender=sent.append)

    result = notifier.send_event(
        "fx_alert",
        "usd_krw moved beyond threshold",
        context={
            "ticker": "USDKRW",
            "account_no": "123-45-6789",
            "bot_token": "secret",
            "raw_payload": {"secret": "value"},
            "authorization_header": "Bearer abc",
        },
        severity="warning",
    )

    assert result.delivered is True
    assert "ticker=USDKRW" in sent[0].text
    assert "123-45-6789" not in sent[0].text
    assert "secret" not in sent[0].text
    assert "authorization_header" not in sent[0].text


def test_telegram_notifier_sender_exception_is_contained(tmp_path) -> None:
    notifier = TelegramNotifier(
        settings=_build_monitor_enabled_settings(tmp_path),
        sender=lambda _: (_ for _ in ()).throw(RuntimeError("sender failed")),
    )

    result = notifier.send_event("pre_close_cancel_failure", "cancel job failed", severity="warning")

    assert result.delivered is False
    assert result.noop is False
    assert "sender failed" in (result.error or "")

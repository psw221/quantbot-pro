from __future__ import annotations

from core.settings import Settings
from monitor.telegram_bot import TelegramMessage, TelegramNotifier
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

    notifier.send_event(
        "polling_mismatch",
        "broker and internal state diverged",
        context={"mismatch_count": 2, "ticker": "AAPL"},
    )

    assert len(sent) == 1
    assert sent[0].chat_id == "chat-1"
    assert "[VTS] Polling Mismatch" in sent[0].text
    assert "mismatch_count=2" in sent[0].text
    assert "ticker=AAPL" in sent[0].text


def test_telegram_notifier_is_noop_when_disabled(tmp_path) -> None:
    sent: list[TelegramMessage] = []
    notifier = TelegramNotifier(
        settings=build_settings(tmp_path),
        sender=sent.append,
    )

    notifier.send_event("trading_blocked", "blocked")

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

    notifier.send_event("token_refresh_failure", "refresh failed")

    assert sent == []

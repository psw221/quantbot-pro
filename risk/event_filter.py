from __future__ import annotations

from core.models import EventFlag, EventType, RiskDecision, Signal
from core.settings import Settings, get_settings


class EventFilter:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def evaluate_signal(self, signal: Signal, event_flags: list[EventFlag] | None = None) -> RiskDecision:
        if not self.settings.strategies.event_filter_enabled:
            return RiskDecision(approved=True, reason="event_filter_disabled")

        flags = [flag for flag in event_flags or [] if flag.active]
        scale_factor = 1.0
        tags: list[str] = []

        if signal.action != "buy":
            return RiskDecision(approved=True, reason="event_filter_not_applicable")

        for flag in flags:
            if flag.event_type == EventType.FOMC and signal.market == "US":
                return RiskDecision(approved=False, reason="blocked_by_fomc", tags=["event:fomc"])
            if flag.event_type == EventType.BOK and signal.market == "KR":
                return RiskDecision(approved=False, reason="blocked_by_bok", tags=["event:bok"])
            if flag.event_type == EventType.CPI_PPI and signal.market == "US":
                return RiskDecision(approved=False, reason="blocked_by_cpi_ppi", tags=["event:cpi_ppi"])
            if flag.event_type == EventType.EARNINGS and flag.ticker == signal.ticker:
                return RiskDecision(approved=False, reason="blocked_by_earnings", tags=["event:earnings"])
            if flag.event_type == EventType.VIX_HIGH and signal.market == "US":
                scale_factor = min(scale_factor, 0.5)
                tags.append("event:vix_high")
            if flag.event_type == EventType.VKOSPI_HIGH and signal.market == "KR":
                scale_factor = min(scale_factor, 0.5)
                tags.append("event:vkospi_high")

        return RiskDecision(
            approved=True,
            reason="approved",
            tags=tags,
            scale_factor=scale_factor,
            metadata={"blocked_by_event": False},
        )

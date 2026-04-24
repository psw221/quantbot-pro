from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, timezone

from core.models import EventFlag, EventType, MarketConstraintDecision, PositionSnapshot, Signal
from core.settings import Settings, get_settings


KST = timezone(timedelta(hours=9))


@dataclass(slots=True)
class MarketConstraintInput:
    signal: Signal
    quantity: int
    current_price: float
    previous_close: float | None
    as_of: datetime
    position: PositionSnapshot | None = None
    order_type: str = "market"
    price: float | None = None
    cash_available: float = 0.0
    event_flags: list[EventFlag] = field(default_factory=list)


class MarketConstraintValidator:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def evaluate(self, constraint_input: MarketConstraintInput) -> MarketConstraintDecision:
        if constraint_input.signal.market != "KR":
            return MarketConstraintDecision(approved=True, reason="not_applicable")

        if constraint_input.quantity < 1:
            return MarketConstraintDecision(
                approved=False,
                reason="invalid_quantity",
                tags=["kr:minimum_quantity"],
            )

        if self.settings.risk.kr_block_auction_entries and self._is_auction_session(constraint_input.as_of):
            return MarketConstraintDecision(
                approved=False,
                reason="kr_auction_session",
                tags=["kr:auction"],
                metadata={"as_of": self._to_kst(constraint_input.as_of).isoformat()},
            )

        if constraint_input.signal.action == "sell":
            return self._evaluate_sell(constraint_input)

        if constraint_input.signal.action == "buy":
            event_decision = self._evaluate_event_flags(constraint_input.event_flags)
            if not event_decision.approved:
                return event_decision
            price_decision = self._evaluate_price_limit(constraint_input)
            if not price_decision.approved:
                return price_decision
            return self._evaluate_cash(constraint_input)

        return MarketConstraintDecision(approved=True, reason="unsupported_action_ignored")

    def _evaluate_sell(self, constraint_input: MarketConstraintInput) -> MarketConstraintDecision:
        if not self.settings.risk.kr_short_sell_block_enabled:
            return MarketConstraintDecision(approved=True, reason="approved")

        held_quantity = 0 if constraint_input.position is None else constraint_input.position.quantity
        if constraint_input.quantity > held_quantity:
            return MarketConstraintDecision(
                approved=False,
                reason="short_sell_blocked",
                tags=["kr:short_sell"],
                metadata={"held_quantity": held_quantity, "order_quantity": constraint_input.quantity},
            )
        return MarketConstraintDecision(approved=True, reason="approved")

    def _evaluate_price_limit(self, constraint_input: MarketConstraintInput) -> MarketConstraintDecision:
        previous_close = constraint_input.previous_close
        if previous_close is None or previous_close <= 0:
            return MarketConstraintDecision(
                approved=False,
                reason="previous_close_missing",
                tags=["kr:price_limit"],
            )

        order_price = constraint_input.price if constraint_input.order_type == "limit" else constraint_input.current_price
        if order_price is None or order_price <= 0:
            return MarketConstraintDecision(
                approved=False,
                reason="invalid_price",
                tags=["kr:price_limit"],
            )

        lower_bound = previous_close * (1 - self.settings.risk.kr_price_limit_pct)
        upper_bound = previous_close * (1 + self.settings.risk.kr_price_limit_pct)
        if order_price < lower_bound or order_price > upper_bound:
            return MarketConstraintDecision(
                approved=False,
                reason="kr_price_limit",
                tags=["kr:price_limit"],
                metadata={
                    "previous_close": previous_close,
                    "order_price": order_price,
                    "lower_bound": lower_bound,
                    "upper_bound": upper_bound,
                },
            )
        return MarketConstraintDecision(approved=True, reason="approved")

    def _evaluate_cash(self, constraint_input: MarketConstraintInput) -> MarketConstraintDecision:
        required_cash = constraint_input.quantity * constraint_input.current_price
        available_cash = constraint_input.cash_available * (1 - self.settings.risk.kr_settlement_cash_buffer_pct)
        if required_cash > available_cash:
            return MarketConstraintDecision(
                approved=False,
                reason="settlement_cash_unavailable",
                tags=["kr:settlement_cash"],
                metadata={"required_cash": required_cash, "available_cash": available_cash},
            )
        return MarketConstraintDecision(approved=True, reason="approved")

    @staticmethod
    def _evaluate_event_flags(event_flags: list[EventFlag]) -> MarketConstraintDecision:
        for flag in event_flags:
            if not flag.active:
                continue
            action = str(flag.metadata.get("action", "")).lower()
            if flag.event_type == EventType.KR_OVERHEATED or action in {"exclude_universe", "block_buy_overheated"}:
                return MarketConstraintDecision(
                    approved=False,
                    reason="kr_overheated",
                    tags=["kr:overheated"],
                    metadata={"event_type": flag.event_type.value, "ticker": flag.ticker},
                )
            if flag.event_type == EventType.KR_TRADING_HALT or action in {"trading_halt", "block_trading"}:
                return MarketConstraintDecision(
                    approved=False,
                    reason="kr_trading_halt",
                    tags=["kr:trading_halt"],
                    metadata={"event_type": flag.event_type.value, "ticker": flag.ticker},
                )
        return MarketConstraintDecision(approved=True, reason="approved")

    def _is_auction_session(self, as_of: datetime) -> bool:
        kst_time = self._to_kst(as_of).time()
        return any(
            self._time_in_range(kst_time, configured_range)
            for configured_range in (
                self.settings.risk.kr_opening_auction,
                self.settings.risk.kr_closing_auction,
            )
        )

    @staticmethod
    def _to_kst(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC).astimezone(KST)
        return value.astimezone(KST)

    @staticmethod
    def _time_in_range(value: time, configured_range: str) -> bool:
        start_raw, end_raw = configured_range.split("-", 1)
        start = time.fromisoformat(start_raw)
        end = time.fromisoformat(end_raw)
        return start <= value < end

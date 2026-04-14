from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal


StrategyName = Literal["dual_momentum", "trend_following", "factor_investing"]
MarketCode = Literal["KR", "US"]
SignalAction = Literal["buy", "sell", "hold"]
OrderSide = Literal["buy", "sell"]
OrderType = Literal["limit", "market"]


class SignalStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    ORDERED = "ordered"


class OrderStatus(str, Enum):
    PENDING = "pending"
    VALIDATED = "validated"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    RECONCILE_HOLD = "reconcile_hold"
    FAILED = "failed"


class ReconciliationStatus(str, Enum):
    IDLE = "idle"
    SCHEDULED_POLLING = "scheduled_polling"
    MISMATCH_DETECTED = "mismatch_detected"
    RECONCILING = "reconciling"
    RECONCILED = "reconciled"
    FAILED = "failed"


class ReconciliationMismatchType(str, Enum):
    MISSING_FILL = "missing_fill"
    QUANTITY_DIFF = "quantity_diff"
    ORDER_STATUS_DIFF = "order_status_diff"
    CASH_DIFF = "cash_diff"


@dataclass(slots=True)
class Signal:
    ticker: str
    market: MarketCode
    action: SignalAction
    strategy: StrategyName
    strength: float
    reason: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    signal_source: str = "strategy"
    is_exit: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderIntent:
    client_order_id: str
    signal_id: int
    ticker: str
    market: MarketCode
    strategy: StrategyName
    side: OrderSide
    quantity: int
    order_type: OrderType
    price: float | None
    risk_tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionFill:
    order_id: int
    execution_no: str
    fill_seq: int
    filled_quantity: int
    filled_price: float
    fee: float
    tax: float
    executed_at: datetime
    currency: str = "KRW"
    trade_fx_rate: float | None = None
    settlement_date: datetime | None = None
    settlement_fx_rate: float | None = None
    fx_rate_source: str | None = None


@dataclass(slots=True)
class PositionSnapshot:
    ticker: str
    market: MarketCode
    strategy: StrategyName
    quantity: int
    avg_cost: float
    current_price: float
    highest_price: float
    entry_date: datetime


@dataclass(slots=True)
class BrokerPositionSnapshot:
    ticker: str
    market: MarketCode
    quantity: int
    avg_cost: float
    currency: Literal["KRW", "USD"]
    snapshot_at: datetime
    source_env: Literal["vts", "prod"]


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    reason: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SizingInput:
    ticker: str
    market: MarketCode
    strategy: StrategyName
    cash_available: float
    price: float
    volatility: float


@dataclass(slots=True)
class SizingDecision:
    quantity: int
    target_notional: float
    capped: bool = False
    reason: str = ""


@dataclass(slots=True)
class ReconciliationMismatch:
    mismatch_type: ReconciliationMismatchType
    ticker: str | None
    detail: str


@dataclass(slots=True)
class ReconciliationResult:
    status: ReconciliationStatus
    mismatches: list[ReconciliationMismatch] = field(default_factory=list)
    missing_fills: list[ExecutionFill] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

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


class SubmitFailureClass(str, Enum):
    RETRYABLE = "retryable"
    TERMINAL = "terminal"
    AUTH = "auth"
    RECONCILE_HOLD = "reconcile_hold"


class RuntimeHealthStatus(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"


class EventType(str, Enum):
    FOMC = "fomc"
    BOK = "bok"
    CPI_PPI = "cpi_ppi"
    EARNINGS = "earnings"
    VIX_HIGH = "vix_high"
    VKOSPI_HIGH = "vkospi_high"
    KR_OVERHEATED = "kr_overheated"
    KR_TRADING_HALT = "kr_trading_halt"


@dataclass(slots=True)
class BrokerOrderSnapshot:
    order_no: str
    ticker: str
    market: MarketCode
    side: OrderSide
    quantity: int
    remaining_quantity: int
    status: str
    price: float | None = None


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
class BrokerFillSnapshot:
    order_no: str
    order_orgno: str | None
    ticker: str
    market: MarketCode
    side: OrderSide
    order_quantity: int
    cumulative_filled_quantity: int
    remaining_quantity: int
    average_filled_price: float | None
    occurred_at: datetime
    execution_hint: str | None = None


@dataclass(slots=True)
class PriceBar:
    ticker: str
    market: MarketCode
    timestamp: datetime
    close: float
    high: float | None = None
    low: float | None = None


@dataclass(slots=True)
class FactorSnapshot:
    ticker: str
    market: MarketCode
    value_score: float
    quality_score: float
    momentum_score: float
    low_vol_score: float


@dataclass(slots=True)
class EventFlag:
    event_type: EventType
    market: MarketCode
    ticker: str | None = None
    active: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    reason: str = ""
    tags: list[str] = field(default_factory=list)
    scale_factor: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MarketConstraintDecision:
    approved: bool
    reason: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SizingInput:
    ticker: str
    market: MarketCode
    strategy: StrategyName
    cash_available: float
    price: float
    volatility: float
    target_volatility: float | None = None
    min_position_fraction: float | None = None
    risk_scale: float = 1.0


@dataclass(slots=True)
class SizingDecision:
    quantity: int
    target_notional: float
    capped: bool = False
    reason: str = ""
    volatility_scale: float = 1.0


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


@dataclass(slots=True)
class BrokerPollingSnapshot:
    positions: list[BrokerPositionSnapshot]
    open_orders: list[BrokerOrderSnapshot]
    cash_available: float
    raw_payloads: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BrokerOrderResult:
    accepted: bool
    broker_order_no: str | None = None
    broker_order_orgno: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SubmitFailureDecision:
    failure_class: SubmitFailureClass
    retryable: bool
    block_trading: bool = False
    require_reconciliation_hold: bool = False


@dataclass(slots=True)
class RuntimeState:
    scheduler_running: bool = False
    trading_blocked: bool = False
    writer_queue_degraded: bool = False
    health_status: RuntimeHealthStatus = RuntimeHealthStatus.NORMAL
    last_token_refresh_at: datetime | None = None
    last_poll_success_at: datetime | None = None
    consecutive_poll_failures: int = 0
    last_error: str | None = None

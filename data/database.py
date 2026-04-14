from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from core.settings import Settings, get_settings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("idx_positions_strategy", "strategy", "market"),
        Index("uq_positions_ticker_market_strategy", "ticker", "market", "strategy", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    market: Mapped[str] = mapped_column(String, nullable=False)
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_cost: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    highest_price: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    entry_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class PositionLot(Base):
    __tablename__ = "position_lots"
    __table_args__ = (Index("idx_position_lots_lookup", "ticker", "market", "strategy", "opened_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), nullable=False)
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    market: Mapped[str] = mapped_column(String, nullable=False)
    open_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    open_price: Mapped[float] = mapped_column(Float, nullable=False)
    open_trade_fx_rate: Mapped[float | None] = mapped_column(Float)
    open_settlement_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    open_settlement_fx_rate: Mapped[float | None] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class BrokerPosition(Base):
    __tablename__ = "broker_positions"
    __table_args__ = (
        Index("idx_broker_positions_snapshot", "ticker", "market", "source_env", "snapshot_at"),
        Index(
            "uq_broker_positions_ticker_market_env_snapshot",
            "ticker",
            "market",
            "source_env",
            "snapshot_at",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    market: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_cost: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="KRW")
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_env: Mapped[str] = mapped_column(String, nullable=False)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("idx_signals_status", "status", "generated_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    market: Mapped[str] = mapped_column(String, nullable=False)
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    strength: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    reject_reason: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("idx_orders_status", "status", "updated_at"),
        Index("idx_orders_kis_order_no", "kis_order_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    kis_order_no: Mapped[str | None] = mapped_column(String)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    market: Mapped[str] = mapped_column(String, nullable=False)
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    order_type: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    error_code: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(Text)


class OrderExecution(Base):
    __tablename__ = "order_executions"
    __table_args__ = (Index("idx_order_executions_order", "order_id", "executed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    execution_no: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    fill_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_price: Mapped[float] = mapped_column(Float, nullable=False)
    fee: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    tax: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="KRW")
    trade_fx_rate: Mapped[float | None] = mapped_column(Float)
    settlement_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settlement_fx_rate: Mapped[float | None] = mapped_column(Float)
    fx_rate_source: Mapped[str | None] = mapped_column(String)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("idx_trades_ticker_date", "ticker", "executed_at"),
        Index("idx_trades_strategy", "strategy", "executed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    execution_id: Mapped[int] = mapped_column(ForeignKey("order_executions.id"), nullable=False, unique=True)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    market: Mapped[str] = mapped_column(String, nullable=False)
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    fee: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    tax: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    net_amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="KRW")
    trade_fx_rate: Mapped[float | None] = mapped_column(Float)
    settlement_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settlement_fx_rate: Mapped[float | None] = mapped_column(Float)
    fx_rate_source: Mapped[str | None] = mapped_column(String)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class TaxEvent(Base):
    __tablename__ = "tax_events"
    __table_args__ = (Index("idx_tax_events_year_market", "tax_year", "market"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), nullable=False, unique=True)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    market: Mapped[str] = mapped_column(String, nullable=False)
    sell_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    sell_price: Mapped[float] = mapped_column(Float, nullable=False)
    cost_basis: Mapped[float] = mapped_column(Float, nullable=False)
    gain_loss_usd: Mapped[float | None] = mapped_column(Float)
    gain_loss_krw: Mapped[float] = mapped_column(Float, nullable=False)
    buy_trade_fx_rate: Mapped[float | None] = mapped_column(Float)
    buy_settlement_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    buy_settlement_fx_rate: Mapped[float | None] = mapped_column(Float)
    sell_trade_fx_rate: Mapped[float | None] = mapped_column(Float)
    sell_settlement_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sell_settlement_fx_rate: Mapped[float | None] = mapped_column(Float)
    fx_rate_source: Mapped[str | None] = mapped_column(String)
    taxable_gain: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    tax_year: Mapped[int] = mapped_column(Integer, nullable=False)
    is_included_in_report: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class TokenStore(Base):
    __tablename__ = "token_store"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    env: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SystemLog(Base):
    __tablename__ = "system_logs"
    __table_args__ = (Index("idx_system_logs_level_date", "level", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String, nullable=False)
    module: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    extra_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (Index("idx_reconciliation_runs_started_at", "started_at", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_type: Mapped[str] = mapped_column(String, nullable=False)
    source_env: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mismatch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False)
    summary_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


_ENGINE = None
_SESSION_FACTORY: sessionmaker[Session] | None = None


def _sqlite_uri(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _apply_sqlite_pragmas(dbapi_connection, settings: Settings) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute(f"PRAGMA busy_timeout={settings.database.busy_timeout_ms};")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


def init_engine(settings: Settings | None = None):
    global _ENGINE, _SESSION_FACTORY

    settings = settings or get_settings()
    db_path = settings.database.absolute_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        _sqlite_uri(db_path),
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record) -> None:  # type: ignore[unused-argument]
        _apply_sqlite_pragmas(dbapi_connection, settings)

    _ENGINE = engine
    _SESSION_FACTORY = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def get_engine():
    if _ENGINE is None:
        return init_engine(get_settings())
    return _ENGINE


def get_session_factory() -> sessionmaker[Session]:
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        init_engine(get_settings())
    assert _SESSION_FACTORY is not None
    return _SESSION_FACTORY


def init_db(settings: Settings | None = None) -> None:
    engine = init_engine(settings)
    Base.metadata.create_all(engine)


@contextmanager
def get_read_session() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()

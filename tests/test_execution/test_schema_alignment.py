from __future__ import annotations

from sqlalchemy import inspect, text

from data.database import get_engine, init_db
from tests.test_execution.test_bootstrap import build_settings


def test_init_db_creates_all_documented_sqlite_tables(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    engine = get_engine()

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    assert {
        "positions",
        "position_lots",
        "broker_positions",
        "signals",
        "orders",
        "order_executions",
        "trades",
        "portfolio_snapshots",
        "token_store",
        "event_calendar",
        "tax_events",
        "backtest_results",
        "system_logs",
        "reconciliation_runs",
    }.issubset(table_names)


def test_init_db_applies_documented_pragmas(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    engine = get_engine()

    with engine.connect() as connection:
        journal_mode = connection.execute(text("PRAGMA journal_mode;")).scalar_one()
        synchronous = connection.execute(text("PRAGMA synchronous;")).scalar_one()
        busy_timeout = connection.execute(text("PRAGMA busy_timeout;")).scalar_one()
        foreign_keys = connection.execute(text("PRAGMA foreign_keys;")).scalar_one()

    assert str(journal_mode).lower() == "wal"
    assert synchronous == 1  # NORMAL
    assert busy_timeout == settings.database.busy_timeout_ms
    assert foreign_keys == 1


def test_init_db_creates_canonical_indexes_and_uniques(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    engine = get_engine()
    inspector = inspect(engine)

    portfolio_uniques = inspector.get_unique_constraints("portfolio_snapshots")
    assert any("snapshot_date" in constraint["column_names"] for constraint in portfolio_uniques)

    orders_uniques = inspector.get_unique_constraints("orders")
    assert any("client_order_id" in constraint["column_names"] for constraint in orders_uniques)

    trades_uniques = inspector.get_unique_constraints("trades")
    assert any("execution_id" in constraint["column_names"] for constraint in trades_uniques)

    token_store_uniques = inspector.get_unique_constraints("token_store")
    assert any("env" in constraint["column_names"] for constraint in token_store_uniques)

    broker_indexes = inspector.get_indexes("broker_positions")
    assert any(
        index["name"] == "uq_broker_positions_ticker_market_env_snapshot" and bool(index.get("unique"))
        for index in broker_indexes
    )

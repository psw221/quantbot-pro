from __future__ import annotations

import json
from datetime import UTC, datetime

from data.database import PortfolioSnapshot, SystemLog, get_session_factory, init_db
from execution.writer_queue import WriterQueue
from monitor.operations import OperationsRecorder, PortfolioSnapshotPayload, SystemLogPayload
from tests.test_execution.test_bootstrap import build_settings


def test_operations_recorder_records_system_log_with_sanitized_extra_fields(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()

    try:
        recorder = OperationsRecorder(writer_queue)
        row_id = recorder.record_system_log(
            SystemLogPayload(
                level="warning",
                module="monitor.operations",
                message="polling mismatch detected",
                extra_fields={
                    "ticker": "AAPL",
                    "account_no": "12345678",
                    "bot_token": "secret",
                    "raw_payload": {"secret": "value"},
                    "mismatch_count": 2,
                },
                created_at=datetime(2026, 4, 16, tzinfo=UTC),
            )
        )
    finally:
        writer_queue.stop()

    with get_session_factory()() as session:
        row = session.get(SystemLog, row_id)

    assert row is not None
    assert row.level == "WARNING"
    extra_json = json.loads(row.extra_json or "{}")
    assert extra_json == {"mismatch_count": 2, "ticker": "AAPL"}


def test_operations_recorder_upserts_portfolio_snapshot(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()

    try:
        recorder = OperationsRecorder(writer_queue)
        snapshot_date = datetime(2026, 4, 16, tzinfo=UTC)
        first_id = recorder.record_portfolio_snapshot(
            PortfolioSnapshotPayload(
                snapshot_date=snapshot_date,
                total_value_krw=1000000,
                cash_krw=100000,
                domestic_value_krw=500000,
                overseas_value_krw=400000,
                usd_krw_rate=1350,
                position_count=3,
            )
        )
        second_id = recorder.record_portfolio_snapshot(
            PortfolioSnapshotPayload(
                snapshot_date=snapshot_date,
                total_value_krw=1100000,
                cash_krw=150000,
                domestic_value_krw=550000,
                overseas_value_krw=400000,
                usd_krw_rate=1360,
                daily_return=0.01,
                cumulative_return=0.12,
                drawdown=-0.03,
                max_drawdown=-0.08,
                position_count=4,
            )
        )
    finally:
        writer_queue.stop()

    with get_session_factory()() as session:
        rows = session.query(PortfolioSnapshot).all()

    assert first_id == second_id
    assert len(rows) == 1
    assert rows[0].total_value_krw == 1100000
    assert rows[0].position_count == 4
    assert rows[0].usd_krw_rate == 1360


def test_operations_recorder_build_system_log_payload_requires_required_fields(tmp_path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()

    try:
        recorder = OperationsRecorder(writer_queue)
        try:
            recorder.build_system_log_payload(level=None, module="monitor.operations", message="x")
        except ValueError as exc:
            assert "required" in str(exc)
        else:
            raise AssertionError("expected ValueError")
    finally:
        writer_queue.stop()

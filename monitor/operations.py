from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from data.database import PortfolioSnapshot, SystemLog, utc_now
from execution.writer_queue import WriterQueue


@dataclass(slots=True)
class PortfolioSnapshotPayload:
    snapshot_date: datetime
    total_value_krw: float
    cash_krw: float
    domestic_value_krw: float
    overseas_value_krw: float
    usd_krw_rate: float
    daily_return: float = 0.0
    cumulative_return: float = 0.0
    drawdown: float = 0.0
    max_drawdown: float = 0.0
    position_count: int = 0


class OperationsRecorder:
    def __init__(self, writer_queue: WriterQueue) -> None:
        self.writer_queue = writer_queue

    def record_system_log(
        self,
        *,
        level: str,
        module: str,
        message: str,
        extra: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> int:
        future = self.writer_queue.submit(
            lambda session: self._insert_system_log(
                session,
                level=level,
                module=module,
                message=message,
                extra=extra,
                created_at=created_at,
            ),
            description="record system log",
        )
        return future.result()

    def record_portfolio_snapshot(self, payload: PortfolioSnapshotPayload) -> int:
        future = self.writer_queue.submit(
            lambda session: self._upsert_portfolio_snapshot(session, payload),
            description="record portfolio snapshot",
        )
        return future.result()

    @staticmethod
    def _insert_system_log(
        session,
        *,
        level: str,
        module: str,
        message: str,
        extra: dict[str, Any] | None,
        created_at: datetime | None,
    ) -> int:
        row = SystemLog(
            level=level,
            module=module,
            message=message,
            extra_json=None if extra is None else json.dumps(extra, sort_keys=True, default=str),
            created_at=created_at or utc_now(),
        )
        session.add(row)
        session.flush()
        return row.id

    @staticmethod
    def _upsert_portfolio_snapshot(session, payload: PortfolioSnapshotPayload) -> int:
        row = session.query(PortfolioSnapshot).filter(PortfolioSnapshot.snapshot_date == payload.snapshot_date).one_or_none()
        if row is None:
            row = PortfolioSnapshot(
                snapshot_date=payload.snapshot_date,
                total_value_krw=payload.total_value_krw,
                cash_krw=payload.cash_krw,
                domestic_value_krw=payload.domestic_value_krw,
                overseas_value_krw=payload.overseas_value_krw,
                usd_krw_rate=payload.usd_krw_rate,
                daily_return=payload.daily_return,
                cumulative_return=payload.cumulative_return,
                drawdown=payload.drawdown,
                max_drawdown=payload.max_drawdown,
                position_count=payload.position_count,
                created_at=utc_now(),
            )
            session.add(row)
            session.flush()
            return row.id

        row.total_value_krw = payload.total_value_krw
        row.cash_krw = payload.cash_krw
        row.domestic_value_krw = payload.domestic_value_krw
        row.overseas_value_krw = payload.overseas_value_krw
        row.usd_krw_rate = payload.usd_krw_rate
        row.daily_return = payload.daily_return
        row.cumulative_return = payload.cumulative_return
        row.drawdown = payload.drawdown
        row.max_drawdown = payload.max_drawdown
        row.position_count = payload.position_count
        session.flush()
        return row.id

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from data.database import PortfolioSnapshot, SystemLog, utc_now
from execution.writer_queue import WriterQueue


SENSITIVE_FIELD_TOKENS = (
    "token",
    "account",
    "header",
    "authorization",
    "raw_payload",
    "payload",
    "credential",
    "app_key",
    "app_secret",
    "chat_id",
)


@dataclass(slots=True)
class SystemLogPayload:
    level: str
    module: str
    message: str
    extra_fields: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None

    def normalized(self) -> "SystemLogPayload":
        return SystemLogPayload(
            level=self.level.strip().upper(),
            module=self.module.strip(),
            message=self.message.strip(),
            extra_fields=_sanitize_extra_fields(self.extra_fields),
            created_at=self.created_at,
        )


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
        payload: SystemLogPayload | None = None,
        /,
        *,
        level: str | None = None,
        module: str | None = None,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> int:
        log_payload = (
            payload.normalized()
            if payload is not None
            else self.build_system_log_payload(
                level=level,
                module=module,
                message=message,
                extra=extra,
                created_at=created_at,
            )
        )
        future = self.writer_queue.submit(
            lambda session: self._insert_system_log(
                session,
                payload=log_payload,
            ),
            description="record system log",
        )
        return future.result()

    def build_system_log_payload(
        self,
        *,
        level: str | None,
        module: str | None,
        message: str | None,
        extra: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> SystemLogPayload:
        if not level or not module or not message:
            raise ValueError("level, module, and message are required")
        return SystemLogPayload(
            level=level,
            module=module,
            message=message,
            extra_fields=dict(extra or {}),
            created_at=created_at,
        ).normalized()

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
        payload: SystemLogPayload,
    ) -> int:
        row = SystemLog(
            level=payload.level,
            module=payload.module,
            message=payload.message,
            extra_json=None
            if not payload.extra_fields
            else json.dumps(payload.extra_fields, sort_keys=True, default=str),
            created_at=payload.created_at or utc_now(),
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


def _sanitize_extra_fields(extra_fields: Mapping[str, Any] | None) -> dict[str, Any]:
    if not extra_fields:
        return {}

    sanitized: dict[str, Any] = {}
    for key, value in extra_fields.items():
        if _is_sensitive_field(key):
            continue
        if isinstance(value, Mapping):
            sanitized[key] = "<omitted>"
            continue
        if isinstance(value, (list, tuple, set)):
            sanitized[key] = "<omitted>"
            continue
        sanitized[key] = value
    return sanitized


def _is_sensitive_field(key: str) -> bool:
    normalized = key.strip().lower()
    return any(token in normalized for token in SENSITIVE_FIELD_TOKENS)

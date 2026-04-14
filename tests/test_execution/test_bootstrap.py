from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from auth.token_manager import TokenManager
from core.settings import Settings
from data.database import TokenStore, get_read_session, init_db, utc_now
from execution.kis_api import KISApiClient
from execution.writer_queue import WriterQueue


class DummyResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self) -> dict:
        return self._payload


class DummySession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return DummyResponse(self.payload)


def build_settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "env": "vts",
            "allocation": {"domestic": 0.60, "overseas": 0.30, "cash_buffer": 0.10},
            "strategy_weights": {
                "dual_momentum": 0.30,
                "trend_following": 0.25,
                "factor_investing": 0.45,
            },
            "database": {"path": str(tmp_path / "quantbot.db"), "busy_timeout_ms": 5000},
            "logging": {"level": "INFO", "directory": str(tmp_path / "logs")},
            "kis": {
                "rate_limit_per_sec": 20,
                "request_timeout_sec": 3,
                "credentials": {
                    "app_key": "key12345",
                    "app_secret": "secret12345",
                    "account_no": "12345678",
                    "product_code": "01",
                },
                "environments": {
                    "vts": {
                        "rest_base_url": "https://example.test:29443",
                        "websocket_base_url": "ws://example.test:31000",
                        "token_path": "/oauth2/tokenP",
                    },
                    "prod": {
                        "rest_base_url": "https://prod.test:9443",
                        "websocket_base_url": "ws://prod.test:21000",
                        "token_path": "/oauth2/tokenP",
                    },
                },
            },
            "rebalancing": {
                "macro_threshold_pct_point": 0.05,
                "macro_check": "monthly_eom",
                "broker_poll_interval_min": 10,
            },
            "risk": {
                "max_single_stock_domestic": 0.05,
                "max_single_stock_overseas": 0.03,
                "max_sector_weight": 0.25,
                "stop_loss_domestic": -0.07,
                "stop_loss_overseas": -0.05,
                "trailing_stop": -0.10,
                "daily_max_loss": -0.02,
                "max_drawdown_limit": -0.15,
            },
        }
    )


def test_init_db_applies_wal_mode(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)

    with get_read_session() as session:
        journal_mode = session.execute(text("PRAGMA journal_mode;")).scalar_one()
        foreign_keys = session.execute(text("PRAGMA foreign_keys;")).scalar_one()

    assert str(journal_mode).lower() == "wal"
    assert foreign_keys == 1


def test_writer_queue_serializes_writes(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue(max_retries=2)
    writer_queue.start()

    try:
        futures = []
        for index in range(3):
            futures.append(
                writer_queue.submit(
                    lambda session, idx=index: session.add(
                        TokenStore(
                            env=f"env-{idx}",
                            issued_at=utc_now(),
                            expires_at=utc_now(),
                            is_valid=True,
                        )
                    ),
                    description=f"insert-{index}",
                )
            )
        for future in futures:
            future.result()
    finally:
        writer_queue.stop()

    with get_read_session() as session:
        rows = session.query(TokenStore).order_by(TokenStore.env).all()

    assert [row.env for row in rows] == ["env-0", "env-1", "env-2"]


def test_token_manager_persists_only_metadata(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    init_db(settings)
    writer_queue = WriterQueue()
    writer_queue.start()
    api_client = KISApiClient(
        settings=settings,
        session=DummySession({"access_token": "persist-me-in-memory-only", "expires_in": 60}),
    )
    token_manager = TokenManager(writer_queue=writer_queue, api_client=api_client, settings=settings)

    try:
        token = token_manager.get_valid_token()
    finally:
        writer_queue.stop()

    assert token == "persist-me-in-memory-only"
    with get_read_session() as session:
        record = session.query(TokenStore).filter(TokenStore.env == "vts").one()

    assert record.is_valid is True
    assert not hasattr(record, "token")


def test_kis_client_uses_expected_base_url_and_headers(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    dummy_session = DummySession({"rt_cd": "0", "msg1": "ok"})
    client = KISApiClient(settings=settings, session=dummy_session)

    client.request("GET", "/hello", access_token="abc")

    call = dummy_session.calls[0]
    assert call["url"] == "https://example.test:29443/hello"
    assert call["headers"]["authorization"] == "Bearer abc"
    assert call["headers"]["appkey"] == "key12345"


def test_kis_client_builds_polling_snapshot(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = KISApiClient(settings=settings, session=DummySession({"rt_cd": "0"}))

    snapshot = client.build_polling_snapshot(
        account_payload={"output1": [{"pdno": "005930", "hldg_qty": "2", "pchs_avg_pric": "70000"}]},
        open_orders_payload={"output": [{"ODNO": "A1", "PDNO": "005930", "ord_qty": "2", "ord_psbl_qty": "1"}]},
        cash_payload={"output": {"ord_psbl_cash": "150000"}},
    )

    assert snapshot.cash_available == 150000
    assert snapshot.positions[0].ticker == "005930"
    assert snapshot.open_orders[0].order_no == "A1"

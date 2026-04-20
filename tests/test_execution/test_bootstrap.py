from __future__ import annotations

import sqlite3
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
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class DummySession:
    def __init__(self, payload: dict | list[dict], status_code: int | list[int] = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.calls: list[dict] = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls) - 1
        payload = self.payload[index] if isinstance(self.payload, list) else self.payload
        status_code = self.status_code[index] if isinstance(self.status_code, list) else self.status_code
        return DummyResponse(payload, status_code=status_code)


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


def test_kis_client_supplies_domestic_balance_query_contract(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    dummy_session = DummySession({"rt_cd": "0", "output1": []})
    client = KISApiClient(settings=settings, session=dummy_session)

    client.get_account_snapshot("abc")

    call = dummy_session.calls[0]
    assert call["url"] == "https://example.test:29443/uapi/domestic-stock/v1/trading/inquire-balance"
    assert call["headers"]["tr_id"] == "VTTC8434R"
    assert call["params"]["CANO"] == "12345678"
    assert call["params"]["ACNT_PRDT_CD"] == "01"
    assert call["params"]["INQR_DVSN"] == "02"
    assert call["params"]["PRCS_DVSN"] == "01"


def test_kis_client_supplies_domestic_cash_query_contract(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    dummy_session = DummySession({"rt_cd": "0", "output": {"ord_psbl_cash": "150000"}})
    client = KISApiClient(settings=settings, session=dummy_session)

    client.get_cash_balance("abc")

    call = dummy_session.calls[0]
    assert call["url"] == "https://example.test:29443/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    assert call["headers"]["tr_id"] == "VTTC8908R"
    assert call["params"]["CANO"] == "12345678"
    assert call["params"]["ACNT_PRDT_CD"] == "01"
    assert call["params"]["PDNO"] == "005930"
    assert call["params"]["ORD_UNPR"] == "65500"
    assert call["params"]["ORD_DVSN"] == "01"
    assert call["params"]["CMA_EVLU_AMT_ICLD_YN"] == "Y"
    assert call["params"]["OVRS_ICLD_YN"] == "Y"


def test_kis_client_supplies_domestic_open_orders_query_contract(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    dummy_session = DummySession({"rt_cd": "0", "output": []})
    client = KISApiClient(settings=settings, session=dummy_session)

    client.list_open_orders("abc")

    call = dummy_session.calls[0]
    assert call["url"] == "https://example.test:29443/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
    assert call["headers"]["tr_id"] == "VTTC8036R"
    assert call["params"]["CANO"] == "12345678"
    assert call["params"]["ACNT_PRDT_CD"] == "01"
    assert call["params"]["INQR_DVSN_1"] == "0"
    assert call["params"]["INQR_DVSN_2"] == "0"


def test_kis_client_returns_empty_open_orders_when_vts_endpoint_is_unsupported(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    dummy_session = DummySession(
        {
            "rt_cd": "1",
            "msg_cd": "OPSQ0001",
            "msg1": "모의투자에서는 해당업무가 제공되지 않습니다.",
        }
    )
    client = KISApiClient(settings=settings, session=dummy_session)

    payload = client.list_open_orders("abc")

    assert payload == {"output": []}


def test_kis_client_supplies_domestic_daily_fill_query_contract(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    dummy_session = DummySession({"rt_cd": "0", "output1": []})
    client = KISApiClient(settings=settings, session=dummy_session)

    client.list_daily_order_fills("abc", market="KR", ticker="005930")

    call = dummy_session.calls[0]
    assert call["url"] == "https://example.test:29443/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    assert call["headers"]["tr_id"] == "VTTC0081R"
    assert call["headers"]["custtype"] == "P"
    assert call["params"]["CANO"] == "12345678"
    assert call["params"]["ACNT_PRDT_CD"] == "01"
    assert call["params"]["PDNO"] == "005930"
    assert call["params"]["CCLD_DVSN"] == "00"
    assert call["params"]["INQR_DVSN"] == "00"
    assert call["params"]["INQR_DVSN_3"] == "00"
    assert call["params"]["SLL_BUY_DVSN_CD"] == "00"


def test_kis_client_supplies_domestic_submit_order_contract(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    dummy_session = DummySession(
        [
            {"HASH": "hash-value"},
            {"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "ok", "output": {"ODNO": "12345"}},
        ]
    )
    client = KISApiClient(settings=settings, session=dummy_session)

    client.submit_order(
        {
            "ticker": "005930",
            "side": "buy",
            "quantity": 1,
            "order_type": "limit",
            "price": 55000,
        },
        access_token="abc",
    )

    hash_call = dummy_session.calls[0]
    order_call = dummy_session.calls[1]
    assert hash_call["url"] == "https://example.test:29443/uapi/hashkey"
    assert hash_call["json"]["PDNO"] == "005930"
    assert order_call["url"] == "https://example.test:29443/uapi/domestic-stock/v1/trading/order-cash"
    assert order_call["headers"]["tr_id"] == "VTTC0012U"
    assert order_call["headers"]["custtype"] == "P"
    assert order_call["headers"]["hashkey"] == "hash-value"
    assert order_call["json"]["CANO"] == "12345678"
    assert order_call["json"]["ACNT_PRDT_CD"] == "01"
    assert order_call["json"]["PDNO"] == "005930"
    assert order_call["json"]["ORD_DVSN"] == "00"
    assert order_call["json"]["ORD_QTY"] == "1"
    assert order_call["json"]["ORD_UNPR"] == "55000"


def test_kis_client_supplies_domestic_cancel_order_contract(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    dummy_session = DummySession(
        [
            {"HASH": "hash-value"},
            {"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "ok", "output": {"ODNO": "12345"}},
        ]
    )
    client = KISApiClient(settings=settings, session=dummy_session)

    client.cancel_order(
        {
            "order_orgno": "06010",
            "order_no": "0001234567",
            "order_division": "00",
            "qty_all_ord_yn": "Y",
        },
        access_token="abc",
    )

    hash_call = dummy_session.calls[0]
    cancel_call = dummy_session.calls[1]
    assert hash_call["url"] == "https://example.test:29443/uapi/hashkey"
    assert cancel_call["url"] == "https://example.test:29443/uapi/domestic-stock/v1/trading/order-rvsecncl"
    assert cancel_call["headers"]["tr_id"] == "VTTC0013U"
    assert cancel_call["headers"]["custtype"] == "P"
    assert cancel_call["headers"]["hashkey"] == "hash-value"
    assert cancel_call["json"]["CANO"] == "12345678"
    assert cancel_call["json"]["ACNT_PRDT_CD"] == "01"
    assert cancel_call["json"]["KRX_FWDG_ORD_ORGNO"] == "06010"
    assert cancel_call["json"]["ORGN_ODNO"] == "0001234567"
    assert cancel_call["json"]["RVSE_CNCL_DVSN_CD"] == "02"
    assert cancel_call["json"]["ORD_QTY"] == "0"
    assert cancel_call["json"]["ORD_UNPR"] == "0"
    assert cancel_call["json"]["QTY_ALL_ORD_YN"] == "Y"


def test_kis_client_allows_domestic_order_tr_id_override(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    dummy_session = DummySession(
        [
            {"HASH": "hash-value"},
            {"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "ok", "output": {"ODNO": "12345"}},
        ]
    )
    client = KISApiClient(settings=settings, session=dummy_session)

    client.submit_order(
        {
            "ticker": "005930",
            "side": "buy",
            "quantity": 1,
            "order_type": "limit",
            "price": 55000,
            "tr_id": "VTTC0802U",
        },
        access_token="abc",
    )

    order_call = dummy_session.calls[1]
    assert order_call["headers"]["tr_id"] == "VTTC0802U"


def test_kis_client_omits_hashkey_when_hash_request_fails(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    dummy_session = DummySession(
        [
            {"msg_cd": "ERR500", "msg1": "hash failed"},
            {"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "ok", "output": {"ODNO": "12345"}},
        ],
        status_code=[500, 200],
    )
    client = KISApiClient(settings=settings, session=dummy_session)

    client.submit_order(
        {
            "ticker": "005930",
            "side": "sell",
            "quantity": 1,
            "order_type": "market",
            "price": 0,
        },
        access_token="abc",
    )

    order_call = dummy_session.calls[1]
    assert order_call["headers"]["tr_id"] == "VTTC0011U"
    assert "hashkey" not in order_call["headers"]


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


def test_kis_client_normalizes_us_polling_fields_with_sample_fallbacks(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = KISApiClient(settings=settings, session=DummySession({"rt_cd": "0"}))

    snapshot = client.build_polling_snapshot(
        account_payload={
            "output1": [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_cblc_qty": "5",
                    "ovrs_pchs_avg_pric": "180.5",
                    "OVRS_EXCG_CD": "NASD",
                    "crcy_cd": "USD",
                }
            ]
        },
        open_orders_payload={
            "output1": [
                {
                    "odno": "US-1",
                    "ovrs_pdno": "AAPL",
                    "ovrs_excg_cd": "NASD",
                    "ord_qty": "7",
                    "nccs_qty": "2",
                    "ovrs_ord_unpr": "185.0",
                    "side": "buy",
                }
            ]
        },
        cash_payload={"output": {"ovrs_ord_psbl_amt": "2500"}},
        default_market="US",
        default_currency="USD",
    )

    assert snapshot.cash_available == 2500
    assert snapshot.positions[0].market == "US"
    assert snapshot.positions[0].avg_cost == 180.5
    assert snapshot.open_orders[0].order_no == "US-1"
    assert snapshot.open_orders[0].remaining_quantity == 2


def test_kis_client_normalizes_order_result(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = KISApiClient(settings=settings, session=DummySession({"rt_cd": "0"}))

    result = client.normalize_order_result(
        {
            "rt_cd": "0",
            "msg_cd": "APBK0013",
            "msg1": "ok",
            "output": {"ODNO": "12345"},
        }
    )

    assert result.accepted is True
    assert result.broker_order_no == "12345"


def test_kis_client_normalizes_broker_order_orgno(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = KISApiClient(settings=settings, session=DummySession({"rt_cd": "0"}))

    result = client.normalize_order_result(
        {
            "rt_cd": "0",
            "msg_cd": "APBK0013",
            "msg1": "ok",
            "output": {"KRX_FWDG_ORD_ORGNO": "06010", "ODNO": "12345"},
        }
    )

    assert result.accepted is True
    assert result.broker_order_no == "12345"
    assert result.broker_order_orgno == "06010"


def test_kis_client_normalizes_daily_fill_rows(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = KISApiClient(settings=settings, session=DummySession({"rt_cd": "0"}))

    result = client.normalize_daily_order_fills(
        {
            "output1": [
                {
                    "odno": "12345",
                    "ord_orgno": "06010",
                    "pdno": "005930",
                    "sll_buy_dvsn_cd": "02",
                    "ord_qty": "3",
                    "tot_ccld_qty": "2",
                    "rmn_qty": "1",
                    "avg_prvs": "70100",
                    "ord_dt": "20260420",
                    "infm_tmd": "091501",
                }
            ]
        }
    )

    assert len(result) == 1
    snapshot = result[0]
    assert snapshot.order_no == "12345"
    assert snapshot.order_orgno == "06010"
    assert snapshot.ticker == "005930"
    assert snapshot.side == "buy"
    assert snapshot.order_quantity == 3
    assert snapshot.cumulative_filled_quantity == 2
    assert snapshot.remaining_quantity == 1
    assert snapshot.average_filled_price == 70100.0


def test_kis_client_normalizes_failed_order_result(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = KISApiClient(settings=settings, session=DummySession({"rt_cd": "0"}))

    result = client.normalize_order_result(
        {
            "rt_cd": "1",
            "msg_cd": "ERR001",
            "msg1": "failed",
        }
    )

    assert result.accepted is False
    assert result.error_code == "ERR001"
    assert result.error_message == "failed"


def test_kis_client_includes_response_body_excerpt_on_http_error(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = KISApiClient(
        settings=settings,
        session=DummySession({"msg_cd": "ERR500", "msg1": "server error"}, status_code=500),
    )

    try:
        client.request("GET", "/broken", access_token="abc")
    except Exception as exc:
        message = str(exc)
    else:
        raise AssertionError("expected exception")

    assert "status" in message
    assert "server error" in message


def test_migration_adds_kis_order_orgno_column_and_index(tmp_path: Path) -> None:
    from scripts.migrate_v20260420 import migrate

    settings = build_settings(tmp_path)
    db_path = settings.database.absolute_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_order_id TEXT NOT NULL UNIQUE,
                kis_order_no TEXT,
                signal_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                market TEXT NOT NULL,
                strategy TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER NOT NULL DEFAULT 0,
                submitted_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT
            )
            """
        )
        connection.execute("CREATE INDEX idx_orders_kis_order_no ON orders (kis_order_no)")
        connection.commit()

    migrate(settings)

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(orders)")}

    assert "kis_order_orgno" in columns
    assert "idx_orders_kis_order_orgno" in indexes

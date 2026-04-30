from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import requests

from core.exceptions import AuthenticationError, BrokerApiError
from core.models import BrokerFillSnapshot, BrokerOrderResult, BrokerOrderSnapshot, BrokerPollingSnapshot, BrokerPositionSnapshot
from core.settings import RuntimeEnv, Settings, get_settings


KST = timezone(timedelta(hours=9))


def _mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def _secret_value(value: Any) -> str:
    if hasattr(value, "get_secret_value"):
        return str(value.get_secret_value())  # type: ignore[call-arg]
    return str(value)


class RateLimiter:
    def __init__(self, rate_limit_per_sec: int) -> None:
        self._rate_limit = rate_limit_per_sec
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._timestamps = [ts for ts in self._timestamps if now - ts < 1.0]
                if len(self._timestamps) < self._rate_limit:
                    self._timestamps.append(now)
                    return
                sleep_for = 1.0 - (now - self._timestamps[0])
            if sleep_for > 0:
                time.sleep(sleep_for)


class KISApiClient:
    _VTS_UNSUPPORTED_OPEN_ORDERS_MARKER = "모의투자에서는 해당업무가 제공되지 않습니다."
    _RETRYABLE_ERROR_MARKERS = (
        "egw00201",
        "초당 거래건수를 초과하였습니다",
        "rate limit",
        "throttle",
        "timeout",
        "temporarily",
        "temporary",
        "retry later",
    )

    def __init__(self, settings: Settings | None = None, session: requests.Session | None = None) -> None:
        self.settings = settings or get_settings()
        self.env = self.settings.env
        self.endpoint = self.settings.kis.endpoint_for(self.env)
        self.credentials = self.settings.kis.credentials
        if self.credentials is None:
            raise AuthenticationError("KIS credentials are not configured")
        self.session = session or requests.Session()
        self.rate_limiter = RateLimiter(self.settings.kis.rate_limit_per_sec)

    def request(
        self,
        method: str,
        path: str,
        *,
        auth_required: bool = True,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        self.rate_limiter.acquire()
        merged_headers = {
            "content-type": "application/json; charset=utf-8",
            "appkey": self.credentials.app_key.get_secret_value(),
            "appsecret": self.credentials.app_secret.get_secret_value(),
        }
        if headers:
            merged_headers.update(headers)
        if auth_required:
            if not access_token:
                raise AuthenticationError("Authenticated request requires an access token")
            merged_headers["authorization"] = f"Bearer {access_token}"

        url = f"{self.endpoint.rest_base_url.rstrip('/')}/{path.lstrip('/')}"
        response = self.session.request(
            method=method.upper(),
            url=url,
            params=params,
            json=json,
            headers=merged_headers,
            timeout=self.settings.kis.request_timeout_sec,
        )
        if not response.ok:
            response_text = getattr(response, "text", "") or ""
            detail = ""
            if response_text:
                detail = f": {response_text[:300]}"
            raise BrokerApiError(
                f"KIS API request failed with status {response.status_code}{detail}",
                status_code=response.status_code,
            )

        payload = response.json()
        if isinstance(payload, dict):
            rt_cd = payload.get("rt_cd")
            if rt_cd not in (None, "0", 0):
                raise BrokerApiError(str(payload.get("msg1") or "KIS API returned an error"))
        return payload

    def request_access_token(self) -> dict[str, Any]:
        body = {
            "grant_type": "client_credentials",
            "appkey": self.credentials.app_key.get_secret_value(),
            "appsecret": self.credentials.app_secret.get_secret_value(),
        }
        return self.request("POST", self.endpoint.token_path, auth_required=False, json=body)

    def list_open_orders(self, access_token: str) -> dict[str, Any]:
        try:
            return self.request(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
                params=self._domestic_open_orders_params(),
                headers={"tr_id": self._domestic_open_orders_tr_id()},
                access_token=access_token,
            )
        except BrokerApiError as exc:
            if self._is_vts_domestic_open_orders_unsupported(exc):
                return {"output": []}
            raise

    def get_account_snapshot(self, access_token: str) -> dict[str, Any]:
        return self.request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            params=self._domestic_balance_params(),
            headers={"tr_id": self._domestic_balance_tr_id()},
            access_token=access_token,
        )

    def submit_order(self, payload: dict[str, Any], access_token: str | None = None) -> dict[str, Any]:
        body = self._domestic_order_payload(payload)
        headers = {
            "tr_id": self._domestic_submit_tr_id(payload),
            "custtype": "P",
        }
        hashkey = self.request_hashkey(body, access_token=access_token)
        if hashkey:
            headers["hashkey"] = hashkey
        return self.request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            json=body,
            headers=headers,
            access_token=access_token,
        )

    def cancel_order(self, payload: dict[str, Any], access_token: str | None = None) -> dict[str, Any]:
        body = self._domestic_cancel_payload(payload)
        headers = {
            "tr_id": self._domestic_cancel_tr_id(payload),
            "custtype": "P",
        }
        hashkey = self.request_hashkey(body, access_token=access_token)
        if hashkey:
            headers["hashkey"] = hashkey
        return self.request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-rvsecncl",
            json=body,
            headers=headers,
            access_token=access_token,
        )

    def get_cash_balance(self, access_token: str) -> dict[str, Any]:
        return self.request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            params=self._domestic_cash_balance_params(),
            headers={"tr_id": self._domestic_cash_balance_tr_id()},
            access_token=access_token,
        )

    def list_daily_order_fills(
        self,
        access_token: str,
        *,
        market: str,
        start_date: str | None = None,
        end_date: str | None = None,
        order_no: str = "",
        order_orgno: str = "",
        ticker: str = "",
        side_code: str = "00",
    ) -> dict[str, Any]:
        market_code = market.upper()
        if market_code != "KR":
            raise BrokerApiError(f"daily order fill inquiry is not implemented for market={market_code}")
        return self.request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            params=self._domestic_daily_fill_params(
                start_date=start_date,
                end_date=end_date,
                order_no=order_no,
                order_orgno=order_orgno,
                ticker=ticker,
                side_code=side_code,
            ),
            headers={"tr_id": self._domestic_daily_fill_tr_id(), "custtype": "P"},
            access_token=access_token,
        )

    def get_daily_price_history(
        self,
        access_token: str,
        *,
        ticker: str,
        start_date: str,
        end_date: str,
        period_code: str = "D",
        adjusted_price: bool = False,
    ) -> dict[str, Any]:
        return self.request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            params=self._domestic_daily_price_params(
                ticker=ticker,
                start_date=start_date,
                end_date=end_date,
                period_code=period_code,
                adjusted_price=adjusted_price,
            ),
            headers={"tr_id": self._domestic_daily_price_tr_id(), "custtype": "P"},
            access_token=access_token,
        )

    def get_intraday_price_history(
        self,
        access_token: str,
        *,
        ticker: str,
        input_hour: str,
        include_past_data: bool = True,
    ) -> dict[str, Any]:
        return self.request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            params=self._domestic_intraday_price_params(
                ticker=ticker,
                input_hour=input_hour,
                include_past_data=include_past_data,
            ),
            headers={"tr_id": self._domestic_intraday_price_tr_id(), "custtype": "P"},
            access_token=access_token,
        )

    def request_hashkey(self, payload: dict[str, Any], *, access_token: str | None = None) -> str | None:
        try:
            response = self.request(
                "POST",
                "/uapi/hashkey",
                auth_required=access_token is not None,
                json=payload,
                access_token=access_token,
            )
        except BrokerApiError:
            return None

        hash_value = response.get("HASH") or response.get("hash")
        if hash_value in (None, ""):
            return None
        return str(hash_value)

    def normalize_open_orders(self, payload: dict[str, Any], *, default_market: str = "KR") -> list[BrokerOrderSnapshot]:
        rows = payload.get("output") or payload.get("output1") or []
        normalized: list[BrokerOrderSnapshot] = []
        for row in rows:
            order_no = str(row.get("ODNO") or row.get("odno") or row.get("order_no") or "")
            if not order_no:
                continue
            ticker = str(row.get("PDNO") or row.get("pdno") or row.get("ovrs_pdno") or row.get("ticker") or "")
            side_raw = str(
                row.get("SLL_BUY_DVSN_CD")
                or row.get("sll_buy_dvsn_cd")
                or row.get("rvse_cncl_dvsn_name")
                or row.get("side")
                or "buy"
            ).lower()
            side = "sell" if side_raw in {"01", "sell", "s"} else "buy"
            quantity = int(float(row.get("ORD_QTY") or row.get("ord_qty") or row.get("quantity") or 0))
            remaining_quantity = int(
                float(
                    row.get("ORD_PSBL_QTY")
                    or row.get("ord_psbl_qty")
                    or row.get("remaining_quantity")
                    or row.get("unfilled_qty")
                    or row.get("nccs_qty")
                    or quantity
                )
            )
            price_raw = row.get("ORD_UNPR") or row.get("ord_unpr") or row.get("ovrs_ord_unpr") or row.get("price")
            market = (
                row.get("OVRS_EXCG_CD")
                or row.get("ovrs_excg_cd")
                or row.get("market")
                or default_market
            )
            normalized.append(
                BrokerOrderSnapshot(
                    order_no=order_no,
                    ticker=ticker,
                    market=self._normalize_market(str(market), default_market=default_market),
                    side=side,
                    quantity=quantity,
                    remaining_quantity=remaining_quantity,
                    status=str(
                        row.get("status")
                        or row.get("ord_st") 
                        or row.get("ord_tmd")
                        or row.get("ord_gno_brno")
                        or "submitted"
                    ),
                    price=None if price_raw in (None, "") else float(price_raw),
                )
            )
        return normalized

    def normalize_positions(
        self,
        payload: dict[str, Any],
        *,
        default_market: str = "KR",
        default_currency: str = "KRW",
    ) -> list[BrokerPositionSnapshot]:
        rows = payload.get("output") or payload.get("output1") or []
        normalized: list[BrokerPositionSnapshot] = []
        for row in rows:
            ticker = str(row.get("pdno") or row.get("ticker") or row.get("ovrs_pdno") or "")
            if not ticker:
                continue
            quantity = int(float(row.get("hldg_qty") or row.get("quantity") or row.get("ovrs_cblc_qty") or 0))
            avg_cost = float(
                row.get("pchs_avg_pric")
                or row.get("avg_cost")
                or row.get("pchs_avg_pric_amt")
                or row.get("ovrs_pchs_avg_pric")
                or row.get("ovrs_now_pric1")
                or 0
            )
            market = row.get("market") or row.get("OVRS_EXCG_CD") or row.get("ovrs_excg_cd") or default_market
            currency = row.get("currency") or row.get("crcy_cd") or default_currency
            normalized.append(
                BrokerPositionSnapshot(
                    ticker=ticker,
                    market=self._normalize_market(str(market), default_market=default_market),
                    quantity=quantity,
                    avg_cost=avg_cost,
                    currency=str(currency),
                    snapshot_at=time_to_utc_now(),
                    source_env=self.env.value,
                )
            )
        return normalized

    def normalize_cash_available(self, payload: dict[str, Any]) -> float:
        output = payload.get("output") or payload.get("output1") or payload
        if isinstance(output, list):
            output = output[0] if output else {}
        return float(
            output.get("ord_psbl_cash")
            or output.get("cash_available")
            or output.get("dnca_tot_amt")
            or output.get("ovrs_ord_psbl_amt")
            or output.get("frcr_ord_psbl_amt1")
            or 0
        )

    def normalize_order_result(self, payload: dict[str, Any]) -> BrokerOrderResult:
        accepted = str(payload.get("rt_cd")) == "0"
        output = payload.get("output") or {}
        broker_order_no = output.get("ODNO") or output.get("odno") or output.get("order_no")
        broker_order_orgno = (
            output.get("KRX_FWDG_ORD_ORGNO")
            or output.get("krx_fwdg_ord_orgno")
            or output.get("order_orgno")
        )
        return BrokerOrderResult(
            accepted=accepted,
            broker_order_no=None if broker_order_no in (None, "") else str(broker_order_no),
            broker_order_orgno=None if broker_order_orgno in (None, "") else str(broker_order_orgno),
            error_code=None if accepted else str(payload.get("msg_cd") or ""),
            error_message=None if accepted else str(payload.get("msg1") or ""),
            raw_payload=payload,
        )

    def normalize_cancel_result(self, payload: dict[str, Any]) -> BrokerOrderResult:
        return self.normalize_order_result(payload)

    def normalize_daily_order_fills(
        self,
        payload: dict[str, Any],
        *,
        default_market: str = "KR",
    ) -> list[BrokerFillSnapshot]:
        rows = payload.get("output1") or payload.get("output") or []
        normalized: list[BrokerFillSnapshot] = []
        for row in rows:
            order_no = str(row.get("odno") or row.get("ODNO") or row.get("order_no") or "")
            if not order_no:
                continue
            ticker = str(row.get("pdno") or row.get("PDNO") or row.get("ticker") or "")
            side_raw = str(row.get("sll_buy_dvsn_cd") or row.get("SLL_BUY_DVSN_CD") or "").strip().lower()
            if side_raw in {"01", "sell", "s"}:
                side = "sell"
            elif side_raw in {"02", "buy", "b"}:
                side = "buy"
            else:
                continue
            order_quantity = int(float(row.get("ord_qty") or row.get("ORD_QTY") or 0))
            remaining_quantity = int(float(row.get("rmn_qty") or row.get("RMN_QTY") or row.get("nccs_qty") or 0))
            cumulative_filled_quantity = int(
                float(
                    row.get("tot_ccld_qty")
                    or row.get("TOT_CCLD_QTY")
                    or row.get("ccld_qty")
                    or row.get("CCLD_QTY")
                    or max(order_quantity - remaining_quantity, 0)
                )
            )
            average_filled_price_raw = (
                row.get("avg_prvs")
                or row.get("AVG_PRVS")
                or row.get("avg_ccld_unpr")
                or row.get("AVG_CCLD_UNPR")
                or row.get("avg_unpr")
                or row.get("AVG_UNPR")
            )
            ordered_at = self._row_timestamp_to_utc(
                row,
                date_key_candidates=("ord_dt", "ORD_DT"),
                time_key_candidates=("infm_tmd", "INFM_TMD", "ord_tmd", "ORD_TMD"),
            )
            normalized.append(
                BrokerFillSnapshot(
                    order_no=order_no,
                    order_orgno=self._optional_str(
                        row.get("ord_orgno")
                        or row.get("ORD_ORGNO")
                        or row.get("ord_gno_brno")
                        or row.get("ORD_GNO_BRNO")
                    ),
                    ticker=ticker,
                    market=self._normalize_market(default_market, default_market=default_market),
                    side=side,
                    order_quantity=order_quantity,
                    cumulative_filled_quantity=cumulative_filled_quantity,
                    remaining_quantity=remaining_quantity,
                    average_filled_price=None if average_filled_price_raw in (None, "") else float(average_filled_price_raw),
                    occurred_at=ordered_at,
                    execution_hint=self._optional_str(
                        row.get("exec_no")
                        or row.get("EXEC_NO")
                        or row.get("ccld_no")
                        or row.get("CCLD_NO")
                    ),
                )
            )
        return normalized

    def normalize_daily_price_history(
        self,
        payload: dict[str, Any],
        *,
        ticker: str,
        default_market: str = "KR",
    ) -> list[dict[str, Any]]:
        rows = payload.get("output2") or payload.get("output1") or payload.get("output") or []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            trade_date = row.get("stck_bsop_date") or row.get("STCK_BSOP_DATE") or row.get("date")
            close = row.get("stck_clpr") or row.get("STCK_CLPR") or row.get("close")
            high = row.get("stck_hgpr") or row.get("STCK_HGPR") or row.get("high")
            low = row.get("stck_lwpr") or row.get("STCK_LWPR") or row.get("low")
            if trade_date in (None, "") or close in (None, ""):
                continue
            normalized.append(
                {
                    "ticker": ticker,
                    "market": self._normalize_market(default_market, default_market=default_market),
                    "timestamp": datetime.strptime(str(trade_date), "%Y%m%d").replace(tzinfo=UTC),
                    "close": float(close),
                    "high": None if high in (None, "") else float(high),
                    "low": None if low in (None, "") else float(low),
                }
            )
        normalized.sort(key=lambda row: row["timestamp"])
        return normalized

    def normalize_intraday_price_history(
        self,
        payload: dict[str, Any],
        *,
        ticker: str,
        default_market: str = "KR",
    ) -> list[dict[str, Any]]:
        rows = payload.get("output2") or payload.get("output1") or payload.get("output") or []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            trade_date = row.get("stck_bsop_date") or row.get("STCK_BSOP_DATE") or row.get("date")
            trade_time = (
                row.get("stck_cntg_hour")
                or row.get("STCK_CNTG_HOUR")
                or row.get("cntg_hour")
                or row.get("time")
            )
            close = row.get("stck_prpr") or row.get("STCK_PRPR") or row.get("close")
            open_price = row.get("stck_oprc") or row.get("STCK_OPRC") or row.get("open")
            high = row.get("stck_hgpr") or row.get("STCK_HGPR") or row.get("high")
            low = row.get("stck_lwpr") or row.get("STCK_LWPR") or row.get("low")
            volume = row.get("cntg_vol") or row.get("CNTG_VOL") or row.get("acml_vol") or row.get("volume")
            if trade_date in (None, "") or trade_time in (None, ""):
                continue
            if any(value in (None, "") for value in (open_price, high, low, close, volume)):
                continue
            timestamp = self._domestic_intraday_timestamp_to_utc(str(trade_date), str(trade_time))
            normalized.append(
                {
                    "ticker": ticker,
                    "market": self._normalize_market(default_market, default_market=default_market),
                    "timestamp": timestamp,
                    "open": float(open_price),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume": int(float(volume)),
                }
            )
        normalized.sort(key=lambda row: row["timestamp"])
        return normalized

    def build_polling_snapshot(
        self,
        *,
        account_payload: dict[str, Any],
        open_orders_payload: dict[str, Any],
        cash_payload: dict[str, Any],
        default_market: str = "KR",
        default_currency: str = "KRW",
    ) -> BrokerPollingSnapshot:
        return BrokerPollingSnapshot(
            positions=self.normalize_positions(
                account_payload,
                default_market=default_market,
                default_currency=default_currency,
            ),
            open_orders=self.normalize_open_orders(open_orders_payload, default_market=default_market),
            cash_available=self.normalize_cash_available(cash_payload),
            raw_payloads={
                "account": account_payload,
                "open_orders": open_orders_payload,
                "cash": cash_payload,
            },
        )

    def describe_environment(self) -> dict[str, str]:
        return {
            "env": self.env.value if isinstance(self.env, RuntimeEnv) else str(self.env),
            "rest_base_url": self.endpoint.rest_base_url,
            "websocket_base_url": self.endpoint.websocket_base_url,
            "app_key_masked": _mask_secret(self.credentials.app_key.get_secret_value()),
        }

    @classmethod
    def is_retryable_broker_error(cls, exc: Exception) -> bool:
        if not isinstance(exc, BrokerApiError):
            return False
        if exc.status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
            return True
        message = str(exc).lower()
        return any(marker in message for marker in cls._RETRYABLE_ERROR_MARKERS)

    @staticmethod
    def _normalize_market(raw_market: str, *, default_market: str) -> str:
        market = raw_market.upper()
        if market in {"NASD", "NYSE", "AMEX", "US"}:
            return "US"
        if market in {"KRX", "KR"}:
            return "KR"
        return default_market

    def _account_base_params(self) -> dict[str, str]:
        return {
            "CANO": _secret_value(self.credentials.account_no),
            "ACNT_PRDT_CD": _secret_value(self.credentials.product_code),
        }

    def _domestic_open_orders_params(self) -> dict[str, str]:
        return {
            **self._account_base_params(),
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "INQR_DVSN_1": "0",
            "INQR_DVSN_2": "0",
        }

    def _domestic_balance_params(self) -> dict[str, str]:
        return {
            **self._account_base_params(),
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

    def _domestic_cash_balance_params(self) -> dict[str, str]:
        return {
            **self._account_base_params(),
            "PDNO": "005930",
            "ORD_UNPR": "65500",
            "ORD_DVSN": "01",
            "CMA_EVLU_AMT_ICLD_YN": "Y",
            "OVRS_ICLD_YN": "Y",
        }

    def _domestic_daily_fill_params(
        self,
        *,
        start_date: str | None,
        end_date: str | None,
        order_no: str,
        order_orgno: str,
        ticker: str,
        side_code: str,
    ) -> dict[str, str]:
        today = time_to_utc_now().astimezone().strftime("%Y%m%d")
        return {
            **self._account_base_params(),
            "INQR_STRT_DT": start_date or today,
            "INQR_END_DT": end_date or today,
            "SLL_BUY_DVSN_CD": side_code,
            "PDNO": ticker,
            "CCLD_DVSN": "00",
            "INQR_DVSN": "00",
            "INQR_DVSN_3": "00",
            "ORD_GNO_BRNO": order_orgno,
            "ODNO": order_no,
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

    def _domestic_daily_price_params(
        self,
        *,
        ticker: str,
        start_date: str,
        end_date: str,
        period_code: str,
        adjusted_price: bool,
    ) -> dict[str, str]:
        return {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": str(ticker),
            "FID_INPUT_DATE_1": str(start_date),
            "FID_INPUT_DATE_2": str(end_date),
            "FID_PERIOD_DIV_CODE": str(period_code),
            "FID_ORG_ADJ_PRC": "1" if adjusted_price else "0",
        }

    def _domestic_intraday_price_params(
        self,
        *,
        ticker: str,
        input_hour: str,
        include_past_data: bool,
    ) -> dict[str, str]:
        if not ticker:
            raise BrokerApiError("domestic intraday price inquiry requires ticker")
        if not input_hour:
            raise BrokerApiError("domestic intraday price inquiry requires input_hour")
        return {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": str(ticker),
            "FID_INPUT_HOUR_1": str(input_hour),
            "FID_PW_DATA_INCU_YN": "Y" if include_past_data else "N",
        }

    def _domestic_order_payload(self, payload: dict[str, Any]) -> dict[str, str]:
        ticker = payload.get("PDNO") or payload.get("ticker")
        quantity = payload.get("ORD_QTY") if "ORD_QTY" in payload else payload.get("quantity")
        order_division = payload.get("ORD_DVSN") or self._normalize_domestic_order_division(payload.get("order_type"))
        price = payload.get("ORD_UNPR") if "ORD_UNPR" in payload else payload.get("price")

        if not ticker:
            raise BrokerApiError("domestic order requires ticker")
        if quantity in (None, ""):
            raise BrokerApiError("domestic order requires quantity")
        if order_division in (None, ""):
            raise BrokerApiError("domestic order requires order division")
        if price in (None, ""):
            price = 0 if str(order_division) == "01" else None
        if price in (None, ""):
            raise BrokerApiError("domestic order requires price")

        return {
            **self._account_base_params(),
            "PDNO": str(ticker),
            "ORD_DVSN": str(order_division),
            "ORD_QTY": self._stringify_whole_number(quantity),
            "ORD_UNPR": self._stringify_whole_number(price),
        }

    def _domestic_cancel_payload(self, payload: dict[str, Any]) -> dict[str, str]:
        order_orgno = (
            payload.get("KRX_FWDG_ORD_ORGNO")
            or payload.get("order_orgno")
            or payload.get("order_branch_no")
        )
        order_no = payload.get("ORGN_ODNO") or payload.get("order_no")
        order_division = payload.get("ORD_DVSN") or payload.get("order_division") or "00"
        cancel_division = payload.get("RVSE_CNCL_DVSN_CD") or payload.get("rvse_cncl_dvsn_cd") or "02"
        qty_all = str(payload.get("QTY_ALL_ORD_YN") or payload.get("qty_all_ord_yn") or "Y")
        quantity = payload.get("ORD_QTY") if "ORD_QTY" in payload else payload.get("quantity")
        price = payload.get("ORD_UNPR") if "ORD_UNPR" in payload else payload.get("price")

        if not order_orgno:
            raise BrokerApiError("domestic cancel requires order organization number")
        if not order_no:
            raise BrokerApiError("domestic cancel requires original order number")
        if quantity in (None, ""):
            quantity = 0 if qty_all == "Y" else None
        if price in (None, ""):
            price = 0 if str(cancel_division) == "02" else None
        if quantity in (None, ""):
            raise BrokerApiError("domestic cancel requires quantity when not cancelling the full remaining balance")
        if price in (None, ""):
            raise BrokerApiError("domestic cancel requires price for revise requests")

        return {
            **self._account_base_params(),
            "KRX_FWDG_ORD_ORGNO": str(order_orgno),
            "ORGN_ODNO": str(order_no),
            "ORD_DVSN": str(order_division),
            "RVSE_CNCL_DVSN_CD": str(cancel_division),
            "ORD_QTY": self._stringify_whole_number(quantity),
            "ORD_UNPR": self._stringify_whole_number(price),
            "QTY_ALL_ORD_YN": qty_all,
        }

    def _domestic_submit_tr_id(self, payload: dict[str, Any]) -> str:
        override = payload.get("tr_id")
        if override:
            return str(override)
        side = self._normalize_domestic_side(payload)
        if side == "sell":
            return "VTTC0011U" if self.env == RuntimeEnv.VTS else "TTTC0011U"
        return "VTTC0012U" if self.env == RuntimeEnv.VTS else "TTTC0012U"

    def _domestic_cancel_tr_id(self, payload: dict[str, Any]) -> str:
        override = payload.get("tr_id")
        if override:
            return str(override)
        return "VTTC0013U" if self.env == RuntimeEnv.VTS else "TTTC0013U"

    @staticmethod
    def _normalize_domestic_side(payload: dict[str, Any]) -> str:
        raw = str(payload.get("side") or payload.get("SLL_BUY_DVSN_CD") or "").strip().lower()
        if raw in {"sell", "s", "01"}:
            return "sell"
        if raw in {"buy", "b", "02"}:
            return "buy"
        raise BrokerApiError("domestic order requires side")

    @staticmethod
    def _normalize_domestic_order_division(order_type: Any) -> str | None:
        if order_type is None:
            return None
        raw = str(order_type).strip().lower()
        if raw in {"00", "limit"}:
            return "00"
        if raw in {"01", "market"}:
            return "01"
        return str(order_type)

    @staticmethod
    def _stringify_whole_number(value: Any) -> str:
        if value in (None, ""):
            raise BrokerApiError("domestic broker payload requires a numeric value")
        return str(int(float(value)))

    def _domestic_open_orders_tr_id(self) -> str:
        return "VTTC8036R" if self.env == RuntimeEnv.VTS else "TTTC8036R"

    def _domestic_balance_tr_id(self) -> str:
        return "VTTC8434R" if self.env == RuntimeEnv.VTS else "TTTC8434R"

    def _domestic_cash_balance_tr_id(self) -> str:
        return "VTTC8908R" if self.env == RuntimeEnv.VTS else "TTTC8908R"

    def _domestic_daily_fill_tr_id(self) -> str:
        return "VTTC0081R" if self.env == RuntimeEnv.VTS else "TTTC0081R"

    def _domestic_daily_price_tr_id(self) -> str:
        return "FHKST03010100"

    def _domestic_intraday_price_tr_id(self) -> str:
        return "FHKST03010200"

    def _is_vts_domestic_open_orders_unsupported(self, exc: BrokerApiError) -> bool:
        if self.env != RuntimeEnv.VTS:
            return False
        return self._VTS_UNSUPPORTED_OPEN_ORDERS_MARKER in str(exc)

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value)

    @staticmethod
    def _row_timestamp_to_utc(
        row: dict[str, Any],
        *,
        date_key_candidates: tuple[str, ...],
        time_key_candidates: tuple[str, ...],
    ) -> datetime:
        date_value = next((row.get(key) for key in date_key_candidates if row.get(key) not in (None, "")), None)
        time_value = next((row.get(key) for key in time_key_candidates if row.get(key) not in (None, "")), None)
        if date_value in (None, ""):
            return time_to_utc_now()
        trade_date = str(date_value)
        trade_time = (str(time_value or "000000") + "000000")[:6]
        return datetime.strptime(f"{trade_date}{trade_time}", "%Y%m%d%H%M%S").replace(tzinfo=UTC)

    @staticmethod
    def _domestic_intraday_timestamp_to_utc(trade_date: str, trade_time: str) -> datetime:
        normalized_time = (trade_time + "000000")[:6]
        kst_timestamp = datetime.strptime(f"{trade_date}{normalized_time}", "%Y%m%d%H%M%S").replace(tzinfo=KST)
        return kst_timestamp.astimezone(UTC)


def time_to_utc_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)

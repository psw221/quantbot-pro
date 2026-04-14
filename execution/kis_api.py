from __future__ import annotations

import threading
import time
from typing import Any

import requests

from core.exceptions import AuthenticationError, BrokerApiError
from core.models import BrokerOrderSnapshot, BrokerPollingSnapshot, BrokerPositionSnapshot
from core.settings import RuntimeEnv, Settings, get_settings


def _mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


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
            raise BrokerApiError(
                f"KIS API request failed with status {response.status_code}",
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
        return self.request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
            access_token=access_token,
        )

    def get_account_snapshot(self, access_token: str) -> dict[str, Any]:
        return self.request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            access_token=access_token,
        )

    def submit_order(self, payload: dict[str, Any], access_token: str | None = None) -> dict[str, Any]:
        return self.request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            json=payload,
            access_token=access_token,
        )

    def get_cash_balance(self, access_token: str) -> dict[str, Any]:
        return self.request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            access_token=access_token,
        )

    def normalize_open_orders(self, payload: dict[str, Any], *, default_market: str = "KR") -> list[BrokerOrderSnapshot]:
        rows = payload.get("output") or payload.get("output1") or []
        normalized: list[BrokerOrderSnapshot] = []
        for row in rows:
            order_no = str(row.get("ODNO") or row.get("order_no") or "")
            if not order_no:
                continue
            ticker = str(row.get("PDNO") or row.get("ticker") or "")
            side_raw = str(row.get("sll_buy_dvsn_cd") or row.get("side") or "buy").lower()
            side = "sell" if side_raw in {"02", "sell", "s"} else "buy"
            quantity = int(float(row.get("ord_qty") or row.get("quantity") or 0))
            remaining_quantity = int(
                float(
                    row.get("ord_psbl_qty")
                    or row.get("remaining_quantity")
                    or row.get("unfilled_qty")
                    or quantity
                )
            )
            price_raw = row.get("ord_unpr") or row.get("price")
            normalized.append(
                BrokerOrderSnapshot(
                    order_no=order_no,
                    ticker=ticker,
                    market=str(row.get("market") or default_market),
                    side=side,
                    quantity=quantity,
                    remaining_quantity=remaining_quantity,
                    status=str(row.get("status") or row.get("ord_tmd") or "submitted"),
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
            avg_cost = float(row.get("pchs_avg_pric") or row.get("avg_cost") or row.get("ovrs_now_pric1") or 0)
            normalized.append(
                BrokerPositionSnapshot(
                    ticker=ticker,
                    market=str(row.get("market") or default_market),
                    quantity=quantity,
                    avg_cost=avg_cost,
                    currency=str(row.get("currency") or default_currency),
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
            or 0
        )

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


def time_to_utc_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)

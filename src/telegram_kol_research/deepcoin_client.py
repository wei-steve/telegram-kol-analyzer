"""Deepcoin REST client helpers for authenticated trading requests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from telegram_kol_research.telegram_client import _load_env_file_values


DEEPCOIN_BASE_URL = "https://api.deepcoin.com"
DEEPCOIN_PLACE_ORDER_PATH = "/deepcoin/trade/order"
DEEPCOIN_CANCEL_ORDER_PATH = "/deepcoin/trade/cancel-order"
DEEPCOIN_REPLACE_ORDER_SLTP_PATH = "/deepcoin/trade/replace-order-sltp"
DEEPCOIN_TRIGGER_ORDER_PATH = "/deepcoin/trade/trigger-order"
DEEPCOIN_TRIGGER_ORDERS_PENDING_PATH = "/deepcoin/trade/trigger-orders-pending"
DEEPCOIN_SET_POSITION_SLTP_PATH = "/deepcoin/trade/set-position-sltp"
DEEPCOIN_ACCOUNT_POSITIONS_PATH = "/deepcoin/account/positions"
DEEPCOIN_MARKET_TICKERS_PATH = "/deepcoin/market/tickers"


class DeepcoinClientError(RuntimeError):
    """Raised when Deepcoin credentials or API responses are invalid."""


@dataclass(slots=True)
class DeepcoinCredentials:
    api_key: str
    api_secret: str
    passphrase: str
    base_url: str = DEEPCOIN_BASE_URL
    timeout_seconds: float = 15.0


class DeepcoinTradingClientProtocol(Protocol):
    def place_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        """Submit one live order and return the raw Deepcoin response."""

    def trigger_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        """Submit one trigger order with optional open-position TP/SL."""

    def set_position_sltp(self, protection_payload: dict[str, Any]) -> dict[str, Any]:
        """Set take-profit / stop-loss protection for an existing position."""

    def replace_order_sltp(self, protection_payload: dict[str, Any]) -> dict[str, Any]:
        """Attach or replace take-profit / stop-loss protection for an open limit order."""

    def cancel_order(self, cancel_payload: dict[str, Any]) -> dict[str, Any]:
        """Cancel one live order."""

    def list_positions(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        """Return account positions, optionally filtered by instrument."""

    def list_trigger_orders_pending(self, *, inst_id: str) -> list[dict[str, Any]]:
        """Return pending trigger / TPSL orders for one instrument."""

    def get_ticker_price(self, *, inst_id: str) -> float | None:
        """Return the latest ticker price for one instrument."""


def load_deepcoin_credentials(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | Path] | None = None,
) -> DeepcoinCredentials:
    """Load Deepcoin API credentials from env vars or config env files."""

    env = dict(_load_env_file_values(env_file_paths or [".env", "config/telegram.env"]))
    env.update(environ or os.environ)
    api_key = env.get("DEEPCOIN_API_KEY", "")
    api_secret = env.get("DEEPCOIN_API_SECRET", "")
    passphrase = env.get("DEEPCOIN_API_PASSPHRASE", "")
    missing = [
        name
        for name, value in {
            "DEEPCOIN_API_KEY": api_key,
            "DEEPCOIN_API_SECRET": api_secret,
            "DEEPCOIN_API_PASSPHRASE": passphrase,
        }.items()
        if not value
    ]
    if missing:
        raise DeepcoinClientError(f"missing Deepcoin credentials: {','.join(missing)}")
    return DeepcoinCredentials(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        base_url=env.get("DEEPCOIN_BASE_URL", DEEPCOIN_BASE_URL).rstrip("/"),
        timeout_seconds=float(env.get("DEEPCOIN_TIMEOUT_SECONDS", "15")),
    )


class DeepcoinRestClient:
    """Small authenticated Deepcoin REST client."""

    def __init__(
        self,
        credentials: DeepcoinCredentials,
        *,
        http_client: httpx.Client | None = None,
        timestamp_factory=None,
    ) -> None:
        self._credentials = credentials
        self._http_client = http_client
        self._timestamp_factory = timestamp_factory or _utc_timestamp_ms

    def place_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", DEEPCOIN_PLACE_ORDER_PATH, order_payload)

    def trigger_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", DEEPCOIN_TRIGGER_ORDER_PATH, order_payload)

    def set_position_sltp(self, protection_payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", DEEPCOIN_SET_POSITION_SLTP_PATH, protection_payload)

    def replace_order_sltp(self, protection_payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", DEEPCOIN_REPLACE_ORDER_SLTP_PATH, protection_payload)

    def cancel_order(self, cancel_payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", DEEPCOIN_CANCEL_ORDER_PATH, cancel_payload)

    def list_positions(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        query = "instType=SWAP"
        if inst_id:
            query += f"&instId={inst_id}"
        payload = self._request("GET", f"{DEEPCOIN_ACCOUNT_POSITIONS_PATH}?{query}")
        data = payload.get("data")
        return data if isinstance(data, list) else []

    def list_trigger_orders_pending(self, *, inst_id: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"{DEEPCOIN_TRIGGER_ORDERS_PENDING_PATH}?instType=SWAP&instId={inst_id}",
        )
        data = payload.get("data")
        return data if isinstance(data, list) else []

    def get_ticker_price(self, *, inst_id: str) -> float | None:
        payload = self._request("GET", f"{DEEPCOIN_MARKET_TICKERS_PATH}?instType=SWAP")
        for item in _iter_deepcoin_payload_items(payload.get("data")):
            if str(item.get("instId") or "").upper() != inst_id.upper():
                continue
            for key in ("last", "lastPx", "markPx", "askPx", "bidPx"):
                value = item.get(key)
                if value in (None, ""):
                    continue
                try:
                    price = float(value)
                except (TypeError, ValueError):
                    continue
                if price > 0:
                    return price
        return None

    def _request(
        self,
        method: str,
        request_path: str,
        body_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = ""
        if body_payload is not None:
            body = json.dumps(body_payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = self._timestamp_factory()
        headers = build_deepcoin_auth_headers(
            credentials=self._credentials,
            timestamp=timestamp,
            method=method,
            request_path=request_path,
            body=body,
        )
        headers["Content-Type"] = "application/json"

        owns_client = self._http_client is None
        client = self._http_client or httpx.Client(
            base_url=self._credentials.base_url,
            timeout=self._credentials.timeout_seconds,
        )
        try:
            response = client.request(method, request_path, content=body, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise DeepcoinClientError(f"Deepcoin request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise DeepcoinClientError("Deepcoin response was not JSON") from exc
        finally:
            if owns_client:
                client.close()

        if str(payload.get("code", "0")) not in {"0", ""}:
            raise DeepcoinClientError(
                f"Deepcoin API error {payload.get('code')}: {payload.get('msg')}"
            )
        _raise_for_deepcoin_business_error(payload)
        return payload


def build_deepcoin_auth_headers(
    *,
    credentials: DeepcoinCredentials,
    timestamp: str,
    method: str,
    request_path: str,
    body: str,
) -> dict[str, str]:
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(
        credentials.api_secret.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    return {
        "DC-ACCESS-KEY": credentials.api_key,
        "DC-ACCESS-SIGN": signature,
        "DC-ACCESS-TIMESTAMP": timestamp,
        "DC-ACCESS-PASSPHRASE": credentials.passphrase,
    }


def build_deepcoin_client_from_env() -> DeepcoinRestClient:
    return DeepcoinRestClient(load_deepcoin_credentials())


def _raise_for_deepcoin_business_error(payload: dict[str, Any]) -> None:
    for item in _iter_deepcoin_payload_items(payload.get("data")):
        s_code = str(item.get("sCode", "0"))
        if s_code not in {"0", ""}:
            raise DeepcoinClientError(
                f"Deepcoin API error {s_code}: {item.get('sMsg') or item.get('msg')}"
            )


def _iter_deepcoin_payload_items(value: Any):
    if isinstance(value, dict):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item


def _utc_timestamp_ms() -> str:
    value = datetime.now(UTC)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

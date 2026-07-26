"""Deepcoin REST client helpers for authenticated trading requests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from telegram_kol_research.telegram_client import _load_env_file_values


DEEPCOIN_BASE_URL = "https://api.deepcoin.com"
DEEPCOIN_PLACE_ORDER_PATH = "/deepcoin/trade/order"
DEEPCOIN_CANCEL_ORDER_PATH = "/deepcoin/trade/cancel-order"
DEEPCOIN_CANCEL_TRIGGER_ORDER_PATH = "/deepcoin/trade/cancel-trigger-order"
DEEPCOIN_REPLACE_ORDER_SLTP_PATH = "/deepcoin/trade/replace-order-sltp"
DEEPCOIN_TRIGGER_ORDER_PATH = "/deepcoin/trade/trigger-order"
DEEPCOIN_ORDERS_PENDING_PATH = "/deepcoin/trade/orders-pending"
DEEPCOIN_ORDERS_HISTORY_PATH = "/deepcoin/trade/orders-history"
DEEPCOIN_TRADE_FILLS_PATH = "/deepcoin/trade/fills"
DEEPCOIN_TRIGGER_ORDERS_PENDING_PATH = "/deepcoin/trade/trigger-orders-pending"
DEEPCOIN_TRIGGER_ORDERS_HISTORY_PATH = "/deepcoin/trade/trigger-orders-history"
DEEPCOIN_SET_POSITION_SLTP_PATH = "/deepcoin/trade/set-position-sltp"
DEEPCOIN_CANCEL_POSITION_SLTP_PATH = "/deepcoin/trade/cancel-position-sltp"
DEEPCOIN_ACCOUNT_POSITIONS_PATH = "/deepcoin/account/positions"
DEEPCOIN_ACCOUNT_POSITIONS_HISTORY_PATH = "/deepcoin/account/positions-history"
DEEPCOIN_MARKET_TICKERS_PATH = "/deepcoin/market/tickers"


class DeepcoinClientError(RuntimeError):
    """Raised when Deepcoin credentials or API responses are invalid."""


class DeepcoinRequestOutcomeUnknown(DeepcoinClientError):
    """Raised when a write may have reached Deepcoin but no result was received."""


class DeepcoinDefiniteRejection(DeepcoinClientError):
    """Raised only when Deepcoin explicitly rejects a validated request."""


def _require_list_data(payload: dict[str, Any], *, endpoint: str) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise DeepcoinClientError(f"invalid list response schema: {endpoint}")
    if not all(isinstance(row, dict) for row in data):
        raise DeepcoinClientError(f"invalid list row schema: {endpoint}")
    return data


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

    def cancel_position_sltp(self, cancel_payload: dict[str, Any]) -> dict[str, Any]:
        """Cancel one existing position TPSL row by its exact order id."""

    def replace_order_sltp(self, protection_payload: dict[str, Any]) -> dict[str, Any]:
        """Attach or replace take-profit / stop-loss protection for an open limit order."""

    def cancel_order(self, cancel_payload: dict[str, Any]) -> dict[str, Any]:
        """Cancel one live order."""

    def cancel_trigger_order(self, cancel_payload: dict[str, Any]) -> dict[str, Any]:
        """Cancel one pending trigger / conditional order."""

    def list_positions(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        """Return account positions, optionally filtered by instrument."""

    def list_position_history(
        self,
        *,
        inst_id: str,
        pos_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return historical records for one exact split position."""

    def list_open_orders(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        """Return pending regular orders, optionally filtered by instrument."""

    def list_order_history(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        """Return historical regular orders, optionally filtered by instrument."""

    def list_trade_fills(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        """Return recent trade fills, optionally filtered by instrument."""

    def list_trigger_orders_pending(self, *, inst_id: str) -> list[dict[str, Any]]:
        """Return pending trigger / TPSL orders for one instrument."""

    def read_trigger_orders_pending(self, *, inst_id: str) -> dict[str, Any]:
        """Return the raw pending-trigger response for completeness auditing."""

    def list_trigger_order_history(self, *, inst_id: str) -> list[dict[str, Any]]:
        """Return historical trigger / TPSL orders for one instrument."""

    def get_ticker_price(self, *, inst_id: str) -> float | None:
        """Return the latest ticker price for one instrument."""

    def get_ticker_quote(self, *, inst_id: str) -> dict[str, str] | None:
        """Return structured latest-price evidence for one instrument."""

    def list_swap_symbols(self) -> list[dict[str, str]]:
        """Return tradable SWAP base symbols and instrument ids."""


def load_deepcoin_credentials(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | Path] | None = None,
) -> DeepcoinCredentials:
    """Load Deepcoin API credentials from env vars or config env files."""

    paths = [".env", "config/telegram.env"] if env_file_paths is None else env_file_paths
    env = {} if paths == [] else dict(_load_env_file_values(paths))
    env.update(os.environ if environ is None else environ)
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


class DeepcoinTpslWriteLimiter:
    """Thread-safe sliding-window limiter shared by all position TPSL writes."""

    def __init__(
        self,
        *,
        monotonic_factory: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        per_second: int = 15,
        per_minute: int = 450,
    ) -> None:
        self._clock = monotonic_factory
        self._sleep = sleep_fn
        self._per_second = max(1, int(per_second))
        self._per_minute = max(1, int(per_minute))
        self._starts: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = self._clock()
                while self._starts and now - self._starts[0] >= 60.0:
                    self._starts.popleft()
                recent_second = [started for started in self._starts if now - started < 1.0]
                delays: list[float] = []
                if len(recent_second) >= self._per_second:
                    delays.append(1.0 - (now - recent_second[-self._per_second]))
                if len(self._starts) >= self._per_minute:
                    delays.append(60.0 - (now - self._starts[-self._per_minute]))
                delay = max(delays, default=0.0)
                if delay <= 0:
                    self._starts.append(now)
                    return
                self._sleep(delay)


_TPSL_LIMITERS_LOCK = threading.Lock()
_TPSL_LIMITERS: dict[tuple[str, str], DeepcoinTpslWriteLimiter] = {}


def _shared_tpsl_limiter(credentials: DeepcoinCredentials) -> DeepcoinTpslWriteLimiter:
    """Return the process-wide limiter for one API credential scope."""

    key = (credentials.base_url.rstrip("/"), credentials.api_key)
    with _TPSL_LIMITERS_LOCK:
        limiter = _TPSL_LIMITERS.get(key)
        if limiter is None:
            limiter = DeepcoinTpslWriteLimiter()
            _TPSL_LIMITERS[key] = limiter
        return limiter


class DeepcoinRestClient:
    """Small authenticated Deepcoin REST client."""

    def __init__(
        self,
        credentials: DeepcoinCredentials,
        *,
        http_client: httpx.Client | None = None,
        timestamp_factory=None,
        monotonic_factory: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        position_history_min_interval_seconds: float = 1.05,
        tpsl_rate_limiter: "DeepcoinTpslWriteLimiter | None" = None,
    ) -> None:
        self._credentials = credentials
        self._http_client = http_client
        self._timestamp_factory = timestamp_factory or _utc_timestamp_ms
        self._monotonic_factory = monotonic_factory or time.monotonic
        self._sleep_fn = sleep_fn or time.sleep
        self._position_history_min_interval_seconds = max(
            0.0,
            float(position_history_min_interval_seconds),
        )
        self._last_position_history_request_started_at: float | None = None
        if tpsl_rate_limiter is not None:
            self._tpsl_rate_limiter = tpsl_rate_limiter
        elif monotonic_factory is not None or sleep_fn is not None:
            # Explicit clocks are test/integration scopes and must remain
            # deterministic. Production defaults share by credential UID.
            self._tpsl_rate_limiter = DeepcoinTpslWriteLimiter(
                monotonic_factory=self._monotonic_factory,
                sleep_fn=self._sleep_fn,
            )
        else:
            self._tpsl_rate_limiter = _shared_tpsl_limiter(credentials)

    def place_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", DEEPCOIN_PLACE_ORDER_PATH, order_payload)

    def trigger_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", DEEPCOIN_TRIGGER_ORDER_PATH, order_payload)

    def set_position_sltp(self, protection_payload: dict[str, Any]) -> dict[str, Any]:
        """Compatibility wrapper; new callers must use PositionMutationGateway."""
        return self._set_position_sltp_unchecked(protection_payload)

    def _set_position_sltp_unchecked(
        self, protection_payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._tpsl_rate_limiter.acquire()
        return self._request("POST", DEEPCOIN_SET_POSITION_SLTP_PATH, protection_payload)

    def cancel_position_sltp(self, cancel_payload: dict[str, Any]) -> dict[str, Any]:
        """Compatibility wrapper; new callers must use PositionMutationGateway."""
        return self._cancel_position_sltp_unchecked(cancel_payload)

    def _cancel_position_sltp_unchecked(
        self, cancel_payload: dict[str, Any]
    ) -> dict[str, Any]:
        required = {"instType", "instId", "ordId"}
        if any(cancel_payload.get(key) in (None, "") for key in required):
            raise DeepcoinClientError(
                "cancel-position-sltp requires instType, instId, and ordId"
            )
        payload = {key: cancel_payload[key] for key in ("instType", "instId", "ordId")}
        self._tpsl_rate_limiter.acquire()
        return self._request("POST", DEEPCOIN_CANCEL_POSITION_SLTP_PATH, payload)

    def _place_position_close_unchecked(
        self, close_payload: dict[str, Any]
    ) -> dict[str, Any]:
        required = {"instId", "closePosId", "ordType", "sz"}
        if any(close_payload.get(key) in (None, "") for key in required):
            raise DeepcoinClientError(
                "position close requires instId, closePosId, ordType, and sz"
            )
        return self._request("POST", DEEPCOIN_PLACE_ORDER_PATH, close_payload)

    def replace_order_sltp(self, protection_payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", DEEPCOIN_REPLACE_ORDER_SLTP_PATH, protection_payload)

    def cancel_order(self, cancel_payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", DEEPCOIN_CANCEL_ORDER_PATH, cancel_payload)

    def cancel_trigger_order(self, cancel_payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", DEEPCOIN_CANCEL_TRIGGER_ORDER_PATH, cancel_payload)

    def list_positions(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_ACCOUNT_POSITIONS_PATH,
                {"instType": "SWAP", "instId": inst_id},
            ),
        )
        return _require_list_data(payload, endpoint=DEEPCOIN_ACCOUNT_POSITIONS_PATH)

    def list_position_history(
        self,
        *,
        inst_id: str,
        pos_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self._pace_position_history_request()
        payload = self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_ACCOUNT_POSITIONS_HISTORY_PATH,
                {
                    "instType": "SWAP",
                    "instId": inst_id,
                    "mrgPosition": "split",
                    "posId": pos_id,
                    "limit": 100,
                },
            ),
        )
        return _require_list_data(
            payload,
            endpoint=DEEPCOIN_ACCOUNT_POSITIONS_HISTORY_PATH,
        )

    def _pace_position_history_request(self) -> None:
        now = self._monotonic_factory()
        previous = self._last_position_history_request_started_at
        if previous is not None:
            remaining = self._position_history_min_interval_seconds - (now - previous)
            if remaining > 0:
                self._sleep_fn(remaining)
                now = self._monotonic_factory()
        self._last_position_history_request_started_at = now

    def list_open_orders(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_ORDERS_PENDING_PATH,
                {"instType": "SWAP", "instId": inst_id},
            ),
        )
        return _require_list_data(payload, endpoint=DEEPCOIN_ORDERS_PENDING_PATH)

    def list_order_history(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_ORDERS_HISTORY_PATH,
                {"instType": "SWAP", "instId": inst_id},
            ),
        )
        return _require_list_data(payload, endpoint=DEEPCOIN_ORDERS_HISTORY_PATH)

    def list_trade_fills(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_TRADE_FILLS_PATH,
                {"instType": "SWAP", "instId": inst_id},
            ),
        )
        return _require_list_data(payload, endpoint=DEEPCOIN_TRADE_FILLS_PATH)

    def get_order_history_by_id(
        self,
        *,
        inst_id: str,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return one historical regular order by Deepcoin id or client order id."""

        return _find_order_by_ids(
            self.list_order_history(inst_id=inst_id),
            order_id=order_id,
            client_order_id=client_order_id,
        )

    def list_trigger_orders_pending(self, *, inst_id: str) -> list[dict[str, Any]]:
        return _require_list_data(
            self.read_trigger_orders_pending(inst_id=inst_id),
            endpoint=DEEPCOIN_TRIGGER_ORDERS_PENDING_PATH,
        )

    def read_trigger_orders_pending(self, *, inst_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_TRIGGER_ORDERS_PENDING_PATH,
                {"instType": "SWAP", "instId": inst_id, "limit": 100},
            ),
        )

    def list_trigger_order_history(self, *, inst_id: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_TRIGGER_ORDERS_HISTORY_PATH,
                {"instType": "SWAP", "instId": inst_id},
            ),
        )
        return _require_list_data(payload, endpoint=DEEPCOIN_TRIGGER_ORDERS_HISTORY_PATH)

    def get_trigger_order_history_by_id(
        self,
        *,
        inst_id: str,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return one historical trigger/TPSL order by Deepcoin id or client id."""

        return _find_order_by_ids(
            self.list_trigger_order_history(inst_id=inst_id),
            order_id=order_id,
            client_order_id=client_order_id,
        )

    def get_ticker_quote(self, *, inst_id: str) -> dict[str, str] | None:
        payload = self._request("GET", f"{DEEPCOIN_MARKET_TICKERS_PATH}?instType=SWAP")
        target_instrument_id = inst_id.strip().upper()
        matches = [
            item
            for item in _iter_deepcoin_payload_items(payload.get("data"))
            if str(item.get("instId") or "").strip().upper() == target_instrument_id
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise DeepcoinClientError(
                f"duplicate ticker rows for instrument: {target_instrument_id}"
            )

        ticker = matches[0]
        price_field = next(
            (
                field
                for field in ("last", "lastPx")
                if ticker.get(field) not in (None, "")
            ),
            None,
        )
        if price_field is None:
            raise DeepcoinClientError(
                f"ticker price missing for instrument: {target_instrument_id}"
            )

        price = str(ticker[price_field]).strip()
        try:
            decimal_price = Decimal(price)
        except (InvalidOperation, ValueError):
            decimal_price = Decimal("NaN")
        if not decimal_price.is_finite() or decimal_price <= 0:
            raise DeepcoinClientError(
                f"invalid ticker {price_field} for instrument: {target_instrument_id}"
            )
        return {
            "instrument_id": target_instrument_id,
            "price": price,
            "price_field": price_field,
        }

    def get_ticker_price(self, *, inst_id: str) -> float | None:
        quote = self.get_ticker_quote(inst_id=inst_id)
        if quote is None:
            return None
        return float(quote["price"])

    def list_swap_symbols(self) -> list[dict[str, str]]:
        payload = self._request("GET", f"{DEEPCOIN_MARKET_TICKERS_PATH}?instType=SWAP")
        symbols_by_instrument: dict[str, dict[str, str]] = {}
        for item in _iter_deepcoin_payload_items(payload.get("data")):
            instrument_id = str(item.get("instId") or "").strip().upper()
            if not instrument_id.endswith("-USDT-SWAP"):
                continue
            symbol = instrument_id.removesuffix("-USDT-SWAP")
            if not symbol:
                continue
            symbols_by_instrument[instrument_id] = {
                "symbol": symbol,
                "instrument_id": instrument_id,
            }
        return sorted(
            symbols_by_instrument.values(),
            key=lambda item: item["symbol"],
        )

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
        except httpx.RequestError as exc:
            if method.upper() == "POST":
                raise DeepcoinRequestOutcomeUnknown(
                    f"Deepcoin request outcome unknown: {exc}"
                ) from exc
            raise DeepcoinClientError(f"Deepcoin request failed: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            if method.upper() == "POST":
                raise DeepcoinRequestOutcomeUnknown(
                    f"Deepcoin request outcome unknown after HTTP status: {exc}"
                ) from exc
            raise DeepcoinClientError(f"Deepcoin request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            if method.upper() == "POST":
                raise DeepcoinRequestOutcomeUnknown(
                    "Deepcoin write response was not JSON"
                ) from exc
            raise DeepcoinClientError("Deepcoin response was not JSON") from exc
        finally:
            if owns_client:
                try:
                    client.close()
                except Exception as exc:
                    if method.upper() == "POST":
                        raise DeepcoinRequestOutcomeUnknown(
                            f"Deepcoin request outcome unknown during cleanup: {exc}"
                        ) from exc
                    raise DeepcoinClientError(
                        f"Deepcoin client cleanup failed: {exc}"
                    ) from exc

        if str(payload.get("code", "0")) not in {"0", ""}:
            raise DeepcoinDefiniteRejection(
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
            raise DeepcoinDefiniteRejection(
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


def _path_with_query(path: str, params: dict[str, Any]) -> str:
    filtered = {
        key: value
        for key, value in params.items()
        if value not in (None, "")
    }
    if not filtered:
        return path
    return f"{path}?{urlencode(filtered)}"


def _find_order_by_ids(
    orders: list[dict[str, Any]],
    *,
    order_id: str | None,
    client_order_id: str | None,
) -> dict[str, Any] | None:
    for order in orders:
        current_order_id = _first_order_string(
            order,
            "ordId",
            "orderId",
            "order_id",
            "algoId",
            "triggerOrderId",
            "id",
        )
        current_client_order_id = _first_order_string(
            order,
            "clOrdId",
            "clientOrderId",
            "client_order_id",
        )
        if order_id and current_order_id == str(order_id):
            return order
        if client_order_id and current_client_order_id == str(client_order_id):
            return order
    return None


def _first_order_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None

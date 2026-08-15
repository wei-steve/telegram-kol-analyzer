"""Deepcoin REST client helpers for authenticated trading requests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import random
import re
import time
import threading
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from telegram_kol_research.deepcoin_request_governor import (
    DeepcoinGovernorDeadlineExceeded,
    DeepcoinGovernorError,
    DeepcoinGovernorStateError,
    build_deepcoin_request_governor_from_environment,
)
from telegram_kol_research.deepcoin_request_policy import (
    ErrorCategory,
    FailureFact,
    OutcomeCertainty,
    RequestPriority,
    classify_business_failure,
    classify_http_failure,
    classify_schema_failure,
    classify_transport_failure,
    normalize_request_path,
)
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
DEEPCOIN_MARKET_INSTRUMENTS_PATH = "/deepcoin/market/instruments"
DEEPCOIN_MARKET_TICKERS_PATH = "/deepcoin/market/tickers"

_DEEPCOIN_LIST_READ_PATHS = frozenset(
    {
        DEEPCOIN_ORDERS_PENDING_PATH,
        DEEPCOIN_ORDERS_HISTORY_PATH,
        DEEPCOIN_TRADE_FILLS_PATH,
        DEEPCOIN_TRIGGER_ORDERS_PENDING_PATH,
        DEEPCOIN_TRIGGER_ORDERS_HISTORY_PATH,
        DEEPCOIN_ACCOUNT_POSITIONS_PATH,
        DEEPCOIN_ACCOUNT_POSITIONS_HISTORY_PATH,
        DEEPCOIN_MARKET_INSTRUMENTS_PATH,
        DEEPCOIN_MARKET_TICKERS_PATH,
    }
)
_SAFE_EXACT_EXCHANGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


class DeepcoinClientError(RuntimeError):
    """Raised when Deepcoin credentials or API responses are invalid."""


class DeepcoinRequestOutcomeUnknown(DeepcoinClientError):
    """Raised when a write may have reached Deepcoin but no result was received."""

    def __init__(self, message: str, *, fact: FailureFact | None = None) -> None:
        self.fact = fact or classify_transport_failure(
            method="POST",
            sent=True,
            code="writer_outcome_unknown",
        )
        super().__init__(message)


class DeepcoinDefiniteRejection(DeepcoinClientError):
    """Raised only when Deepcoin explicitly rejects a validated request."""

    def __init__(self, message: str, *, fact: FailureFact | None = None) -> None:
        self.fact = fact or classify_business_failure(
            method="POST",
            exchange_code="unspecified",
        )
        super().__init__(message)


class DeepcoinReadUnavailable(DeepcoinClientError):
    """A safe read did not produce authoritative exchange state."""

    def __init__(self, message: str, *, fact: FailureFact) -> None:
        self.fact = fact
        super().__init__(message)


class DeepcoinPreSendUnavailable(DeepcoinClientError):
    """A typed local refusal proved that no exchange request was submitted."""

    def __init__(self, message: str, *, fact: FailureFact) -> None:
        self.fact = fact
        super().__init__(message)


class _MonitorResponseSizeExceeded(RuntimeError):
    """A monitor-scoped response crossed its pre-decode byte limit."""


@dataclass(frozen=True, slots=True)
class RequestAttemptFact:
    ordinal: int
    method: str
    normalized_path: str
    phase: str
    priority: RequestPriority
    correlation_id: str | None
    outcome_certainty: OutcomeCertainty
    error_category: ErrorCategory | None
    safe_code: str
    http_status: int | None
    business_code: str | None
    governor_wait_ms: int
    retry_delay_ms: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class DeepcoinRequestScope:
    phase: str
    priority: RequestPriority
    deadline_monotonic: float | None
    correlation_id: str | None = None
    attempt_recorder: Callable[[RequestAttemptFact], None] | None = None
    max_response_bytes: int | None = None


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

    def read_positions(self, *, inst_id: str | None = None) -> dict[str, Any]:
        """Return raw positions response including pagination metadata."""

    def list_position_history(
        self,
        *,
        inst_id: str,
        pos_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return historical records for one exact split position."""

    def read_position_history(
        self, *, inst_id: str, pos_id: str | None = None
    ) -> dict[str, Any]:
        """Return raw position-history response including pagination metadata."""

    def list_open_orders(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        """Return pending regular orders, optionally filtered by instrument."""

    def read_open_orders(self, *, inst_id: str | None = None) -> dict[str, Any]:
        """Return raw pending-order response including pagination metadata."""

    def list_order_history(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        """Return historical regular orders, optionally filtered by instrument."""

    def read_order_history(
        self,
        *,
        inst_id: str | None = None,
        order_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Return raw order-history response including pagination metadata."""

    def list_trade_fills(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        """Return recent trade fills, optionally filtered by instrument."""

    def read_trade_fills(
        self,
        *,
        inst_id: str | None = None,
        order_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Return raw fills response including pagination metadata."""

    def list_trigger_orders_pending(self, *, inst_id: str) -> list[dict[str, Any]]:
        """Return pending trigger / TPSL orders for one instrument."""

    def read_trigger_orders_pending(self, *, inst_id: str) -> dict[str, Any]:
        """Return the raw pending-trigger response for completeness auditing."""

    def list_trigger_order_history(self, *, inst_id: str) -> list[dict[str, Any]]:
        """Return historical trigger / TPSL orders for one instrument."""

    def read_trigger_order_history(
        self,
        *,
        inst_id: str,
        order_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Return raw trigger-history response including pagination metadata."""

    def get_ticker_price(self, *, inst_id: str) -> float | None:
        """Return the latest ticker price for one instrument."""

    def get_ticker_quote(self, *, inst_id: str) -> dict[str, str] | None:
        """Return structured latest-price evidence for one instrument."""

    def list_swap_symbols(self) -> list[dict[str, str]]:
        """Return tradable SWAP base symbols and instrument ids."""

    def list_swap_instruments(self) -> list[dict[str, Any]]:
        """Return raw SWAP product information for contract-spec validation."""


def load_deepcoin_credentials(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | Path] | None = None,
) -> DeepcoinCredentials:
    """Load Deepcoin API credentials from env vars or config env files."""

    env = _load_deepcoin_environment(
        environ=environ,
        env_file_paths=env_file_paths,
    )
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
        request_governor: Any | None = None,
        retry_jitter_fn: Callable[[], float] | None = None,
        read_only: bool = False,
        trust_env: bool | None = None,
    ) -> None:
        self._credentials = credentials
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._http_client_lock = threading.Lock()
        self._closed = False
        self._read_only = bool(read_only)
        self._trust_env = trust_env
        self._timestamp_factory = timestamp_factory or _utc_timestamp_ms
        self._monotonic_factory = monotonic_factory or time.monotonic
        self._sleep_fn = sleep_fn or time.sleep
        self._retry_jitter_fn = retry_jitter_fn or (
            lambda: random.uniform(0.0, 0.25)
        )
        self._request_scope_context: ContextVar[DeepcoinRequestScope | None] = (
            ContextVar(f"deepcoin_request_scope_{id(self)}", default=None)
        )
        self._position_history_min_interval_seconds = max(
            0.0,
            float(position_history_min_interval_seconds),
        )
        self._last_position_history_request_started_at: float | None = None
        self._request_governor = request_governor
        scope_source = (
            f"{str(credentials.base_url).rstrip('/')}\0{str(credentials.api_key)}".encode(
                "utf-8"
            )
        )
        self.uid_scope_hash = hashlib.sha256(scope_source).hexdigest()
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

    def close(self) -> None:
        """Release a lazily owned HTTP connection exactly once."""
        with self._http_client_lock:
            if self._closed:
                return
            self._closed = True
            client = self._http_client if self._owns_http_client else None
            self._http_client = None
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                raise DeepcoinClientError(
                    f"Deepcoin client cleanup failed: {exc}"
                ) from exc

    def __enter__(self) -> "DeepcoinRestClient":
        with self._http_client_lock:
            if self._closed:
                raise DeepcoinClientError("Deepcoin client is closed")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.close()
        except DeepcoinClientError:
            # Context-manager cleanup must not replace a parsed exchange result
            # or the caller's original exception. Explicit close still reports.
            return None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _get_http_client(self):
        with self._http_client_lock:
            if self._closed:
                raise DeepcoinClientError("Deepcoin client is closed")
            if self._http_client is None:
                client_options: dict[str, Any] = {
                    "base_url": self._credentials.base_url,
                    "timeout": self._credentials.timeout_seconds,
                }
                if self._trust_env is not None:
                    client_options["trust_env"] = self._trust_env
                self._http_client = httpx.Client(
                    **client_options,
                )
            return self._http_client

    @contextmanager
    def request_scope(self, scope: DeepcoinRequestScope):
        if not isinstance(scope, DeepcoinRequestScope):
            raise TypeError("scope must be DeepcoinRequestScope")
        RequestPriority(scope.priority)
        _bounded_optional_deadline(scope.deadline_monotonic)
        response_limit = _bounded_optional_response_limit(scope.max_response_bytes)
        if response_limit is not None and scope.phase != "production_monitor_snapshot":
            raise DeepcoinClientError(
                "bounded response bytes are reserved for the production monitor"
            )
        token = self._request_scope_context.set(scope)
        try:
            yield self
        finally:
            self._request_scope_context.reset(token)

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
        if not self._request_governor_enforces("POST"):
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
        if not self._request_governor_enforces("POST"):
            self._tpsl_rate_limiter.acquire()
        return self._request("POST", DEEPCOIN_CANCEL_POSITION_SLTP_PATH, payload)

    def _request_governor_enforces(self, method: str) -> bool:
        governor = self._request_governor
        if governor is None:
            return False
        predicate = getattr(governor, "enforces", None)
        if not callable(predicate):
            return False
        return bool(predicate(method))

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
        payload = self.read_positions(inst_id=inst_id)
        return _require_list_data(payload, endpoint=DEEPCOIN_ACCOUNT_POSITIONS_PATH)

    def read_positions(self, *, inst_id: str | None = None) -> dict[str, Any]:
        return self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_ACCOUNT_POSITIONS_PATH,
                {"instType": "SWAP", "instId": inst_id},
            ),
        )

    def list_position_history(
        self,
        *,
        inst_id: str,
        pos_id: str | None = None,
    ) -> list[dict[str, Any]]:
        payload = self.read_position_history(inst_id=inst_id, pos_id=pos_id)
        return _require_list_data(
            payload,
            endpoint=DEEPCOIN_ACCOUNT_POSITIONS_HISTORY_PATH,
        )

    def read_position_history(
        self,
        *,
        inst_id: str,
        pos_id: str | None = None,
    ) -> dict[str, Any]:
        self._pace_position_history_request()
        return self._request(
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
        payload = self.read_open_orders(inst_id=inst_id)
        return _require_list_data(payload, endpoint=DEEPCOIN_ORDERS_PENDING_PATH)

    def read_open_orders(self, *, inst_id: str | None = None) -> dict[str, Any]:
        return self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_ORDERS_PENDING_PATH,
                {"instType": "SWAP", "instId": inst_id},
            ),
        )

    def list_order_history(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        payload = self.read_order_history(inst_id=inst_id)
        return _require_list_data(payload, endpoint=DEEPCOIN_ORDERS_HISTORY_PATH)

    def read_order_history(
        self,
        *,
        inst_id: str | None = None,
        order_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_ORDERS_HISTORY_PATH,
                {
                    "instType": "SWAP",
                    "instId": inst_id,
                    "ordId": _optional_exact_exchange_id(order_id),
                    "limit": _optional_history_limit(limit),
                },
            ),
        )

    def list_trade_fills(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        payload = self.read_trade_fills(inst_id=inst_id)
        return _require_list_data(payload, endpoint=DEEPCOIN_TRADE_FILLS_PATH)

    def read_trade_fills(
        self,
        *,
        inst_id: str | None = None,
        order_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_TRADE_FILLS_PATH,
                {
                    "instType": "SWAP",
                    "instId": inst_id,
                    "ordId": _optional_exact_exchange_id(order_id),
                    "limit": _optional_history_limit(limit),
                },
            ),
        )

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
        payload = self.read_trigger_order_history(inst_id=inst_id)
        return _require_list_data(payload, endpoint=DEEPCOIN_TRIGGER_ORDERS_HISTORY_PATH)

    def read_trigger_order_history(
        self,
        *,
        inst_id: str,
        order_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_TRIGGER_ORDERS_HISTORY_PATH,
                {
                    "instType": "SWAP",
                    "instId": inst_id,
                    "ordId": _optional_exact_exchange_id(order_id),
                    "limit": _optional_history_limit(limit),
                },
            ),
        )

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
        observed_at = _ticker_timestamp_iso(ticker.get("ts"))
        if observed_at is None:
            raise DeepcoinClientError(
                f"ticker timestamp missing for instrument: {target_instrument_id}"
            )
        return {
            "instrument_id": target_instrument_id,
            "price": price,
            "price_field": price_field,
            "observed_at": observed_at,
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

    def list_swap_instruments(self) -> list[dict[str, Any]]:
        payload = self._request(
            "GET", f"{DEEPCOIN_MARKET_INSTRUMENTS_PATH}?instType=SWAP"
        )
        return _require_list_data(payload, endpoint=DEEPCOIN_MARKET_INSTRUMENTS_PATH)

    def _request(
        self,
        method: str,
        request_path: str,
        body_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_method = str(method or "").strip().upper()
        if self._read_only and normalized_method != "GET":
            raise DeepcoinClientError("read-only Deepcoin client forbids writes")
        normalized_path = normalize_request_path(request_path)
        body = ""
        if body_payload is not None:
            body = json.dumps(body_payload, ensure_ascii=False, separators=(",", ":"))
        scope = self._request_scope_context.get()
        if scope is None:
            priority = RequestPriority.NORMAL
            deadline = self._safe_monotonic() + 10.0
            phase = "legacy_unscoped"
            correlation_id = None
            recorder = None
            response_limit = None
        else:
            priority = RequestPriority(scope.priority)
            deadline = _bounded_optional_deadline(scope.deadline_monotonic)
            phase = _bounded_label(scope.phase, fallback="unspecified_phase")
            correlation_id = _bounded_optional_label(scope.correlation_id)
            recorder = scope.attempt_recorder
            response_limit = _bounded_optional_response_limit(
                scope.max_response_bytes
            )
        if response_limit is not None and normalized_method != "GET":
            raise DeepcoinClientError(
                "bounded production monitor response only supports GET"
            )
        max_attempts = (
            1
            if normalized_method != "GET"
            else (2 if priority == RequestPriority.BACKGROUND else 4)
        )
        schema_occurrences = 0
        client = self._get_http_client()
        for ordinal in range(1, max_attempts + 1):
            attempt_started = self._safe_monotonic()
            governor_wait_ms = 0
            if self._request_governor is not None:
                try:
                    lease = self._request_governor.acquire(
                        method=normalized_method,
                        request_path=request_path,
                        priority=priority,
                        deadline_monotonic=deadline,
                    )
                    governor_wait_ms = max(
                        0,
                        int(getattr(lease, "waited_ms", 0)),
                    )
                except DeepcoinGovernorError as exc:
                    fact = _governor_failure_fact(exc)
                    self._record_attempt(
                        recorder=recorder,
                        fact=_attempt_fact(
                            ordinal=ordinal,
                            method=normalized_method,
                            normalized_path=normalized_path,
                            phase=phase,
                            priority=priority,
                            correlation_id=correlation_id,
                            failure=fact,
                            governor_wait_ms=0,
                            retry_delay_ms=0,
                            latency_ms=0,
                        ),
                    )
                    if normalized_method == "POST":
                        raise DeepcoinPreSendUnavailable(
                            fact.safe_code,
                            fact=fact,
                        ) from None
                    raise DeepcoinReadUnavailable(
                        fact.safe_code,
                        fact=fact,
                    ) from None

            remaining = _remaining_seconds(deadline, self._safe_monotonic())
            if remaining is not None and remaining <= 0:
                fact = _request_deadline_exhausted_fact()
                self._record_attempt(
                    recorder=recorder,
                    fact=_attempt_fact(
                        ordinal=ordinal,
                        method=normalized_method,
                        normalized_path=normalized_path,
                        phase=phase,
                        priority=priority,
                        correlation_id=correlation_id,
                        failure=fact,
                        governor_wait_ms=governor_wait_ms,
                        retry_delay_ms=0,
                        latency_ms=0,
                    ),
                )
                if normalized_method == "POST":
                    raise DeepcoinPreSendUnavailable(
                        fact.safe_code,
                        fact=fact,
                    )
                raise DeepcoinReadUnavailable(fact.safe_code, fact=fact)

            timestamp = self._timestamp_factory()
            headers = build_deepcoin_auth_headers(
                credentials=self._credentials,
                timestamp=timestamp,
                method=normalized_method,
                request_path=request_path,
                body=body,
            )
            headers["Content-Type"] = "application/json"
            if response_limit is not None:
                headers["Accept-Encoding"] = "identity"
            remaining = _remaining_seconds(deadline, self._safe_monotonic())
            if remaining is not None and remaining <= 0:
                fact = _request_deadline_exhausted_fact()
                self._record_attempt(
                    recorder=recorder,
                    fact=_attempt_fact(
                        ordinal=ordinal,
                        method=normalized_method,
                        normalized_path=normalized_path,
                        phase=phase,
                        priority=priority,
                        correlation_id=correlation_id,
                        failure=fact,
                        governor_wait_ms=governor_wait_ms,
                        retry_delay_ms=0,
                        latency_ms=_elapsed_ms(
                            attempt_started,
                            self._safe_monotonic(),
                        ),
                    ),
                )
                if normalized_method == "POST":
                    raise DeepcoinPreSendUnavailable(
                        fact.safe_code,
                        fact=fact,
                    )
                raise DeepcoinReadUnavailable(fact.safe_code, fact=fact)
            timeout_seconds = self._credentials.timeout_seconds
            if remaining is not None:
                timeout_seconds = min(timeout_seconds, remaining)
            failure: FailureFact | None = None
            business_code: str | None = None
            retry_after: float | None = None
            cause: BaseException | None = None
            try:
                raw_payload: bytes | None = None
                if response_limit is None:
                    response = client.request(
                        normalized_method,
                        request_path,
                        content=body,
                        headers=headers,
                        timeout=timeout_seconds,
                    )
                    response_status = response.status_code
                    response_headers = response.headers
                else:
                    with client.stream(
                        normalized_method,
                        request_path,
                        content=body,
                        headers=headers,
                        timeout=timeout_seconds,
                    ) as response:
                        response_status = response.status_code
                        response_headers = response.headers
                        if response_status < 400:
                            raw_payload = _read_bounded_monitor_response(
                                response,
                                limit=response_limit,
                            )
                if response_status >= 400:
                    failure = classify_http_failure(
                        method=normalized_method,
                        status_code=response_status,
                    )
                    retry_after = _bounded_retry_after(
                        response_headers.get("Retry-After")
                    )
                else:
                    try:
                        payload = (
                            response.json()
                            if raw_payload is None
                            else json.loads(raw_payload)
                        )
                    except (
                        json.JSONDecodeError,
                        RecursionError,
                        UnicodeDecodeError,
                        ValueError,
                    ) as exc:
                        schema_occurrences += 1
                        failure = classify_schema_failure(
                            method=normalized_method,
                            occurrence=schema_occurrences,
                        )
                        cause = exc
                    else:
                        if not isinstance(payload, dict):
                            schema_occurrences += 1
                            failure = classify_schema_failure(
                                method=normalized_method,
                                occurrence=schema_occurrences,
                            )
                        else:
                            try:
                                business_code = _payload_business_code(payload)
                            except ValueError as exc:
                                schema_occurrences += 1
                                failure = classify_schema_failure(
                                    method=normalized_method,
                                    occurrence=schema_occurrences,
                                )
                                cause = exc
                            if failure is not None:
                                pass
                            elif business_code is not None:
                                failure = classify_business_failure(
                                    method=normalized_method,
                                    exchange_code=business_code,
                                )
                            elif not _response_schema_valid(
                                method=normalized_method,
                                normalized_path=normalized_path,
                                payload=payload,
                            ):
                                schema_occurrences += 1
                                failure = classify_schema_failure(
                                    method=normalized_method,
                                    occurrence=schema_occurrences,
                                )
                            else:
                                self._record_attempt(
                                    recorder=recorder,
                                    fact=RequestAttemptFact(
                                        ordinal=ordinal,
                                        method=normalized_method,
                                        normalized_path=normalized_path,
                                        phase=phase,
                                        priority=priority,
                                        correlation_id=correlation_id,
                                        outcome_certainty=OutcomeCertainty.ACCEPTED,
                                        error_category=None,
                                        safe_code="request_accepted",
                                        http_status=response_status,
                                        business_code=None,
                                        governor_wait_ms=governor_wait_ms,
                                        retry_delay_ms=0,
                                        latency_ms=_elapsed_ms(
                                            attempt_started,
                                            self._safe_monotonic(),
                                        ),
                                    ),
                                )
                                return payload
            except _MonitorResponseSizeExceeded as exc:
                failure = _monitor_response_size_failure()
                cause = exc
            except httpx.RequestError as exc:
                failure = classify_transport_failure(
                    method=normalized_method,
                    sent=True,
                    code=_transport_safe_code(exc),
                )
                cause = exc

            assert failure is not None
            remaining = _remaining_seconds(deadline, self._safe_monotonic())
            retry_candidate = (
                normalized_method == "GET"
                and failure.retryable
                and ordinal < max_attempts
            )
            delay = 0.0
            if retry_candidate:
                try:
                    delay = self._retry_delay(
                        failure=failure,
                        retry_after=retry_after,
                        ordinal=ordinal,
                    )
                except Exception:
                    retry_candidate = False
            retry_permitted = retry_candidate and (
                remaining is None or delay < remaining
            )
            self._record_attempt(
                recorder=recorder,
                fact=_attempt_fact(
                    ordinal=ordinal,
                    method=normalized_method,
                    normalized_path=normalized_path,
                    phase=phase,
                    priority=priority,
                    correlation_id=correlation_id,
                    failure=failure,
                    business_code=business_code,
                    governor_wait_ms=governor_wait_ms,
                    retry_delay_ms=(_milliseconds(delay) if retry_permitted else 0),
                    latency_ms=_elapsed_ms(
                        attempt_started,
                        self._safe_monotonic(),
                    ),
                ),
            )
            if retry_permitted:
                self._sleep_fn(delay)
                continue
            if failure.outcome_certainty == OutcomeCertainty.REJECTED:
                rejection_code = business_code or failure.safe_code
                raise DeepcoinDefiniteRejection(
                    f"Deepcoin request rejected: {rejection_code}",
                    fact=failure,
                ) from cause
            if normalized_method == "POST":
                raise DeepcoinRequestOutcomeUnknown(
                    "Deepcoin request outcome unknown",
                    fact=failure,
                ) from cause
            raise DeepcoinReadUnavailable(
                failure.safe_code,
                fact=failure,
            ) from cause
        raise AssertionError("unreachable request attempt loop")

    def _safe_monotonic(self) -> float:
        value = float(self._monotonic_factory())
        if not math.isfinite(value) or value < 0:
            raise DeepcoinClientError("invalid monotonic clock")
        return value

    def _retry_delay(
        self,
        *,
        failure: FailureFact,
        retry_after: float | None,
        ordinal: int,
    ) -> float:
        if failure.category == ErrorCategory.RATE_LIMITED and retry_after is not None:
            return retry_after
        base = (0.5, 1.0, 2.0)[min(max(ordinal - 1, 0), 2)]
        try:
            jitter = float(self._retry_jitter_fn())
        except (TypeError, ValueError) as exc:
            raise DeepcoinClientError("invalid retry jitter") from exc
        if not math.isfinite(jitter):
            raise DeepcoinClientError("invalid retry jitter")
        return min(2.25, max(0.0, base + min(0.25, max(0.0, jitter))))

    @staticmethod
    def _record_attempt(
        *,
        recorder: Callable[[RequestAttemptFact], None] | None,
        fact: RequestAttemptFact,
    ) -> None:
        if recorder is None:
            return
        try:
            recorder(fact)
        except Exception as exc:
            sent = not (
                fact.outcome_certainty == OutcomeCertainty.NOT_SENT
            )
            evidence_failure = FailureFact(
                category=ErrorCategory.STATE_CONFLICT,
                outcome_certainty=(
                    OutcomeCertainty.UNKNOWN
                    if sent
                    else OutcomeCertainty.NOT_SENT
                ),
                retryable=False,
                safe_code="attempt_evidence_unavailable",
            )
            if fact.method == "POST":
                if sent:
                    raise DeepcoinRequestOutcomeUnknown(
                        "Deepcoin request outcome unknown",
                        fact=evidence_failure,
                    ) from None
                raise DeepcoinPreSendUnavailable(
                    evidence_failure.safe_code,
                    fact=evidence_failure,
                ) from None
            raise DeepcoinReadUnavailable(
                evidence_failure.safe_code,
                fact=evidence_failure,
            ) from None


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


def build_deepcoin_client_from_env(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | Path] | None = None,
) -> DeepcoinRestClient:
    environment = _load_deepcoin_environment(
        environ=environ,
        env_file_paths=env_file_paths,
    )
    credentials = load_deepcoin_credentials(
        environ=environment,
        env_file_paths=[],
    )
    governor = build_deepcoin_request_governor_from_environment(
        base_url=credentials.base_url,
        api_key=credentials.api_key,
        environ=environment,
    )
    return DeepcoinRestClient(
        credentials,
        request_governor=governor,
    )


def build_deepcoin_read_only_client_from_env(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | Path] | None = None,
) -> DeepcoinRestClient:
    """Build an authenticated client whose transport refuses every write."""

    environment = _load_deepcoin_environment(
        environ=environ,
        env_file_paths=env_file_paths,
    )
    credentials = load_deepcoin_credentials(
        environ=environment,
        env_file_paths=[],
    )
    governor = build_deepcoin_request_governor_from_environment(
        base_url=credentials.base_url,
        api_key=credentials.api_key,
        environ=environment,
    )
    return DeepcoinRestClient(
        credentials,
        request_governor=governor,
        read_only=True,
    )


def build_deepcoin_monitor_snapshot_client_from_env(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | Path] | None = None,
) -> DeepcoinRestClient:
    """Build the isolated monitor transport without ambient proxy/CA trust."""

    environment = _load_deepcoin_environment(
        environ=environ,
        env_file_paths=env_file_paths,
    )
    credentials = load_deepcoin_credentials(
        environ=environment,
        env_file_paths=[],
    )
    governor = build_deepcoin_request_governor_from_environment(
        base_url=credentials.base_url,
        api_key=credentials.api_key,
        environ=environment,
    )
    return DeepcoinRestClient(
        credentials,
        request_governor=governor,
        read_only=True,
        trust_env=False,
    )


def _load_deepcoin_environment(
    *,
    environ: dict[str, str] | None,
    env_file_paths: list[str | Path] | None,
) -> dict[str, str]:
    paths = (
        [".env", "config/telegram.env"]
        if env_file_paths is None
        else env_file_paths
    )
    environment = {} if paths == [] else dict(_load_env_file_values(paths))
    environment.update(os.environ if environ is None else environ)
    return environment


def _raise_for_deepcoin_business_error(payload: dict[str, Any]) -> None:
    for item in _iter_deepcoin_payload_items(payload.get("data")):
        s_code = str(item.get("sCode", "0"))
        if s_code not in {"0", ""}:
            raise DeepcoinDefiniteRejection(
                f"Deepcoin API error {s_code}: {item.get('sMsg') or item.get('msg')}"
            )


def _payload_business_code(payload: dict[str, Any]) -> str | None:
    code = _validated_business_code(payload.get("code", "0"))
    if code not in {"0", ""}:
        return code
    for item in _iter_deepcoin_payload_items(payload.get("data")):
        s_code = _validated_business_code(item.get("sCode", "0"))
        if s_code not in {"0", ""}:
            return s_code
    return None


def _governor_failure_fact(exc: DeepcoinGovernorError) -> FailureFact:
    if isinstance(exc, DeepcoinGovernorDeadlineExceeded):
        return FailureFact(
            category=ErrorCategory.TRANSPORT_TIMEOUT,
            outcome_certainty=OutcomeCertainty.NOT_SENT,
            retryable=False,
            safe_code="governor_deadline_exceeded",
        )
    if isinstance(exc, DeepcoinGovernorStateError):
        return FailureFact(
            category=ErrorCategory.STATE_CONFLICT,
            outcome_certainty=OutcomeCertainty.NOT_SENT,
            retryable=False,
            safe_code="governor_state_unavailable",
        )
    return FailureFact(
        category=ErrorCategory.STATE_CONFLICT,
        outcome_certainty=OutcomeCertainty.NOT_SENT,
        retryable=False,
        safe_code="governor_unavailable",
    )


def _request_deadline_exhausted_fact() -> FailureFact:
    return FailureFact(
        category=ErrorCategory.TRANSPORT_TIMEOUT,
        outcome_certainty=OutcomeCertainty.NOT_SENT,
        retryable=False,
        safe_code="request_deadline_exceeded",
    )


def _monitor_response_size_failure() -> FailureFact:
    return FailureFact(
        category=ErrorCategory.SCHEMA_INVALID,
        outcome_certainty=OutcomeCertainty.UNKNOWN,
        retryable=False,
        safe_code="monitor_response_size_exceeded",
    )


def _read_bounded_monitor_response(response: Any, *, limit: int) -> bytes:
    raw_content_length = response.headers.get("Content-Length")
    if raw_content_length not in (None, ""):
        try:
            declared_length = int(raw_content_length)
        except (TypeError, ValueError):
            declared_length = -1
        if declared_length > limit:
            raise _MonitorResponseSizeExceeded(
                "monitor response content length exceeds limit"
            )
    payload = bytearray()
    for chunk in response.iter_raw():
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise DeepcoinClientError("monitor response chunk is invalid")
        if len(payload) + len(chunk) > limit:
            raise _MonitorResponseSizeExceeded(
                "monitor response body exceeds limit"
            )
        payload.extend(chunk)
    return bytes(payload)


def _validated_business_code(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("business code schema invalid")
    code = str(value).strip()
    if len(code) > 64 or any(
        not (character.isascii() and (character.isalnum() or character in {"_", "-"}))
        for character in code
    ):
        raise ValueError("business code schema invalid")
    return code


def _response_schema_valid(
    *,
    method: str,
    normalized_path: str,
    payload: dict[str, Any],
) -> bool:
    if method != "GET" or normalized_path not in _DEEPCOIN_LIST_READ_PATHS:
        return True
    data = payload.get("data")
    return isinstance(data, list) and all(isinstance(row, dict) for row in data)


def _transport_safe_code(exc: httpx.RequestError) -> str:
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, httpx.WriteTimeout):
        return "write_timeout"
    if isinstance(exc, httpx.PoolTimeout):
        return "pool_timeout"
    return "transport_failure"


def _bounded_optional_deadline(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise DeepcoinClientError("invalid request deadline") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise DeepcoinClientError("invalid request deadline")
    return parsed


def _bounded_optional_response_limit(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeepcoinClientError("invalid monitor response byte limit")
    if not (1 <= value <= 16 * 1024 * 1024):
        raise DeepcoinClientError("invalid monitor response byte limit")
    return value


def _remaining_seconds(deadline: float | None, now: float) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - now)


def _bounded_retry_after(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return min(60.0, parsed)


def _bounded_label(value: object, *, fallback: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in {"_", "-"} else "_"
        for character in str(value or "").strip().lower()
    ).strip("_")
    return (normalized or fallback)[:128]


def _bounded_optional_label(value: object) -> str | None:
    if value is None:
        return None
    return _bounded_label(value, fallback="unspecified")


def _elapsed_ms(started: float, finished: float) -> int:
    return _milliseconds(max(0.0, finished - started))


def _milliseconds(seconds: float) -> int:
    return max(0, round(float(seconds) * 1000))


def _attempt_fact(
    *,
    ordinal: int,
    method: str,
    normalized_path: str,
    phase: str,
    priority: RequestPriority,
    correlation_id: str | None,
    failure: FailureFact,
    governor_wait_ms: int,
    retry_delay_ms: int,
    latency_ms: int,
    business_code: str | None = None,
) -> RequestAttemptFact:
    return RequestAttemptFact(
        ordinal=ordinal,
        method=method,
        normalized_path=normalized_path,
        phase=phase,
        priority=priority,
        correlation_id=correlation_id,
        outcome_certainty=failure.outcome_certainty,
        error_category=failure.category,
        safe_code=failure.safe_code,
        http_status=failure.http_status,
        business_code=business_code,
        governor_wait_ms=max(0, int(governor_wait_ms)),
        retry_delay_ms=max(0, int(retry_delay_ms)),
        latency_ms=max(0, int(latency_ms)),
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


def _ticker_timestamp_iso(value: Any) -> str | None:
    try:
        raw = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not raw.is_finite() or raw <= 0:
        return None
    seconds = raw / Decimal("1000") if raw >= Decimal("100000000000") else raw
    try:
        return datetime.fromtimestamp(float(seconds), tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _path_with_query(path: str, params: dict[str, Any]) -> str:
    filtered = {
        key: value
        for key, value in params.items()
        if value not in (None, "")
    }
    if not filtered:
        return path
    return f"{path}?{urlencode(filtered)}"


def _optional_exact_exchange_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SAFE_EXACT_EXCHANGE_ID.fullmatch(value) is None:
        raise DeepcoinClientError("invalid exact exchange order id")
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    if any(
        marker in normalized
        for marker in (
            "authorization",
            "bearer",
            "dcaccesskey",
            "dcaccesspassphrase",
            "dcaccesssign",
            "privatekey",
            "secret",
            "token",
        )
    ):
        raise DeepcoinClientError("invalid exact exchange order id")
    return value


def _optional_history_limit(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise DeepcoinClientError("history limit must be an integer from 1 to 100")
    return value


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

"""Deepcoin REST client helpers for authenticated trading requests."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import time
import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
# V1 is undocumented and returns an empty list for live regular limit orders
# (phase 5 experiment, 2026-09-07). It is kept only to name the retired path.
DEEPCOIN_ORDERS_PENDING_PATH = "/deepcoin/trade/orders-pending"
DEEPCOIN_ORDERS_PENDING_V2_PATH = "/deepcoin/trade/v2/orders-pending"
# Official "获取未成交订单列表": index is a 1-based page number, limit maxes at 100.
DEEPCOIN_ORDERS_PENDING_V2_FIRST_PAGE_INDEX = 1
DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT = 100
DEEPCOIN_ORDERS_PENDING_V2_MAX_PAGES = 200
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
DEEPCOIN_LISTENKEY_ACQUIRE_PATH = "/deepcoin/listenkey/acquire"


class DeepcoinClientError(RuntimeError):
    """Raised when Deepcoin credentials or API responses are invalid."""


class DeepcoinRequestOutcomeUnknown(DeepcoinClientError):
    """Raised when a write may have reached Deepcoin but no result was received."""


class DeepcoinDefiniteRejection(DeepcoinClientError):
    """Raised only when Deepcoin explicitly rejects a validated request."""


class DeepcoinRateLimited(DeepcoinClientError):
    """Raised when Deepcoin refused a read because the API quota was exhausted.

    Deepcoin signals frequency limiting as HTTP ``401`` carrying the body
    ``{"code":"50000","msg":"Trigger the api frequency limiting"}`` -- a status
    that otherwise means authentication failure. ``50000`` is not in the
    published error-code table; the pairing was measured in production on
    2026-09-07 together with ``X-Ratelimit-Limit: 5 / Window: 1s /
    Retry-After: 1``.

    Only the ``401`` + ``50000`` pair is rate limiting. Every other ``401``
    stays an ordinary failure so that a genuinely broken signature is never
    silently retried as congestion.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


DEEPCOIN_RATE_LIMIT_HTTP_STATUS = 401
DEEPCOIN_RATE_LIMIT_CODE = "50000"
# Used when the exchange rate-limits without a parseable ``Retry-After``. The
# measured window is one second, so waiting one second is the documented
# behaviour rather than a guess.
DEEPCOIN_RATE_LIMIT_DEFAULT_RETRY_AFTER_SECONDS = 1.0
# Hard ceiling on how long one GET may wait before its single retry. A read
# that cannot be served inside this budget is reported as unavailable, which is
# "unknown", never "empty".
DEEPCOIN_RATE_LIMIT_MAX_RETRY_WAIT_SECONDS = 2.0
DEEPCOIN_RATE_LIMIT_MAX_RETRIES = 1


def _rate_limit_retry_after_seconds(response: Any) -> float | None:
    """Return the ``Retry-After`` a rate-limit response asked for, if any."""

    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    for header_name in ("Retry-After", "X-Ratelimit-Retry-After"):
        try:
            raw = headers.get(header_name)
        except Exception:
            return None
        if raw in (None, ""):
            continue
        try:
            seconds = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            return seconds
    return None


def _response_is_rate_limited(response: Any) -> bool:
    """True only for the measured ``401`` + body ``code=50000`` pairing."""

    if getattr(response, "status_code", None) != DEEPCOIN_RATE_LIMIT_HTTP_STATUS:
        return False
    try:
        payload = response.json()
    except Exception:
        # A 401 whose body is not JSON is an authentication failure as far as
        # anything here can tell. Guessing "rate limited" would turn a broken
        # signature into a silent retry loop.
        return False
    if not isinstance(payload, dict):
        return False
    return str(payload.get("code", "")).strip() == DEEPCOIN_RATE_LIMIT_CODE


def _require_list_data(payload: dict[str, Any], *, endpoint: str) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise DeepcoinClientError(f"invalid list response schema: {endpoint}")
    if not all(isinstance(row, dict) for row in data):
        raise DeepcoinClientError(f"invalid list row schema: {endpoint}")
    return data


# Explicit V1 -> V2 field mapping for ``list_open_orders``.
#
# Every field the codebase reads off a pending regular-order row, mapped to the
# field the documented V2 response carries.  The mapping is the identity on all
# of them: V2 renames nothing that is consumed here, so rows are handed to
# callers verbatim rather than rewritten.  The two real V1/V2 differences are
# therefore both on the request side and are handled in ``list_open_orders``:
#
#   * V1 took ``instType=SWAP``; V2 has no ``instType`` request parameter and
#     returns every product category when ``instId`` is omitted, so the SWAP
#     restriction moves client-side (``_open_order_row_is_swap``).
#   * V1 was unpaginated; V2 requires a 1-based ``index`` page number.
#
# V2 additionally carries ``category`` and ``source`` which V1 did not document.
# They are passed through untouched; nothing here depends on them.
DEEPCOIN_OPEN_ORDER_V1_TO_V2_FIELDS: dict[str, str] = {
    "instType": "instType",
    "instId": "instId",
    "ordId": "ordId",
    "clOrdId": "clOrdId",
    "tag": "tag",
    "px": "px",
    "sz": "sz",
    "ordType": "ordType",
    "side": "side",
    "posSide": "posSide",
    "tdMode": "tdMode",
    "accFillSz": "accFillSz",
    "fillPx": "fillPx",
    "fillSz": "fillSz",
    "fillTime": "fillTime",
    "avgPx": "avgPx",
    "state": "state",
    "lever": "lever",
    "tpTriggerPx": "tpTriggerPx",
    "tpOrdPx": "tpOrdPx",
    "slTriggerPx": "slTriggerPx",
    "slOrdPx": "slOrdPx",
    "uTime": "uTime",
    "cTime": "cTime",
}


def _open_order_row_is_swap(row: dict[str, Any]) -> bool:
    """Reproduce V1's ``instType=SWAP`` request filter on the V2 response.

    A row whose ``instType`` is absent or empty cannot be classified, so it is
    kept: an unclassifiable row is unknown, and dropping it would silently
    shrink a snapshot that callers read as "what is live on the exchange".
    """

    inst_type = str(row.get("instType") or "").strip()
    return not inst_type or inst_type.upper() == "SWAP"


def _open_order_page_identity(page: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        f"{row.get('ordId') or ''}|{row.get('clOrdId') or ''}|{row.get('cTime') or ''}"
        for row in page
    )


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

    def read_order_history(self, *, inst_id: str | None = None) -> dict[str, Any]:
        """Return raw regular-order history for completeness auditing."""

    def list_trade_fills(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        """Return recent trade fills, optionally filtered by instrument."""

    def list_trade_fills_by_order_id(
        self,
        *,
        inst_id: str,
        order_id: str,
    ) -> list[dict[str, Any]]:
        """Return fills for one exact exchange order identifier."""

    def list_trigger_orders_pending(self, *, inst_id: str) -> list[dict[str, Any]]:
        """Return pending trigger / TPSL orders for one instrument."""

    def read_trigger_orders_pending(self, *, inst_id: str) -> dict[str, Any]:
        """Return the raw pending-trigger response for completeness auditing."""

    def list_trigger_order_history(self, *, inst_id: str) -> list[dict[str, Any]]:
        """Return historical trigger / TPSL orders for one instrument."""

    def read_trigger_order_history(self, *, inst_id: str) -> dict[str, Any]:
        """Return raw trigger-order history for completeness auditing."""

    def list_trigger_order_history_by_order_id(
        self,
        *,
        inst_id: str,
        order_id: str,
    ) -> list[dict[str, Any]]:
        """Return trigger history for one exact exchange order identifier."""

    def get_ticker_price(self, *, inst_id: str) -> float | None:
        """Return the latest ticker price for one instrument."""

    def get_ticker_quote(self, *, inst_id: str) -> dict[str, str] | None:
        """Return structured latest-price evidence for one instrument."""

    def list_swap_symbols(self) -> list[dict[str, str]]:
        """Return tradable SWAP base symbols and instrument ids."""

    def list_swap_instruments(self) -> list[dict[str, Any]]:
        """Return raw SWAP product information for contract-spec validation."""

    def acquire_listen_key(self) -> str:
        """Return one private WebSocket listen key. The value is a credential."""


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


# Deepcoin allows 5 requests per second per API key, measured across the whole
# account rather than per process. The three runtime roles are three operating
# system processes and no in-process limiter can see the other two, so each one
# can only hold a fixed share of the account quota.
#
# The shares are split by role rather than evenly, because the read load is
# nothing like even: `worker` is the only role with a continuous read loop
# (deepcoin_reconcile plus the shadow pass), while `web` and `ingest` read only
# on demand -- a manual API call, a resync after a dropped stream. An even 2/2/2
# split was measured on 2026-09-07 to roughly halve the worker's throughput:
# reconcile rounds went from a 13.8s median to 32-44s and the round-to-round
# interval from ~44s to ~65s, which is protection-convergence latency paid for
# headroom the other two roles were not using.
#
# 3 + 1 + 1 = 5 exactly. There is no spare request per second left for writes:
# writes are rare and bursty and take their own limiter, and a write that does
# collide with the ceiling is a 401 whose handling (unknown outcome, never
# retried) is already correct.
#
# `all` is the local single-process development mode; it runs all three roles'
# loops, so it holds all three shares. Anything else -- an operator CLI tool, an
# ad-hoc script -- runs *alongside* the three services rather than instead of
# them, so it takes the smallest share: its requests are added to an account
# that is already fully allocated.
#
# The rationale is repeated in docs/ARCHITECTURE.md 4.6; change both together.
DEEPCOIN_READ_LIMIT_PER_SECOND_BY_ROLE: dict[str, int] = {
    "worker": 3,
    "web": 1,
    "ingest": 1,
    "all": 5,
}
DEEPCOIN_READ_LIMIT_UNKNOWN_ROLE_PER_SECOND = 1
DEEPCOIN_RUNTIME_ROLE_ENV_VAR = "TELEGRAM_KOL_RUNTIME_ROLE"


def deepcoin_read_limit_per_second(role: str | None = None) -> int:
    """Return this process's share of the account-wide 5 reads per second.

    Fixed constants per role, resolved once when the limiter is built. There is
    deliberately no runtime setting: the shares are only safe as a set, and a
    per-process switch would let one role be raised without the others being
    lowered, which is exactly how the account ceiling gets breached.
    """

    if role is None:
        role = os.environ.get(DEEPCOIN_RUNTIME_ROLE_ENV_VAR, "")
    return DEEPCOIN_READ_LIMIT_PER_SECOND_BY_ROLE.get(
        str(role or "").strip().lower(),
        DEEPCOIN_READ_LIMIT_UNKNOWN_ROLE_PER_SECOND,
    )


class DeepcoinReadRateLimiter:
    """Thread-safe token bucket paced per *physical* Deepcoin GET request.

    Same shape as :class:`DeepcoinTpslWriteLimiter` -- one process-wide
    instance per credential scope, injectable clock and sleep for tests -- but
    it counts HTTP requests rather than logical calls. That distinction is
    load-bearing since ``list_open_orders`` moved to the paginated V2 endpoint:
    one logical read expands into one request per page, and a limiter that
    charged the call rather than the page would under-count the true rate by
    exactly the page count.
    """

    def __init__(
        self,
        *,
        monotonic_factory: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        per_second: int | None = None,
        metrics: "DeepcoinRateLimitMetrics | None" = None,
    ) -> None:
        self._clock = monotonic_factory
        self._sleep = sleep_fn
        self._per_second = max(
            1,
            int(deepcoin_read_limit_per_second() if per_second is None else per_second),
        )
        self._capacity = float(self._per_second)
        self._tokens = float(self._per_second)
        self._refilled_at = monotonic_factory()
        self._lock = threading.Lock()
        self._metrics = metrics

    @property
    def per_second(self) -> int:
        return self._per_second

    def acquire(self) -> None:
        """Consume one token, sleeping until one is available."""

        # Timed around the lock, not just around the sleeps inside it. A thread
        # that waits for another thread's sleep to finish has lost exactly as
        # much wall time to the quota as one that slept itself, and counting
        # only its own sleep would report a quota as free while it serialised
        # every reader in the process.
        started_at = self._clock()
        with self._lock:
            while True:
                now = self._clock()
                elapsed = max(0.0, now - self._refilled_at)
                self._refilled_at = now
                self._tokens = min(
                    self._capacity, self._tokens + elapsed * self._per_second
                )
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    break
                self._sleep((1.0 - self._tokens) / self._per_second)
        # Recorded outside the lock: this is the only measurement that says
        # whether the quota is actually binding. Without it "reads are slower"
        # cannot be told apart from "the quota is not the bottleneck".
        if self._metrics is not None:
            self._metrics.record_read_request(max(0.0, self._clock() - started_at))


_READ_LIMITERS_LOCK = threading.Lock()
_READ_LIMITERS: dict[tuple[str, str], DeepcoinReadRateLimiter] = {}


def _shared_read_limiter(credentials: DeepcoinCredentials) -> DeepcoinReadRateLimiter:
    """Return the process-wide read limiter for one API credential scope."""

    key = (credentials.base_url.rstrip("/"), credentials.api_key)
    with _READ_LIMITERS_LOCK:
        limiter = _READ_LIMITERS.get(key)
        if limiter is None:
            limiter = DeepcoinReadRateLimiter(metrics=_RATE_LIMIT_METRICS)
            _READ_LIMITERS[key] = limiter
        return limiter


_RATE_LIMIT_METRICS_WINDOW_SECONDS = 3600.0


class DeepcoinRateLimitMetrics:
    """Process-local rolling hour of rate-limit hits and the waits they caused.

    Counters only -- no path, no instrument, no body. They answer "is this
    process still being throttled, and how much wall time is it losing to it",
    which is what the phase 5b health check compares before and after.
    """

    def __init__(self, *, wall_clock: Callable[[], float] = time.time) -> None:
        self._wall_clock = wall_clock
        self._rate_limited: deque[float] = deque()
        self._retry_waits: deque[tuple[float, float]] = deque()
        self._read_requests: deque[tuple[float, float]] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - _RATE_LIMIT_METRICS_WINDOW_SECONDS
        while self._rate_limited and self._rate_limited[0] < cutoff:
            self._rate_limited.popleft()
        while self._retry_waits and self._retry_waits[0][0] < cutoff:
            self._retry_waits.popleft()
        while self._read_requests and self._read_requests[0][0] < cutoff:
            self._read_requests.popleft()

    def record_read_request(self, throttled_seconds: float) -> None:
        """One physical GET took a token, after waiting this long for it."""

        now = self._wall_clock()
        with self._lock:
            self._prune(now)
            self._read_requests.append((now, max(0.0, float(throttled_seconds))))

    def record_rate_limited(self) -> None:
        now = self._wall_clock()
        with self._lock:
            self._prune(now)
            self._rate_limited.append(now)

    def record_retry_after_wait(self, seconds: float) -> None:
        now = self._wall_clock()
        with self._lock:
            self._prune(now)
            self._retry_waits.append((now, max(0.0, float(seconds))))

    def snapshot(self) -> dict[str, Any]:
        now = self._wall_clock()
        with self._lock:
            self._prune(now)
            waits = [seconds for _, seconds in self._retry_waits]
            throttles = [seconds for _, seconds in self._read_requests]
            return {
                "rate_limited_last_hour": len(self._rate_limited),
                "retry_after_waits_last_hour": len(waits),
                "retry_after_wait_seconds_last_hour": round(sum(waits), 3),
                # Demand and cost. ``read_requests`` is what this process
                # actually asked the exchange for; ``throttled_seconds`` is the
                # wall time the quota made it wait. A near-zero throttle with a
                # slow loop means the quota is not the bottleneck.
                "read_requests_last_hour": len(throttles),
                "read_throttled_seconds_last_hour": round(sum(throttles), 3),
            }


_RATE_LIMIT_METRICS = DeepcoinRateLimitMetrics()


def deepcoin_rate_limit_metrics() -> DeepcoinRateLimitMetrics:
    """Return this process's rate-limit counters."""

    return _RATE_LIMIT_METRICS


# GET paths whose response one reconcile round may reuse. Deliberately short:
# these three are the reads the round issues repeatedly for the same
# instrument, and each is a whole-snapshot read whose meaning does not depend
# on when inside the round it was taken. History and fills are excluded because
# nothing re-reads them within a round, and every write path is excluded by
# construction -- the cache lives on GET only.
DEEPCOIN_ROUND_CACHEABLE_READ_PATHS: frozenset[str] = frozenset(
    {
        DEEPCOIN_ACCOUNT_POSITIONS_PATH,
        DEEPCOIN_TRIGGER_ORDERS_PENDING_PATH,
        DEEPCOIN_ORDERS_PENDING_V2_PATH,
    }
)


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
        read_rate_limiter: "DeepcoinReadRateLimiter | None" = None,
        rate_limit_metrics: "DeepcoinRateLimitMetrics | None" = None,
    ) -> None:
        self._credentials = credentials
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._http_client_lock = threading.Lock()
        self._closed = False
        self._reuse_http_client = False
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
        if read_rate_limiter is not None:
            self._read_rate_limiter = read_rate_limiter
        elif monotonic_factory is not None or sleep_fn is not None:
            # Same rule as the write limiter: an explicit clock is a test or
            # integration scope and must stay deterministic and unshared.
            self._read_rate_limiter = DeepcoinReadRateLimiter(
                monotonic_factory=self._monotonic_factory,
                sleep_fn=self._sleep_fn,
            )
        else:
            self._read_rate_limiter = _shared_read_limiter(credentials)
        self._rate_limit_metrics = rate_limit_metrics or _RATE_LIMIT_METRICS
        # ``None`` means "no round is open"; reads then always go to the
        # exchange. Only an explicit ``round_read_cache()`` scope opens one.
        self._round_read_cache: dict[str, dict[str, Any]] | None = None
        self._round_read_cache_lock = threading.Lock()

    def begin_round_read_cache(self) -> Any:
        """Open a round scope in which the three repeated reads are served once.

        One ``deepcoin_reconcile`` round re-reads the same ``positions`` /
        ``trigger-orders-pending`` / ``orders-pending`` snapshot several times
        while it walks its ledgers, and every repeat spends a token and can
        draw a 401. Inside a scope the first read of a given path goes to the
        exchange and later identical reads reuse its response.

        The scope is the round and nothing wider. It is discarded outright by
        any write issued through this client, so a post-write re-read is never
        served a pre-write snapshot, and :meth:`end_round_read_cache` drops it
        whatever happened. Exchange state is never carried between rounds.

        Returns an opaque token to hand back to :meth:`end_round_read_cache`.
        """

        with self._round_read_cache_lock:
            previous = self._round_read_cache
            self._round_read_cache = {}
        return previous

    def end_round_read_cache(self, token: Any = None) -> None:
        """Close the scope opened by :meth:`begin_round_read_cache`."""

        with self._round_read_cache_lock:
            self._round_read_cache = token

    @contextmanager
    def round_read_cache(self) -> "Iterator[None]":
        """Scope one round's repeated reads; see :meth:`begin_round_read_cache`."""

        token = self.begin_round_read_cache()
        try:
            yield
        finally:
            self.end_round_read_cache(token)

    def _invalidate_round_read_cache(self) -> None:
        """Drop every cached read; called by every write through this client."""

        with self._round_read_cache_lock:
            if self._round_read_cache is not None:
                self._round_read_cache = {}

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
            self._reuse_http_client = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

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
                self._http_client = httpx.Client(
                    base_url=self._credentials.base_url,
                    timeout=self._credentials.timeout_seconds,
                )
            return self._http_client

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
        """Return every pending regular SWAP order, paging V2 until exhausted.

        Fail-closed by construction: any page that raises leaves this method via
        the exception, so a partial set of pages is never returned.  Incomplete
        is unknown, not empty.
        """

        rows: list[dict[str, Any]] = []
        previous_page_identity: tuple[str, ...] | None = None
        index = DEEPCOIN_ORDERS_PENDING_V2_FIRST_PAGE_INDEX
        while True:
            payload = self._request(
                "GET",
                _path_with_query(
                    DEEPCOIN_ORDERS_PENDING_V2_PATH,
                    {
                        "instId": inst_id,
                        "index": index,
                        "limit": DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT,
                    },
                ),
            )
            page = _require_list_data(
                payload, endpoint=DEEPCOIN_ORDERS_PENDING_V2_PATH
            )
            page_identity = _open_order_page_identity(page)
            if page and page_identity == previous_page_identity:
                # The server ignored ``index``; treat a non-advancing cursor as
                # an unusable read rather than looping or truncating silently.
                raise DeepcoinClientError(
                    "orders-pending v2 pagination did not advance: "
                    f"{DEEPCOIN_ORDERS_PENDING_V2_PATH} index={index}"
                )
            previous_page_identity = page_identity
            rows.extend(row for row in page if _open_order_row_is_swap(row))
            if len(page) < DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT:
                return rows
            index += 1
            if (
                index - DEEPCOIN_ORDERS_PENDING_V2_FIRST_PAGE_INDEX
                >= DEEPCOIN_ORDERS_PENDING_V2_MAX_PAGES
            ):
                raise DeepcoinClientError(
                    "orders-pending v2 pagination exceeded "
                    f"{DEEPCOIN_ORDERS_PENDING_V2_MAX_PAGES} pages"
                )

    def list_order_history(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_ORDERS_HISTORY_PATH,
                {"instType": "SWAP", "instId": inst_id},
            ),
        )
        return _require_list_data(payload, endpoint=DEEPCOIN_ORDERS_HISTORY_PATH)

    def read_order_history(self, *, inst_id: str | None = None) -> dict[str, Any]:
        return self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_ORDERS_HISTORY_PATH,
                {"instType": "SWAP", "instId": inst_id, "limit": 100},
            ),
        )

    def list_trade_fills(self, *, inst_id: str | None = None) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_TRADE_FILLS_PATH,
                {"instType": "SWAP", "instId": inst_id},
            ),
        )
        return _require_list_data(payload, endpoint=DEEPCOIN_TRADE_FILLS_PATH)

    def list_trade_fills_by_order_id(
        self,
        *,
        inst_id: str,
        order_id: str,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_TRADE_FILLS_PATH,
                {"instType": "SWAP", "instId": inst_id, "ordId": order_id},
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

    def read_trigger_order_history(self, *, inst_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_TRIGGER_ORDERS_HISTORY_PATH,
                {"instType": "SWAP", "instId": inst_id, "limit": 100},
            ),
        )

    def list_trigger_order_history_by_order_id(
        self,
        *,
        inst_id: str,
        order_id: str,
    ) -> list[dict[str, Any]]:
        """**Unreliable: the exchange ignores the ``ordId`` filter.**

        Measured on 2026-09-08 (A-5b): this returns ``[]`` for every order id,
        including ids that are demonstrably present in the unfiltered history.
        An empty list from here therefore means *nothing* -- it does not mean
        "no such historical order" -- so a caller that reads it as absence is
        fail-open. Use :meth:`find_trigger_order_history_rows`, which pages the
        unfiltered endpoint and filters locally, and which says explicitly when
        it could not finish looking.

        Kept only so an existing caller does not break at import time.
        """

        payload = self._request(
            "GET",
            _path_with_query(
                DEEPCOIN_TRIGGER_ORDERS_HISTORY_PATH,
                {"instType": "SWAP", "instId": inst_id, "ordId": order_id},
            ),
        )
        return _require_list_data(payload, endpoint=DEEPCOIN_TRIGGER_ORDERS_HISTORY_PATH)

    def find_trigger_order_history_rows(
        self,
        *,
        inst_id: str,
        order_id: str,
        max_pages: int = 5,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return ``(exact matches, searched_to_the_end)`` from trigger history.

        The endpoint returns the newest hundred rows and pages backwards with
        ``after=<oldest id on the page>``. The second element is ``False`` when
        the page budget ran out before the history did, which is the caller's
        signal that "not found" is *unknown* rather than *absent*.
        """

        cursor: str | None = None
        for _ in range(max(1, int(max_pages))):
            query: dict[str, Any] = {"instType": "SWAP", "instId": inst_id}
            if cursor is not None:
                query["after"] = cursor
            payload = self._request(
                "GET", _path_with_query(DEEPCOIN_TRIGGER_ORDERS_HISTORY_PATH, query)
            )
            rows = _require_list_data(
                payload, endpoint=DEEPCOIN_TRIGGER_ORDERS_HISTORY_PATH
            )
            if not rows:
                return [], True
            matches = [
                dict(row)
                for row in rows
                if isinstance(row, dict)
                and str(order_id)
                in {
                    str(row.get(key)).strip()
                    for key in ("ordId", "orderId", "order_id", "id")
                    if row.get(key) not in (None, "")
                }
            ]
            if matches:
                return matches, True
            identifiers = [
                str(row.get("ordId") or "").strip()
                for row in rows
                if isinstance(row, dict) and str(row.get("ordId") or "").strip()
            ]
            if not identifiers:
                return [], True
            next_cursor = min(identifiers)
            if next_cursor == cursor:
                return [], True
            cursor = next_cursor
        return [], False

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

    def acquire_listen_key(self) -> str:
        """Return one private WebSocket listen key.

        The returned value is a credential: it authenticates the private stream
        on its own. Never log it, never persist it, and never include it (or the
        stream URL built from it) in exception text or evidence files.

        Deepcoin returns ``data`` either as an object or as a single-element
        list; both shapes carry ``listenkey``.

        Phase 2 hook: the key slides on a one-hour window, so a renewal /
        rotation loop belongs here. Phase 1 acquires it once per connection
        attempt and does not renew.
        """

        payload = self._request("GET", DEEPCOIN_LISTENKEY_ACQUIRE_PATH)
        data = payload.get("data")
        if isinstance(data, list):
            if len(data) != 1:
                raise DeepcoinClientError(
                    "invalid listenkey response schema: "
                    f"{DEEPCOIN_LISTENKEY_ACQUIRE_PATH}"
                )
            data = data[0]
        if not isinstance(data, dict):
            raise DeepcoinClientError(
                f"invalid listenkey response schema: {DEEPCOIN_LISTENKEY_ACQUIRE_PATH}"
            )
        listen_key = str(data.get("listenkey") or "").strip()
        if not listen_key:
            # Deliberately does not echo the response body: it holds the key.
            raise DeepcoinClientError(
                f"listenkey missing from response: {DEEPCOIN_LISTENKEY_ACQUIRE_PATH}"
            )
        return listen_key

    def _request(
        self,
        method: str,
        request_path: str,
        body_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue one logical Deepcoin call: cache, limiter, then the request.

        Order matters. A round-cache hit is served without touching the token
        bucket because no HTTP request leaves the process; every physical
        request that does leave -- including each page of a paginated read and
        including the one retry -- charges exactly one token.
        """

        if method.upper() != "GET":
            # Any write invalidates this client's round cache before it is
            # issued, so a read racing the write cannot repopulate the cache
            # with pre-write state.
            self._invalidate_round_read_cache()
            return self._request_with_rate_limit(method, request_path, body_payload)

        cache = self._round_read_cache
        cacheable = (
            cache is not None
            and request_path.split("?", 1)[0] in DEEPCOIN_ROUND_CACHEABLE_READ_PATHS
        )
        if cacheable:
            hit = cache.get(request_path)
            if hit is not None:
                return copy.deepcopy(hit)
        payload = self._request_with_rate_limit(method, request_path, body_payload)
        if cacheable:
            with self._round_read_cache_lock:
                # Only store into the generation this read started in: a write
                # that landed meanwhile has already replaced it, and this
                # response predates that write.
                if self._round_read_cache is cache:
                    cache[request_path] = copy.deepcopy(payload)
        return payload

    def _request_with_rate_limit(
        self,
        method: str,
        request_path: str,
        body_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Pace GETs and retry one rate-limited GET; never retry a write."""

        is_read = method.upper() == "GET"
        if not is_read:
            return self._request_once(method, request_path, body_payload)

        attempts = DEEPCOIN_RATE_LIMIT_MAX_RETRIES + 1
        for attempt in range(attempts):
            self._read_rate_limiter.acquire()
            try:
                return self._request_once(method, request_path, body_payload)
            except DeepcoinRateLimited as exc:
                if attempt == attempts - 1:
                    raise
                wait_seconds = exc.retry_after
                if wait_seconds is None:
                    wait_seconds = DEEPCOIN_RATE_LIMIT_DEFAULT_RETRY_AFTER_SECONDS
                wait_seconds = min(
                    max(0.0, float(wait_seconds)),
                    DEEPCOIN_RATE_LIMIT_MAX_RETRY_WAIT_SECONDS,
                )
                self._rate_limit_metrics.record_retry_after_wait(wait_seconds)
                if wait_seconds > 0:
                    # This client is synchronous and every runtime caller
                    # reaches it from a worker thread, so the wait never sits
                    # on an event loop.
                    self._sleep_fn(wait_seconds)
        raise AssertionError("unreachable")  # pragma: no cover

    def _request_once(
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

        owns_request_client = self._owns_http_client and not self._reuse_http_client
        client = (
            httpx.Client(
                base_url=self._credentials.base_url,
                timeout=self._credentials.timeout_seconds,
            )
            if owns_request_client
            else self._get_http_client()
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
            rate_limited = _response_is_rate_limited(exc.response)
            if rate_limited:
                self._rate_limit_metrics.record_rate_limited()
            if method.upper() == "POST":
                # A throttled write is still a write whose outcome is unknown:
                # the request may have been accepted before the limiter saw it.
                # Never retried, never reclassified.
                raise DeepcoinRequestOutcomeUnknown(
                    f"Deepcoin request outcome unknown after HTTP status: {exc}"
                ) from exc
            if rate_limited:
                raise DeepcoinRateLimited(
                    f"Deepcoin rate limited: {exc}",
                    retry_after=_rate_limit_retry_after_seconds(exc.response),
                ) from exc
            raise DeepcoinClientError(f"Deepcoin request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            if method.upper() == "POST":
                raise DeepcoinRequestOutcomeUnknown(
                    "Deepcoin write response was not JSON"
                ) from exc
            raise DeepcoinClientError("Deepcoin response was not JSON") from exc
        finally:
            if owns_request_client:
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


def build_deepcoin_client_from_env(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | Path] | None = None,
) -> DeepcoinRestClient:
    return DeepcoinRestClient(
        load_deepcoin_credentials(
            environ=environ,
            env_file_paths=env_file_paths,
        )
    )


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

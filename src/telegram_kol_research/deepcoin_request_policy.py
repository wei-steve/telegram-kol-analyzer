"""Closed request budgets and outcome facts for Deepcoin transport calls."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class RequestPriority(StrEnum):
    CRITICAL = "critical"
    NORMAL = "normal"
    BACKGROUND = "background"


class OutcomeCertainty(StrEnum):
    NOT_SENT = "not_sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    CONFIRMED = "confirmed"


class ErrorCategory(StrEnum):
    RATE_LIMITED = "rate_limited"
    TRANSPORT_TIMEOUT = "transport_timeout"
    HTTP_RETRYABLE = "http_retryable"
    AUTH_FAILED = "auth_failed"
    BUSINESS_REJECTED = "business_rejected"
    SNAPSHOT_INCOMPLETE = "snapshot_incomplete"
    SCHEMA_INVALID = "schema_invalid"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    STATE_CONFLICT = "state_conflict"


@dataclass(frozen=True, slots=True)
class RequestProfile:
    per_second: int
    per_minute: int
    background_per_second: int
    background_per_minute: int
    min_interval_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class FailureFact:
    category: ErrorCategory
    outcome_certainty: OutcomeCertainty
    retryable: bool
    safe_code: str
    http_status: int | None = None


_FIVE_PER_SECOND = RequestProfile(4, 120, 2, 60)
_TEN_PER_SECOND = RequestProfile(8, 240, 4, 120)
_FIFTEEN_PER_SECOND = RequestProfile(12, 360, 6, 180)
_STRICT_ONE_PER_SECOND = RequestProfile(1, 48, 1, 24, 1.25)

_FIVE_PER_SECOND_READ_PATHS = frozenset(
    {
        "/deepcoin/trade/orders-history",
        "/deepcoin/trade/fills",
        "/deepcoin/trade/trigger-orders-pending",
        "/deepcoin/trade/trigger-orders-history",
    }
)
_TEN_PER_SECOND_READ_PATHS = frozenset(
    {
        "/deepcoin/account/positions",
        "/deepcoin/trade/orders-pending",
    }
)
_STRICT_READ_PATHS = frozenset({"/deepcoin/account/positions-history"})
_FIFTEEN_PER_SECOND_WRITE_PATHS = frozenset(
    {
        "/deepcoin/trade/order",
        "/deepcoin/trade/cancel-order",
        "/deepcoin/trade/cancel-trigger-order",
        "/deepcoin/trade/replace-order-sltp",
        "/deepcoin/trade/trigger-order",
        "/deepcoin/trade/set-position-sltp",
        "/deepcoin/trade/cancel-position-sltp",
    }
)
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def normalize_request_path(request_path: str) -> str:
    """Return a query-free absolute path suitable for one endpoint budget."""

    parsed = urlsplit(str(request_path or ""))
    path = parsed.path.strip()
    if not path:
        return "/"
    return path if path.startswith("/") else f"/{path}"


def request_profile(method: str, request_path: str) -> RequestProfile:
    normalized_method = str(method or "").strip().upper()
    normalized_path = normalize_request_path(request_path)
    if normalized_method == "GET":
        if normalized_path in _STRICT_READ_PATHS:
            return _STRICT_ONE_PER_SECOND
        if normalized_path in _FIVE_PER_SECOND_READ_PATHS:
            return _FIVE_PER_SECOND
        if normalized_path in _TEN_PER_SECOND_READ_PATHS:
            return _TEN_PER_SECOND
    if (
        normalized_method == "POST"
        and normalized_path in _FIFTEEN_PER_SECOND_WRITE_PATHS
    ):
        return _FIFTEEN_PER_SECOND
    return _STRICT_ONE_PER_SECOND


def classify_http_failure(*, method: str, status_code: int) -> FailureFact:
    normalized_method = str(method or "").strip().upper()
    status = int(status_code)
    if status in {401, 403}:
        return FailureFact(
            category=ErrorCategory.AUTH_FAILED,
            outcome_certainty=(
                OutcomeCertainty.UNKNOWN
                if normalized_method == "GET"
                else OutcomeCertainty.REJECTED
            ),
            retryable=False,
            safe_code=f"http_{status}",
            http_status=status,
        )
    if status in _RETRYABLE_HTTP_STATUSES:
        return FailureFact(
            category=(
                ErrorCategory.RATE_LIMITED
                if status == 429
                else ErrorCategory.HTTP_RETRYABLE
            ),
            outcome_certainty=OutcomeCertainty.UNKNOWN,
            retryable=normalized_method == "GET",
            safe_code=f"http_{status}",
            http_status=status,
        )
    return FailureFact(
        category=ErrorCategory.BUSINESS_REJECTED,
        outcome_certainty=OutcomeCertainty.REJECTED,
        retryable=False,
        safe_code=f"http_{status}",
        http_status=status,
    )


def classify_transport_failure(
    *,
    method: str,
    sent: bool,
    code: str,
) -> FailureFact:
    normalized_method = str(method or "").strip().upper()
    request_was_sent = bool(sent)
    return FailureFact(
        category=ErrorCategory.TRANSPORT_TIMEOUT,
        outcome_certainty=(
            OutcomeCertainty.NOT_SENT
            if not request_was_sent
            else OutcomeCertainty.UNKNOWN
        ),
        retryable=normalized_method == "GET" or not request_was_sent,
        safe_code=_safe_code(code, fallback="transport_failure"),
    )


def classify_schema_failure(*, method: str, occurrence: int) -> FailureFact:
    normalized_method = str(method or "").strip().upper()
    terminal = int(occurrence) >= 2
    return FailureFact(
        category=(
            ErrorCategory.SCHEMA_INCOMPATIBLE
            if terminal
            else ErrorCategory.SCHEMA_INVALID
        ),
        outcome_certainty=OutcomeCertainty.UNKNOWN,
        retryable=normalized_method == "GET" and not terminal,
        safe_code=("schema_incompatible" if terminal else "schema_invalid"),
    )


def classify_business_failure(*, method: str, exchange_code: str) -> FailureFact:
    del method
    return FailureFact(
        category=ErrorCategory.BUSINESS_REJECTED,
        outcome_certainty=OutcomeCertainty.REJECTED,
        retryable=False,
        safe_code="business_rejected",
    )


def _safe_code(value: str, *, fallback: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in str(value or "").strip().lower()
    ).strip("_")
    return (normalized or fallback)[:128]

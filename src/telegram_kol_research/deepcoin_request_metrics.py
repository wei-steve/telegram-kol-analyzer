"""Bounded, aggregate-only health metrics for Deepcoin request evidence."""

from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import String, and_, cast, func, or_
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_execution_operations import (
    contains_credential_marker,
)
from telegram_kol_research.models import (
    DeepcoinExecutionOperation,
    DeepcoinRequestAttempt,
)
from telegram_kol_research.time_utils import normalize_to_utc_naive


class DeepcoinRequestMetricsIncomplete(RuntimeError):
    """The bounded evidence scan cannot support a complete projection."""


_SAFE_ENDPOINTS = frozenset(
    {
        "/deepcoin/account/positions",
        "/deepcoin/account/positions-history",
        "/deepcoin/market/instruments",
        "/deepcoin/market/tickers",
        "/deepcoin/trade/cancel-order",
        "/deepcoin/trade/cancel-position-sltp",
        "/deepcoin/trade/cancel-trigger-order",
        "/deepcoin/trade/fills",
        "/deepcoin/trade/order",
        "/deepcoin/trade/orders-history",
        "/deepcoin/trade/orders-pending",
        "/deepcoin/trade/replace-order-sltp",
        "/deepcoin/trade/set-position-sltp",
        "/deepcoin/trade/trigger-order",
        "/deepcoin/trade/trigger-orders-history",
        "/deepcoin/trade/trigger-orders-pending",
    }
)
_SAFE_PHASES = frozenset(
    {
        "entry_preflight",
        "entry_submit",
        "entry_readback",
        "protection_submit",
        "protection_readback",
        "next_leg_preflight",
        "reconciliation",
        "completed",
    }
)
_SAFE_METHODS = frozenset({"GET", "POST"})
_SAFE_PRIORITIES = frozenset({"critical", "normal", "background"})
_SAFE_CERTAINTIES = frozenset(
    {"not_sent", "accepted", "rejected", "unknown", "confirmed"}
)
_SAFE_ERROR_CATEGORIES = frozenset(
    {
        "rate_limited",
        "transport_timeout",
        "http_retryable",
        "auth_failed",
        "business_rejected",
        "snapshot_incomplete",
        "schema_invalid",
        "schema_incompatible",
        "state_conflict",
    }
)
_SAFE_OPERATION_STATES = frozenset(
    {
        "planned",
        "entry_prepared",
        "entry_submitting",
        "entry_pending_readback",
        "entry_unknown",
        "entry_rejected",
        "entry_confirmed",
        "protection_prepared",
        "protection_pending_readback",
        "protection_unknown",
        "protected",
        "next_leg_preflight",
        "pre_submit_deferred",
        "completed",
        "recovery_required",
        "submission_failed_no_exposure",
    }
)
_OPERATION_STATE_CERTAINTIES = {
    "planned": frozenset({"not_sent"}),
    "entry_prepared": frozenset({"not_sent"}),
    "entry_submitting": frozenset({"not_sent"}),
    "entry_pending_readback": frozenset({"accepted"}),
    "entry_unknown": frozenset({"unknown"}),
    "entry_rejected": frozenset({"rejected"}),
    "entry_confirmed": frozenset({"confirmed"}),
    "protection_prepared": frozenset({"not_sent", "confirmed"}),
    "protection_pending_readback": frozenset({"accepted"}),
    "protection_unknown": frozenset({"unknown"}),
    "protected": frozenset({"confirmed"}),
    "next_leg_preflight": frozenset({"not_sent"}),
    "pre_submit_deferred": frozenset({"not_sent"}),
    "completed": frozenset({"confirmed"}),
    "recovery_required": _SAFE_CERTAINTIES,
    "submission_failed_no_exposure": frozenset({"confirmed"}),
}
_LIVE_UNPROTECTED_ROOT_STATES = frozenset(
    {
        "entry_confirmed",
        "protection_prepared",
        "protection_pending_readback",
        "protection_unknown",
        "recovery_required",
    }
)
_UNAVAILABLE_CATEGORIES = frozenset(
    {
        "rate_limited",
        "transport_timeout",
        "http_retryable",
        "snapshot_incomplete",
    }
)
_RETRYABLE_CATEGORIES = frozenset(
    {
        "rate_limited",
        "transport_timeout",
        "http_retryable",
        "snapshot_incomplete",
        "schema_invalid",
    }
)
_NONRETRYABLE_CATEGORIES = frozenset(
    {
        "auth_failed",
        "business_rejected",
        "schema_incompatible",
        "state_conflict",
    }
)


def _bounded_limit(value: object, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4096:
        raise ValueError(code)
    return value


def _safe_nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DeepcoinRequestMetricsIncomplete("metric_numeric_evidence_invalid")
    return value


def _safe_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and 1 <= len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise DeepcoinRequestMetricsIncomplete(
                "metric_datetime_evidence_invalid"
            ) from None
    else:
        raise DeepcoinRequestMetricsIncomplete("metric_datetime_evidence_invalid")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_endpoint(value: object) -> str:
    candidate = value if isinstance(value, str) else ""
    if (
        candidate not in _SAFE_ENDPOINTS
        or contains_credential_marker(candidate)
    ):
        return "/deepcoin/unknown"
    return candidate


def _safe_phase(value: object) -> str:
    candidate = value if isinstance(value, str) else ""
    return candidate if candidate in _SAFE_PHASES else "unknown"


def _validate_attempt_row(row: object) -> None:
    if (
        row.method not in _SAFE_METHODS
        or row.priority not in _SAFE_PRIORITIES
        or row.phase not in _SAFE_PHASES
        or row.outcome_certainty not in _SAFE_CERTAINTIES
        or (
            row.error_category is not None
            and row.error_category not in _SAFE_ERROR_CATEGORIES
        )
        or not isinstance(row.safe_code, str)
        or not 1 <= len(row.safe_code) <= 128
        or contains_credential_marker(row.safe_code)
        or (
            row.http_status is not None
            and (
                isinstance(row.http_status, bool)
                or not isinstance(row.http_status, int)
                or not 100 <= row.http_status <= 599
            )
        )
    ):
        raise DeepcoinRequestMetricsIncomplete("metric_attempt_evidence_invalid")
    started_at = _safe_datetime(row.started_at_text)
    completed_at = _safe_datetime(row.completed_at_text)
    if completed_at < started_at:
        raise DeepcoinRequestMetricsIncomplete("metric_attempt_evidence_invalid")


def _validate_operation_row(row: object, *, until: datetime) -> None:
    if (
        row.state not in _SAFE_OPERATION_STATES
        or row.phase not in _SAFE_PHASES
        or row.outcome_certainty
        not in _OPERATION_STATE_CERTAINTIES.get(row.state, frozenset())
        or (
            row.error_category is not None
            and row.error_category not in _SAFE_ERROR_CATEGORIES
        )
        or (
            row.reason_code is not None
            and (
                not isinstance(row.reason_code, str)
                or not 1 <= len(row.reason_code) <= 128
                or contains_credential_marker(row.reason_code)
            )
        )
        or isinstance(row.attempt_count, bool)
        or not isinstance(row.attempt_count, int)
        or row.attempt_count < 0
        or isinstance(row.state_version, bool)
        or not isinstance(row.state_version, int)
        or row.state_version < 0
        or (
            row.parent_operation_id is not None
            and (
                isinstance(row.parent_operation_id, bool)
                or not isinstance(row.parent_operation_id, int)
                or row.parent_operation_id <= 0
            )
        )
    ):
        raise DeepcoinRequestMetricsIncomplete("metric_operation_evidence_invalid")
    created_at = _safe_datetime(row.created_at_text)
    updated_at = _safe_datetime(row.updated_at_text)
    if updated_at < created_at:
        raise DeepcoinRequestMetricsIncomplete("metric_operation_evidence_invalid")
    if row.writer_attempted_at_text is not None:
        writer_attempted_at = _safe_datetime(row.writer_attempted_at_text)
        if writer_attempted_at < created_at or writer_attempted_at > updated_at:
            raise DeepcoinRequestMetricsIncomplete(
                "metric_operation_evidence_invalid"
            )


def _nearest_rank(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, math.ceil(float(percentile) * len(ordered)))
    return ordered[min(rank - 1, len(ordered) - 1)]


def _distribution(values: list[int]) -> dict[str, int]:
    return {
        "p50": _nearest_rank(values, 0.50),
        "p95": _nearest_rank(values, 0.95),
        "max": max(values, default=0),
    }


def project_deepcoin_request_health(
    session_factory: sessionmaker,
    *,
    since: datetime,
    until: datetime,
    max_attempts: int = 4096,
    max_operations: int = 4096,
) -> dict[str, object]:
    """Project one complete bounded window without exposing durable identities."""

    attempt_limit = _bounded_limit(max_attempts, code="max_attempts_invalid")
    operation_limit = _bounded_limit(max_operations, code="max_operations_invalid")
    since_naive = normalize_to_utc_naive(since)
    until_naive = normalize_to_utc_naive(until)
    if (
        since_naive is None
        or until_naive is None
        or until_naive <= since_naive
    ):
        raise ValueError("metric_window_invalid")

    with session_factory() as session:
        attempt_started_text = cast(DeepcoinRequestAttempt.started_at, String)
        attempt_started_julian = func.julianday(attempt_started_text)
        attempt_completed_julian = func.julianday(
            cast(DeepcoinRequestAttempt.completed_at, String)
        )
        since_julian = func.julianday(since_naive.isoformat(sep=" "))
        until_julian = func.julianday(until_naive.isoformat(sep=" "))
        attempt_rows = (
            session.query(
                DeepcoinRequestAttempt.normalized_path,
                DeepcoinRequestAttempt.phase,
                DeepcoinRequestAttempt.priority,
                DeepcoinRequestAttempt.method,
                DeepcoinRequestAttempt.outcome_certainty,
                DeepcoinRequestAttempt.error_category,
                DeepcoinRequestAttempt.safe_code,
                DeepcoinRequestAttempt.http_status,
                DeepcoinRequestAttempt.governor_wait_ms,
                DeepcoinRequestAttempt.retry_delay_ms,
                DeepcoinRequestAttempt.latency_ms,
                cast(DeepcoinRequestAttempt.started_at, String).label(
                    "started_at_text"
                ),
                cast(DeepcoinRequestAttempt.completed_at, String).label(
                    "completed_at_text"
                ),
            )
            .filter(
                or_(
                    attempt_started_julian.between(
                        since_julian,
                        until_julian,
                    ),
                    attempt_started_julian.is_(None),
                    attempt_completed_julian.is_(None),
                    attempt_completed_julian < attempt_started_julian,
                ),
            )
            .order_by(
                attempt_started_julian.asc(),
                DeepcoinRequestAttempt.id.asc(),
            )
            .limit(attempt_limit + 1)
            .all()
        )
        if len(attempt_rows) > attempt_limit:
            raise DeepcoinRequestMetricsIncomplete("metric_attempt_scan_truncated")

        operation_created_text = cast(
            DeepcoinExecutionOperation.created_at,
            String,
        )
        operation_updated_text = cast(
            DeepcoinExecutionOperation.updated_at,
            String,
        )
        operation_writer_text = cast(
            DeepcoinExecutionOperation.writer_attempted_at,
            String,
        )
        operation_created_julian = func.julianday(operation_created_text)
        operation_updated_julian = func.julianday(operation_updated_text)
        operation_writer_julian = func.julianday(operation_writer_text)
        operation_rows = (
            session.query(
                DeepcoinExecutionOperation.parent_operation_id,
                DeepcoinExecutionOperation.phase,
                DeepcoinExecutionOperation.state,
                DeepcoinExecutionOperation.outcome_certainty,
                DeepcoinExecutionOperation.error_category,
                DeepcoinExecutionOperation.reason_code,
                DeepcoinExecutionOperation.attempt_count,
                DeepcoinExecutionOperation.state_version,
                cast(DeepcoinExecutionOperation.created_at, String).label(
                    "created_at_text"
                ),
                cast(DeepcoinExecutionOperation.writer_attempted_at, String).label(
                    "writer_attempted_at_text"
                ),
                cast(DeepcoinExecutionOperation.updated_at, String).label(
                    "updated_at_text"
                ),
            )
            .filter(
                or_(
                    and_(
                        operation_created_julian <= until_julian,
                        operation_updated_julian <= until_julian,
                        or_(
                            operation_updated_julian >= since_julian,
                            DeepcoinExecutionOperation.state.in_(
                                tuple(_LIVE_UNPROTECTED_ROOT_STATES)
                            ),
                        ),
                    ),
                    operation_created_julian.is_(None),
                    operation_updated_julian.is_(None),
                    operation_updated_julian < operation_created_julian,
                    and_(
                        DeepcoinExecutionOperation.writer_attempted_at.is_not(
                            None
                        ),
                        or_(
                            operation_writer_julian.is_(None),
                            operation_writer_julian < operation_created_julian,
                            operation_writer_julian > operation_updated_julian,
                        ),
                    ),
                ),
            )
            .order_by(
                operation_updated_julian.asc(),
                DeepcoinExecutionOperation.id.asc(),
            )
            .limit(operation_limit + 1)
            .all()
        )
        if len(operation_rows) > operation_limit:
            raise DeepcoinRequestMetricsIncomplete("metric_operation_scan_truncated")

    endpoint_phase_counts: Counter[tuple[str, str]] = Counter()
    waits: list[int] = []
    readback_latencies: list[int] = []
    retry_count = 0
    http_429_count = 0
    unavailable_count = 0
    schema_invalid_count = 0
    schema_incompatible_count = 0
    auth_failure_count = 0
    retryable_error_count = 0
    nonretryable_error_count = 0
    unknown_writer_count = 0
    circuit_windows: list[tuple[datetime, datetime]] = []

    for row in attempt_rows:
        _validate_attempt_row(row)
        endpoint = _safe_endpoint(row.normalized_path)
        phase = _safe_phase(row.phase)
        endpoint_phase_counts[(endpoint, phase)] += 1
        wait_ms = _safe_nonnegative_int(row.governor_wait_ms)
        retry_ms = _safe_nonnegative_int(row.retry_delay_ms)
        latency_ms = _safe_nonnegative_int(row.latency_ms)
        waits.append(wait_ms)
        if row.method == "GET" and latency_ms > 0:
            readback_latencies.append(latency_ms)
        if retry_ms > 0:
            retry_count += 1
        if row.http_status == 429:
            http_429_count += 1
        category = str(row.error_category or "")
        if category in _UNAVAILABLE_CATEGORIES:
            unavailable_count += 1
        if category == "schema_invalid":
            schema_invalid_count += 1
        if category == "schema_incompatible":
            schema_incompatible_count += 1
        if category == "auth_failed":
            auth_failure_count += 1
        if category in _RETRYABLE_CATEGORIES:
            retryable_error_count += 1
        if category in _NONRETRYABLE_CATEGORIES:
            nonretryable_error_count += 1
        if row.method == "POST" and row.outcome_certainty == "unknown":
            unknown_writer_count += 1
        if row.safe_code == "circuit_open":
            circuit_windows.append(
                (
                    _safe_datetime(row.started_at_text),
                    _safe_datetime(row.completed_at_text),
                )
            )

    until_aware = until_naive.replace(tzinfo=UTC)
    pre_submit_deferred_count = 0
    live_durations: list[int] = []
    for row in operation_rows:
        _validate_operation_row(row, until=until_aware)
        state = str(row.state or "")
        if state == "pre_submit_deferred":
            pre_submit_deferred_count += 1
        if row.parent_operation_id is not None or state not in _LIVE_UNPROTECTED_ROOT_STATES:
            continue
        if state == "recovery_required" and row.writer_attempted_at_text is None:
            continue
        started_at = _safe_datetime(
            row.writer_attempted_at_text or row.created_at_text
        )
        live_durations.append(max(0, int((until_aware - started_at).total_seconds())))

    circuit_duration_ms = sum(
        max(0, int((ended - started).total_seconds() * 1000))
        for started, ended in circuit_windows
    )
    window_seconds = int((until_naive - since_naive).total_seconds())
    return {
        "complete": True,
        "window": {
            "since": since_naive.replace(tzinfo=UTC).isoformat(),
            "until": until_naive.replace(tzinfo=UTC).isoformat(),
            "minutes": window_seconds // 60,
        },
        "request_count": len(attempt_rows),
        "request_count_by_endpoint_phase": [
            {"endpoint": endpoint, "phase": phase, "count": count}
            for (endpoint, phase), count in sorted(endpoint_phase_counts.items())
        ],
        "limiter_wait_ms": _distribution(waits),
        "retry_count": retry_count,
        "http_429_count": http_429_count,
        "unavailable_count": unavailable_count,
        "schema_invalid_count": schema_invalid_count,
        "schema_incompatible_count": schema_incompatible_count,
        "auth_failure_count": auth_failure_count,
        "retryable_error_count": retryable_error_count,
        "nonretryable_error_count": nonretryable_error_count,
        "readback_latency_ms": _distribution(readback_latencies),
        "open_circuit": {
            "available": bool(circuit_windows),
            "count": len(circuit_windows),
            "duration_ms": circuit_duration_ms,
        },
        "unknown_writer_count": unknown_writer_count,
        "pre_submit_deferred_count": pre_submit_deferred_count,
        "live_unprotected": {
            "count": len(live_durations),
            "max_duration_seconds": max(live_durations, default=0),
            "total_duration_seconds": sum(live_durations),
        },
    }

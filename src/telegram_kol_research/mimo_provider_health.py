"""Is the MiMo provider answering at all -- and has a person been told.

2026-09-12 (A line, step-18): the provider returned ``402 Payment Required``
494 times between 03:00Z and 17:42Z. Recognition stopped for fourteen hours,
116 messages were never recognised, and nobody was told. Two things made that
silence possible, and this module removes both:

* **"The provider will not serve us" looked exactly like "our request was
  bad".** Every failure was folded into one generic ``MiMo failed after 2
  attempts`` string and recorded as ``v1_authoritative_failed``. An empty
  balance is an operations event; a malformed request is a code defect. They
  need different people and different actions, so they are classified from the
  exception itself -- the HTTP status and the transport error type -- never
  from message text.
* **Nothing counted.** The failure count went from 1 to 494 and no threshold
  looked at it. The outage is now derived from the append-only
  ``mimo_recognition_attempts`` audit on every tick of the worker's gap
  recovery loop, so it is noticed without new messages arriving and survives a
  restart without any in-process state.

Cadence: the first unavailable attempt is alerted on the next tick, then once
per 30 minutes measured from the start of the outage, until an attempt shows
the provider answering again; that produces exactly one recovery notice. Both
are deduplicated on durable ``runtime_incidents`` rows, keyed by the outage's
start and the 30-minute bucket, so a restart neither repeats nor loses them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable, Sequence

import httpx
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import MimoRecognitionAttempt, RuntimeIncident


logger = logging.getLogger(__name__)

#: The provider did not serve the request: nothing we change in the request
#: would help. An outage, not a defect.
PROVIDER_UNAVAILABLE = "provider_unavailable"
#: The provider answered with an HTTP error about the request itself (400,
#: 404, 413, 422 ...). The provider is up; our request is the problem.
REQUEST_REJECTED = "request_rejected"
#: The provider answered 2xx but the payload failed validation. Also "up".
RESPONSE_INVALID = "response_invalid"

INSUFFICIENT_BALANCE = "insufficient_balance"
AUTH_REJECTED = "auth_rejected"
RATE_LIMITED = "rate_limited"
SERVER_ERROR = "server_error"
TIMEOUT = "timeout"
NETWORK_ERROR = "network_error"

PROVIDER_UNAVAILABLE_KINDS = frozenset(
    {
        INSUFFICIENT_BALANCE,
        AUTH_REJECTED,
        RATE_LIMITED,
        SERVER_ERROR,
        TIMEOUT,
        NETWORK_ERROR,
    }
)

#: ``mimo_recognition_attempts.error_code`` values. The identifier validator
#: there allows ``[a-z0-9_.-]``, so the kind and status travel as dotted parts.
UNAVAILABLE_ERROR_CODE_PREFIX = "mimo_provider_unavailable."
REQUEST_REJECTED_ERROR_CODE_PREFIX = "mimo_request_rejected."
RESPONSE_INVALID_ERROR_CODE = "mimo_response_invalid"

UNAVAILABLE_INCIDENT_TYPE = "mimo_provider_unavailable"
RECOVERED_INCIDENT_TYPE = "mimo_provider_recovered"
HEALTH_CHECK_FAILED_INCIDENT_TYPE = "mimo_provider_health_check_failed"

REMINDER_INTERVAL = timedelta(minutes=30)

#: How many recent attempt rows one tick reads. At the 2026-09-12 rate (about
#: 34 failed calls an hour) this covers roughly six days of continuous outage;
#: beyond it the outage start is reported as the oldest row read, and the
#: summary says so.
DEFAULT_SCAN_LIMIT = 5000
#: Stop looking for an outage below this many consecutive answered attempts.
#: A recovery is noticed within one 20-second tick, so an outage buried under
#: hundreds of successes was either announced long ago or never was.
_MAX_ANSWERED_ROWS_ABOVE_OUTAGE = 200


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    failure_class: str
    kind: str | None = None
    http_status: int | None = None


def _kind_for_http_status(status: int) -> str | None:
    if status == 402:
        return INSUFFICIENT_BALANCE
    if status in {401, 403}:
        return AUTH_REJECTED
    if status == 429:
        return RATE_LIMITED
    if 500 <= status <= 599:
        return SERVER_ERROR
    return None


def _exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending and len(chain) < 8:
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        pending.extend([current.__cause__, current.__context__])
    return chain


def classify_provider_failure(error: BaseException) -> ProviderFailure | None:
    """Classify one failed provider request from the exception, not its text.

    ``_call_mimo_direct_model`` re-raises an ``HTTPStatusError`` as a
    ``RuntimeError`` carrying the response body, with the original as
    ``__cause__``; the chain is walked so that wrapping does not hide the
    status. ``None`` means "not a provider-level failure we can name" -- the
    caller keeps its generic code for those.
    """

    for link in _exception_chain(error):
        if isinstance(link, httpx.HTTPStatusError):
            status = int(link.response.status_code)
            kind = _kind_for_http_status(status)
            if kind is not None:
                return ProviderFailure(PROVIDER_UNAVAILABLE, kind, status)
            return ProviderFailure(REQUEST_REJECTED, None, status)
        if isinstance(link, (httpx.TimeoutException, TimeoutError)):
            return ProviderFailure(PROVIDER_UNAVAILABLE, TIMEOUT, None)
        if isinstance(link, httpx.TransportError):
            return ProviderFailure(PROVIDER_UNAVAILABLE, NETWORK_ERROR, None)
    return None


def unavailable_error_code(kind: str, http_status: int | None) -> str:
    code = f"{UNAVAILABLE_ERROR_CODE_PREFIX}{kind}"
    if http_status is not None:
        code = f"{code}.http_{int(http_status)}"
    return code


def parse_unavailable_error_code(code: Any) -> tuple[str, int | None] | None:
    text = str(code or "")
    if not text.startswith(UNAVAILABLE_ERROR_CODE_PREFIX):
        return None
    parts = text[len(UNAVAILABLE_ERROR_CODE_PREFIX):].split(".")
    kind = parts[0]
    if kind not in PROVIDER_UNAVAILABLE_KINDS:
        return None
    status = None
    if len(parts) > 1 and parts[1].startswith("http_"):
        try:
            status = int(parts[1][len("http_"):])
        except ValueError:
            status = None
    return kind, status


def v1_failure_error_code(attempts: Sequence[Any]) -> str | None:
    """The attempt-row error code for a failed v1 call, or ``None``.

    Unavailable only when **every** request that reached the provider was
    unavailable: one attempt that the provider answered proves it was up. The
    last request decides between "rejected" and "invalid". ``attempts`` are
    ``MimoProviderAttemptTelemetry``; read by attribute to avoid importing the
    recognition module that imports this one.
    """

    requested = [
        attempt
        for attempt in attempts
        if bool(getattr(attempt, "provider_request_made", False))
    ]
    if not requested:
        return None
    last = requested[-1]
    if all(
        getattr(attempt, "failure_class", None) == PROVIDER_UNAVAILABLE
        and getattr(attempt, "failure_kind", None) in PROVIDER_UNAVAILABLE_KINDS
        for attempt in requested
    ):
        return unavailable_error_code(
            str(last.failure_kind), getattr(last, "http_status", None)
        )
    last_class = getattr(last, "failure_class", None)
    last_status = getattr(last, "http_status", None)
    if last_class == REQUEST_REJECTED and last_status is not None:
        return f"{REQUEST_REJECTED_ERROR_CODE_PREFIX}http_{int(last_status)}"
    if last_class == RESPONSE_INVALID:
        return RESPONSE_INVALID_ERROR_CODE
    return None


# --------------------------------------------------------------------------
# Deriving the outage from the attempt audit
# --------------------------------------------------------------------------

_UNAVAILABLE = "unavailable"
_ANSWERED = "answered"
_NEUTRAL = "neutral"


def _row_signal(status: Any, error_code: Any) -> str:
    """What one attempt row says about the provider.

    ``neutral`` is the important third answer: an empty message, an unreadable
    image, a context-resolution failure never reached the provider, so it can
    neither extend an outage nor end one. Treating it as "answered" would
    announce a recovery in the middle of an outage.
    """

    if parse_unavailable_error_code(error_code) is not None:
        return _UNAVAILABLE
    code = str(error_code or "")
    if str(status or "") == "completed":
        return _ANSWERED
    if code.startswith(REQUEST_REJECTED_ERROR_CODE_PREFIX):
        return _ANSWERED
    if code == RESPONSE_INVALID_ERROR_CODE:
        return _ANSWERED
    return _NEUTRAL


@dataclass(frozen=True, slots=True)
class ProviderOutage:
    started_at: datetime
    last_failure_at: datetime
    kind: str
    http_status: int | None
    failures: int
    recovered_at: datetime | None
    scan_exhausted: bool

    @property
    def key(self) -> str:
        return f"outage_{int(self.started_at.timestamp())}"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def derive_provider_outage(
    rows_newest_first: Iterable[tuple[Any, Any, datetime]],
    *,
    scan_limit: int | None = None,
) -> ProviderOutage | None:
    """The most recent outage, open or just closed, from ``(status,
    error_code, completed_at)`` rows ordered newest first; ``None`` if none.

    Pure, so the same derivation runs against production rows offline. Answered
    rows at the top mean the provider is up; the earliest of them sitting
    directly above a run of unavailable rows is the recovery moment, and that
    run of unavailable rows is the outage.
    """

    rows_read = 0
    recovered_at: datetime | None = None
    answered_above = 0
    failures = 0
    started_at: datetime | None = None
    last_failure_at: datetime | None = None
    latest_kind: str | None = None
    latest_status: int | None = None
    ended_inside_scan = False
    for status, error_code, completed_at in rows_newest_first:
        rows_read += 1
        signal = _row_signal(status, error_code)
        if signal == _NEUTRAL:
            continue
        if failures == 0 and signal == _ANSWERED:
            recovered_at = _aware(completed_at)
            answered_above += 1
            if answered_above >= _MAX_ANSWERED_ROWS_ABOVE_OUTAGE:
                return None
            continue
        if signal == _ANSWERED:
            ended_inside_scan = True
            break
        parsed = parse_unavailable_error_code(error_code)
        assert parsed is not None
        if failures == 0:
            last_failure_at = _aware(completed_at)
            latest_kind, latest_status = parsed
        failures += 1
        started_at = _aware(completed_at)
    if failures == 0 or started_at is None or last_failure_at is None:
        return None
    return ProviderOutage(
        started_at=started_at,
        last_failure_at=last_failure_at,
        kind=str(latest_kind),
        http_status=latest_status,
        failures=failures,
        recovered_at=recovered_at,
        scan_exhausted=(
            not ended_inside_scan
            and scan_limit is not None
            and rows_read >= int(scan_limit)
        ),
    )


def _load_recent_attempt_rows(
    session_factory: sessionmaker,
    *,
    scan_limit: int,
) -> list[tuple[Any, Any, datetime]]:
    with session_factory() as session:
        return [
            (row.status, row.error_code, row.completed_at)
            for row in (
                session.query(
                    MimoRecognitionAttempt.status,
                    MimoRecognitionAttempt.error_code,
                    MimoRecognitionAttempt.completed_at,
                )
                .order_by(MimoRecognitionAttempt.id.desc())
                .limit(max(1, int(scan_limit)))
                .all()
            )
        ]


def load_latest_provider_outage(
    session_factory: sessionmaker,
    *,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
) -> ProviderOutage | None:
    rows = _load_recent_attempt_rows(session_factory, scan_limit=scan_limit)
    return derive_provider_outage(rows, scan_limit=scan_limit)


# --------------------------------------------------------------------------
# Telling a person
# --------------------------------------------------------------------------


def _incident_recorded(
    session_factory: sessionmaker,
    *,
    incident_type: str,
    source_record_id: str | None = None,
    source_record_prefix: str | None = None,
) -> bool:
    with session_factory() as session:
        query = session.query(RuntimeIncident.id).filter(
            RuntimeIncident.incident_type == incident_type,
            RuntimeIncident.source_kind == "mimo_provider",
        )
        if source_record_id is not None:
            query = query.filter(RuntimeIncident.source_record_id == source_record_id)
        if source_record_prefix is not None:
            query = query.filter(
                RuntimeIncident.source_record_id.startswith(source_record_prefix)
            )
        return query.first() is not None


def _default_capture(adapter_name: str) -> Callable[..., Any]:
    def capture(session_factory: sessionmaker, **kwargs: Any) -> Any:
        from telegram_kol_research import runtime_incident_adapters

        return runtime_incident_adapters.capture_runtime_incident_best_effort(
            getattr(runtime_incident_adapters, adapter_name),
            session_factory,
            **kwargs,
        )

    return capture


def run_mimo_provider_health_tick(
    session_factory: sessionmaker,
    *,
    now: datetime | None = None,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    capture_unavailable: Callable[..., Any] | None = None,
    capture_recovered: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """One evaluation: alert an open outage per 30-minute bucket, or its end.

    Returns what it decided, including ``rows_read`` -- the number of audit
    rows this tick actually examined. A healthy provider sends nothing, so that
    count is the only thing that distinguishes "checked and healthy" from "not
    checking at all"; the loop logs it.
    """

    current = _aware(now or datetime.now(UTC))
    rows = _load_recent_attempt_rows(session_factory, scan_limit=scan_limit)
    outage = derive_provider_outage(rows, scan_limit=scan_limit)
    rows_read = len(rows)
    if outage is None:
        return {"state": "healthy", "rows_read": rows_read}
    if outage.recovered_at is None:
        elapsed = max(timedelta(0), current - outage.started_at)
        bucket = int(elapsed // REMINDER_INTERVAL)
        source_record_id = f"{outage.key}_b{bucket}"
        if _incident_recorded(
            session_factory,
            incident_type=UNAVAILABLE_INCIDENT_TYPE,
            source_record_id=source_record_id,
        ):
            return {
                "state": "unavailable_already_alerted",
                "bucket": bucket,
                "rows_read": rows_read,
            }
        capture = capture_unavailable or _default_capture(
            "capture_mimo_provider_unavailable"
        )
        capture(
            session_factory,
            outage=outage,
            bucket=bucket,
            occurred_at=current,
        )
        logger.warning(
            "mimo provider unavailable alert raised kind=%s http_status=%s "
            "failures=%s started_at=%s bucket=%s scan_exhausted=%s",
            outage.kind,
            outage.http_status,
            outage.failures,
            outage.started_at.isoformat(),
            bucket,
            outage.scan_exhausted,
        )
        return {"state": "unavailable_alerted", "bucket": bucket, "rows_read": rows_read}
    if not _incident_recorded(
        session_factory,
        incident_type=UNAVAILABLE_INCIDENT_TYPE,
        source_record_prefix=f"{outage.key}_b",
    ):
        # An outage nobody was told about (it predates this code, or its
        # alert could not be recorded) gets no "recovered" message: a recovery
        # notice for an outage the reader never heard of is only confusing.
        return {"state": "recovered_outage_never_alerted", "rows_read": rows_read}
    if _incident_recorded(
        session_factory,
        incident_type=RECOVERED_INCIDENT_TYPE,
        source_record_id=outage.key,
    ):
        return {"state": "recovery_already_announced", "rows_read": rows_read}
    capture = capture_recovered or _default_capture("capture_mimo_provider_recovered")
    capture(session_factory, outage=outage, occurred_at=current)
    logger.warning(
        "mimo provider recovered notice raised kind=%s failures=%s "
        "started_at=%s recovered_at=%s",
        outage.kind,
        outage.failures,
        outage.started_at.isoformat(),
        outage.recovered_at.isoformat(),
    )
    return {"state": "recovery_announced", "rows_read": rows_read}

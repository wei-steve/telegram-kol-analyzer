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
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable, Sequence

import httpx
from sqlalchemy import or_
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


def _answered_during(
    completions: Sequence[datetime],
    started_at: datetime | None,
    completed_at: datetime,
) -> bool:
    """Did any of ``completions`` (sorted) fall while this request was running."""

    if started_at is None:
        return False
    from bisect import bisect_left

    index = bisect_left(completions, _aware(started_at))
    return (
        index < len(completions)
        and completions[index] <= _aware(completed_at)
    )


def derive_provider_outage(
    rows_newest_first: Iterable[tuple[Any, Any, datetime | None, datetime]],
    *,
    scan_limit: int | None = None,
) -> ProviderOutage | None:
    """The most recent outage, open or just closed, from ``(status,
    error_code, started_at, completed_at)`` rows ordered newest first;
    ``None`` if none.

    Pure, so the same derivation runs against production rows offline. Answered
    rows at the top mean the provider is up; the earliest of them sitting
    directly above a run of unavailable rows is the recovery moment, and that
    run of unavailable rows is the outage.

    **An isolated failure is not an outage** (ruling of 2026-09-13, rule A). On
    2026-09-13 00:02:34Z one request that had hung since 23:52:28Z failed with
    a network error while three other calls succeeded inside that same span;
    reading only the newest rows called that an outage and paged a person, then
    paged again four minutes later that it had recovered. So an unavailable row
    counts only if no answered attempt -- by any message, anywhere in the rows
    read, including rows *below* it in id order -- completed between its start
    and its end. An isolated one still carries its code and its log line; it
    just opens no outage.
    """

    rows = list(rows_newest_first)
    unavailable_completions = sorted(
        _aware(completed)
        for status, error_code, _started, completed in rows
        if _row_signal(status, error_code) == _UNAVAILABLE
    )
    # The mirror of rule A (found by the step-5 replay of 2026-09-12): an
    # answer to a request that was already in flight when unavailable failures
    # completed is not evidence the provider is up. Attempt 6749 started
    # 03:00:21 and answered 03:00:53 after two 402s completed inside its span;
    # read as a recovery it paged "unavailable", "recovered", "unavailable"
    # within forty seconds. Such stale answers are neutral, and they do not
    # make a failure isolated either.
    stale_answers: set[int] = set()
    fresh_answer_completions: list[datetime] = []
    for index, (status, error_code, request_started_at, completed_at) in enumerate(rows):
        if _row_signal(status, error_code) != _ANSWERED:
            continue
        if _answered_during(unavailable_completions, request_started_at, completed_at):
            stale_answers.add(index)
        else:
            fresh_answer_completions.append(_aware(completed_at))
    answered_completions = sorted(fresh_answer_completions)
    rows_read = 0
    recovered_at: datetime | None = None
    answered_above = 0
    failures = 0
    started_at: datetime | None = None
    last_failure_at: datetime | None = None
    latest_kind: str | None = None
    latest_status: int | None = None
    ended_inside_scan = False
    for index, (status, error_code, request_started_at, completed_at) in enumerate(rows):
        rows_read += 1
        signal = _row_signal(status, error_code)
        if signal == _ANSWERED and index in stale_answers:
            signal = _NEUTRAL
        elif signal == _UNAVAILABLE and _answered_during(
            answered_completions, request_started_at, completed_at
        ):
            signal = _NEUTRAL
        if signal == _NEUTRAL:
            continue
        completed = _aware(completed_at)
        if failures == 0 and signal == _ANSWERED:
            # Rows are ordered by id, and chat lanes finish in parallel, so
            # id order is not time order: take the earliest, never the last
            # row read.
            recovered_at = (
                completed if recovered_at is None else min(recovered_at, completed)
            )
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
            latest_kind, latest_status = parsed
        failures += 1
        # Same reason: the outage starts at its earliest failure and was last
        # seen at its latest, whatever order the ids put them in.
        last_failure_at = (
            completed if last_failure_at is None else max(last_failure_at, completed)
        )
        started_at = completed if started_at is None else min(started_at, completed)
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


#: Where the chain head is read from when no loader is supplied.
DEFAULT_AI_CONFIG_PATH = "config/ai_recognition.yaml"

#: ``(path, mtime_ns, size) -> head model``. Both health ticks run on every
#: iteration of the worker's 20-second gap-recovery loop, and each one needs
#: the same answer from the same file; parsing a 40 KB YAML twice per tick to
#: learn something that changes when a person saves a page is waste. The file
#: identity is the key, so a save is picked up on the next tick.
_CHAIN_HEAD_CACHE: dict[tuple[str, int, int], str | None] = {}


def _config_file_identity(path: Any) -> tuple[str, int, int] | None:
    try:
        stat = os.stat(os.fspath(path))
    except (OSError, TypeError, ValueError):
        return None
    return (str(path), int(stat.st_mtime_ns), int(stat.st_size))


def resolve_chain_head_model(
    config_loader: Callable[[], Any] | None = None,
    *,
    ai_recognition_config_path: Any = DEFAULT_AI_CONFIG_PATH,
) -> str | None:
    """The model name at the head of ``authoritative_recognition``.

    ``None`` means "could not tell" -- an unreadable configuration, or a stage
    with nothing usable bound. Every caller then reads **all** attempt rows,
    which is exactly what this module did before chains existed: a health
    check that fails open is one that keeps working, and a health check that
    quietly stops counting is the failure this module exists to end.
    """

    identity = (
        _config_file_identity(ai_recognition_config_path)
        if config_loader is None
        else None
    )
    if identity is not None and identity in _CHAIN_HEAD_CACHE:
        return _CHAIN_HEAD_CACHE[identity]
    try:
        from telegram_kol_research.ai_recognition_config import (
            load_ai_recognition_config,
        )
        from telegram_kol_research.recognition_experiments import (
            resolve_authoritative_chain,
        )

        config = (
            config_loader()
            if config_loader is not None
            else load_ai_recognition_config(ai_recognition_config_path)
        )
        chain = resolve_authoritative_chain(config)
    except Exception:
        logger.warning(
            "mimo provider health could not read the model chain; "
            "counting every attempt row",
            exc_info=True,
        )
        return None
    head = chain[0].model if chain else None
    if identity is not None:
        # Bounded: one entry per file version, and a worker reads one file.
        if len(_CHAIN_HEAD_CACHE) > 8:
            _CHAIN_HEAD_CACHE.clear()
        _CHAIN_HEAD_CACHE[identity] = head
    return head


def _chain_head_filter(query, chain_head_model: str | None):
    """Only the primary model's attempts say whether *it* is available.

    A fallback model answering proves nothing about the model that failed, so
    counting its rows would announce a recovery in the middle of an outage --
    the same mistake rule A already fixed for stale answers. Rows written
    before the column existed carry ``NULL`` and were the head by definition.
    """

    if not chain_head_model:
        return query
    return query.filter(
        or_(
            MimoRecognitionAttempt.model.is_(None),
            MimoRecognitionAttempt.model == chain_head_model,
        )
    )


def _load_recent_attempt_rows(
    session_factory: sessionmaker,
    *,
    scan_limit: int,
    chain_head_model: str | None = None,
) -> list[tuple[Any, Any, datetime, datetime]]:
    with session_factory() as session:
        query = _chain_head_filter(
            session.query(
                MimoRecognitionAttempt.status,
                MimoRecognitionAttempt.error_code,
                MimoRecognitionAttempt.started_at,
                MimoRecognitionAttempt.completed_at,
            ),
            chain_head_model,
        )
        return [
            (row.status, row.error_code, row.started_at, row.completed_at)
            for row in (
                query.order_by(MimoRecognitionAttempt.id.desc())
                .limit(max(1, int(scan_limit)))
                .all()
            )
        ]


def load_latest_provider_outage(
    session_factory: sessionmaker,
    *,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    chain_head_model: str | None = None,
) -> ProviderOutage | None:
    rows = _load_recent_attempt_rows(
        session_factory,
        scan_limit=scan_limit,
        chain_head_model=chain_head_model,
    )
    return derive_provider_outage(rows, scan_limit=scan_limit)


def _fallback_model_answering_since(
    session_factory: sessionmaker,
    *,
    since: datetime,
    chain_head_model: str | None,
) -> str | None:
    """Which non-head model has answered since the outage started, if any.

    The alert says so, because "recognition has stopped" and "the primary
    model has stopped and a backup is carrying it" need different urgency
    from the person reading it.
    """

    if not chain_head_model:
        return None
    cutoff = _aware(since).replace(tzinfo=None)
    with session_factory() as session:
        row = (
            session.query(MimoRecognitionAttempt.model)
            .filter(MimoRecognitionAttempt.completed_at >= cutoff)
            .filter(MimoRecognitionAttempt.status == "completed")
            .filter(MimoRecognitionAttempt.model.isnot(None))
            .filter(MimoRecognitionAttempt.model != chain_head_model)
            .order_by(MimoRecognitionAttempt.id.desc())
            .first()
        )
    return str(row[0]) if row is not None and row[0] else None


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
    config_loader: Callable[[], Any] | None = None,
    chain_head_model: str | None = None,
) -> dict[str, Any]:
    """One evaluation: alert an open outage per 30-minute bucket, or its end.

    Only the chain head's attempts are counted: a fallback model answering
    says nothing about whether the primary one is back, and the primary is
    retried from the head of the chain on every new message anyway, so a real
    recovery is still noticed within one tick.

    Returns what it decided, including ``rows_read`` -- the number of audit
    rows this tick actually examined. A healthy provider sends nothing, so that
    count is the only thing that distinguishes "checked and healthy" from "not
    checking at all"; the loop logs it.
    """

    current = _aware(now or datetime.now(UTC))
    head = chain_head_model or resolve_chain_head_model(config_loader)
    rows = _load_recent_attempt_rows(
        session_factory, scan_limit=scan_limit, chain_head_model=head
    )
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
        fallback_model = _fallback_model_answering_since(
            session_factory,
            since=outage.started_at,
            chain_head_model=head,
        )
        capture(
            session_factory,
            outage=outage,
            bucket=bucket,
            occurred_at=current,
            fallback_model=fallback_model,
        )
        logger.warning(
            "mimo provider unavailable alert raised kind=%s http_status=%s "
            "failures=%s started_at=%s bucket=%s scan_exhausted=%s "
            "fallback_model=%s",
            outage.kind,
            outage.http_status,
            outage.failures,
            outage.started_at.isoformat(),
            bucket,
            outage.scan_exhausted,
            fallback_model,
        )
        return {
            "state": "unavailable_alerted",
            "bucket": bucket,
            "rows_read": rows_read,
            "fallback_model": fallback_model,
        }
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


HEALTH_TICK_TASK = "mimo_provider_health_tick"
FAILURE_STREAK_TICK_TASK = "mimo_provider_failure_streak_tick"
PROBE_TICK_TASK = "mimo_provider_probe_tick"


def record_health_check_failure(
    session_factory: sessionmaker,
    *,
    consecutive_failures: int,
    error_type: str,
    task_name: str = HEALTH_TICK_TASK,
) -> None:
    """Raise ``mimo_provider_health_check_failed``; meant to run in a thread.

    The occurrence time is read here rather than by the caller, because the
    caller is the event loop and a clock read there is exactly what the
    event-loop blocking census exists to refuse. ``task_name`` says which of
    the provider checks keeps failing (step 4 added two).
    """

    _default_capture("capture_mimo_provider_health_check_failed")(
        session_factory,
        consecutive_failures=int(consecutive_failures),
        error_type=str(error_type),
        task_name=str(task_name),
        occurred_at=datetime.now(UTC),
    )


# --------------------------------------------------------------------------
# Step 4: the same error, again and again
# --------------------------------------------------------------------------

STREAK_INCIDENT_TYPE = "mimo_provider_failure_streak"
#: Consecutive failures with one error code that make a streak.
STREAK_THRESHOLD = 5
#: A streak whose last failure is older than this is history, not news: the
#: first tick after a deploy must not page about last week.
STREAK_FRESHNESS = timedelta(minutes=30)
#: Recent attempt rows one streak tick reads. A streak longer than this within
#: the freshness window keeps being re-keyed by its oldest row still read, so
#: it is re-alerted roughly once per this many failures.
STREAK_SCAN_LIMIT = 200
#: A failure code none of the classes above names (the legacy
#: ``v1_authoritative_failed`` fallback).
UNCLASSIFIED = "unclassified"


def describe_failure_code(code: Any) -> ProviderFailure:
    """Read an attempt-row error code back into its class, kind and status."""

    parsed = parse_unavailable_error_code(code)
    if parsed is not None:
        return ProviderFailure(PROVIDER_UNAVAILABLE, parsed[0], parsed[1])
    text = str(code or "")
    if text.startswith(REQUEST_REJECTED_ERROR_CODE_PREFIX):
        suffix = text[len(REQUEST_REJECTED_ERROR_CODE_PREFIX):]
        status = None
        if suffix.startswith("http_"):
            try:
                status = int(suffix[len("http_"):])
            except ValueError:
                status = None
        return ProviderFailure(REQUEST_REJECTED, None, status)
    if text == RESPONSE_INVALID_ERROR_CODE:
        return ProviderFailure(RESPONSE_INVALID)
    return ProviderFailure(UNCLASSIFIED)


@dataclass(frozen=True, slots=True)
class FailureStreak:
    error_code: str
    first_attempt_id: int
    failures: int
    started_at: datetime
    last_failure_at: datetime

    @property
    def key(self) -> str:
        return f"streak_{int(self.first_attempt_id)}"


def derive_failure_streaks(
    rows: Iterable[tuple[Any, Any, Any, Any, datetime]],
    *,
    threshold: int = STREAK_THRESHOLD,
) -> list[FailureStreak]:
    """Runs of one error code, in completion order, at least ``threshold``
    long, from ``(attempt_id, status, error_code, provider_request_count,
    completed_at)`` rows in any order.

    Pure, like :func:`derive_provider_outage`. What breaks a run: a completed
    attempt, or an attempt that reached the provider with a different code.
    What neither counts nor breaks: an attempt that never reached the provider
    (``provider_request_count == 0`` -- an empty message, an unreadable image).
    ``NULL`` counts as reached: rows written before the column existed carry
    no count, and a streak reported once too often beats one never reported.

    Completion order, not id order: chat lanes finish in parallel, and the
    outage derivation already learned that ids lie about time.
    """

    ordered = sorted(rows, key=lambda row: (_aware(row[4]), int(row[0])))
    streaks: list[FailureStreak] = []
    code: str | None = None
    first_id = 0
    count = 0
    first_at: datetime | None = None
    last_at: datetime | None = None
    for attempt_id, status, error_code, request_count, completed_at in ordered:
        answered = str(status or "") == "completed"
        if not answered and request_count is not None and int(request_count) <= 0:
            continue
        completed = _aware(completed_at)
        row_code = None if answered else (str(error_code or "") or None)
        if row_code is not None and row_code == code:
            count += 1
            last_at = completed
            continue
        if code is not None and count >= threshold and first_at and last_at:
            streaks.append(FailureStreak(code, first_id, count, first_at, last_at))
        code = row_code
        first_id = int(attempt_id)
        count = 1 if row_code is not None else 0
        first_at = last_at = completed
    if code is not None and count >= threshold and first_at and last_at:
        streaks.append(FailureStreak(code, first_id, count, first_at, last_at))
    return streaks


def _load_streak_rows(
    session_factory: sessionmaker,
    *,
    scan_limit: int,
    chain_head_model: str | None = None,
) -> list[tuple[Any, Any, Any, Any, datetime]]:
    with session_factory() as session:
        query = _chain_head_filter(
            session.query(
                MimoRecognitionAttempt.id,
                MimoRecognitionAttempt.status,
                MimoRecognitionAttempt.error_code,
                MimoRecognitionAttempt.provider_request_count,
                MimoRecognitionAttempt.completed_at,
            ).filter(MimoRecognitionAttempt.completed_at.isnot(None)),
            chain_head_model,
        )
        return [
            (
                row.id,
                row.status,
                row.error_code,
                row.provider_request_count,
                row.completed_at,
            )
            for row in (
                query.order_by(MimoRecognitionAttempt.id.desc())
                .limit(max(1, int(scan_limit)))
                .all()
            )
        ]


def _outage_alert_covers(
    session_factory: sessionmaker,
    outage: ProviderOutage | None,
    streak: FailureStreak,
) -> bool:
    """Has a person already been told about the outage this streak is part of."""

    if outage is None:
        return False
    if outage.started_at > streak.last_failure_at:
        return False
    if outage.recovered_at is not None and outage.recovered_at < streak.started_at:
        return False
    return _incident_recorded(
        session_factory,
        incident_type=UNAVAILABLE_INCIDENT_TYPE,
        source_record_prefix=f"{outage.key}_b",
    )


def run_mimo_failure_streak_tick(
    session_factory: sessionmaker,
    *,
    now: datetime | None = None,
    scan_limit: int = STREAK_SCAN_LIMIT,
    capture: Callable[..., Any] | None = None,
    config_loader: Callable[[], Any] | None = None,
    chain_head_model: str | None = None,
) -> dict[str, Any]:
    """Alert each fresh streak once.

    What step 1 cannot see: a request the provider rejects (a 400 is the
    provider answering, so it is no outage), an unclassified failure, and
    unavailable failures that rule A calls isolated -- five hung requests in a
    row while quick ones succeed around them. An unavailable streak inside an
    outage step 1 already announced is not alerted again.
    """

    current = _aware(now or datetime.now(UTC))
    head = chain_head_model or resolve_chain_head_model(config_loader)
    rows = _load_streak_rows(
        session_factory, scan_limit=scan_limit, chain_head_model=head
    )
    fresh = [
        streak
        for streak in derive_failure_streaks(rows)
        if streak.last_failure_at >= current - STREAK_FRESHNESS
    ]
    if not fresh:
        return {"state": "no_streak", "rows_read": len(rows)}
    alerted = covered = already = 0
    outage_loaded = False
    outage: ProviderOutage | None = None
    for streak in fresh:
        if _incident_recorded(
            session_factory,
            incident_type=STREAK_INCIDENT_TYPE,
            source_record_id=streak.key,
        ):
            already += 1
            continue
        failure = describe_failure_code(streak.error_code)
        if failure.failure_class == PROVIDER_UNAVAILABLE:
            if not outage_loaded:
                outage = load_latest_provider_outage(
                    session_factory, chain_head_model=head
                )
                outage_loaded = True
            if _outage_alert_covers(session_factory, outage, streak):
                covered += 1
                continue
        (capture or _default_capture("capture_mimo_provider_failure_streak"))(
            session_factory,
            streak=streak,
            failure=failure,
            occurred_at=current,
        )
        logger.warning(
            "mimo provider failure streak alert raised error_code=%s failures=%s "
            "started_at=%s last_failure_at=%s",
            streak.error_code,
            streak.failures,
            streak.started_at.isoformat(),
            streak.last_failure_at.isoformat(),
        )
        alerted += 1
    if alerted:
        state = "streak_alerted"
    elif covered:
        state = "covered_by_outage_alert"
    else:
        state = "streak_already_alerted"
    return {
        "state": state,
        "rows_read": len(rows),
        "alerted": alerted,
        "covered": covered,
        "already_alerted": already,
    }

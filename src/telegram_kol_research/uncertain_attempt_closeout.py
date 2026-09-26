"""Close out frozen ``uncertain`` authoritative execution attempts.

Design: ``docs/plans/2026-09-26-uncertain-attempt-closeout-design.md``.

An authoritative execution that crossed the side-effect boundary and then lost
track of its own outcome ends in ``uncertain``. That is the correct fail-closed
terminal for the *execution*, but until now it was not a terminal for the *row*:
``recognition_execution_scanner`` scans exactly the non-terminal statuses, wraps
its cursor to zero when a family runs dry, and therefore re-reported the same 37
September rows roughly every two minutes -- about 27000 ``ERROR`` lines a day,
every one of them with action ``observe_uncertain``, which observes and does
nothing.

This module is the missing path: it reads what those rows actually did to the
exchange, and gives the ones that are settled a terminal status.

**It closes the attempt row and nothing else.** In particular it never touches
``recognition_decisions``. ``comparison_status='execution_uncertain'`` is what
makes any new recognition write for that message raise
:class:`~telegram_kol_research.recognition_decisions.AuthoritativeExecutionInProgress`
-- the message is frozen and cannot be re-recognised. Clearing the decision row
as part of "tidying up" would unfreeze 37 September messages, 7 of them entry
signals, and any re-recognition after that could place an order at September
prices. The user's instruction on 2026-09-26 was that too much time has passed
for that to be allowed, and this module keeps that guarantee structural rather
than careful: the decision row is never in any statement it issues, and
``tests/test_uncertain_attempt_closeout.py`` pins that a closed-out message is
still refused a new decision.

**Bucketing reuses the ledger's own rule**, not a new one. An execution event
counts as having written to the exchange when it carries an exchange identity
(:func:`execution_event_has_exchange_identity`) *or* its action is not in
:data:`NON_EXCHANGE_WRITING_EXECUTION_ACTIONS` -- the same disjunction
``source_message_deletion_worker`` uses, so an action nobody has classified yet
stays hazardous. Unlike that caller, this one excludes no actions from the test
at all: a deletion-outcome or cleanup-outcome row here pushes the attempt into
the bucket that demands a settled binding, which is the fail-closed direction.

Two terminals, not one, so no row's status lies about it:

* :data:`CLOSED_NO_WRITE` -- nothing under this message ever reached the venue.
* :data:`CLOSED_SETTLED_BINDING` -- something did, and every binding it can be
  traced to is already in a terminal state. A live binding is a refusal, never
  a closeout.

``exchange_effect`` stays ``outcome_unknown`` on both: what that attempt's
request did is still unknown and always will be. What this module establishes is
that the *exposure* is settled, which is a different claim and belongs in
``error_summary`` and in the status, not in a field about the venue's answer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from sqlalchemy import or_, update

from telegram_kol_research.authoritative_execution_attempts import (
    CLOSED_NO_WRITE,
    CLOSED_SETTLED_BINDING,
    CLOSEOUT_STATUSES,
)
from telegram_kol_research.authoritative_execution_schema import (
    require_recognition_execution_schema,
)
from telegram_kol_research.execution_events import (
    NON_EXCHANGE_WRITING_EXECUTION_ACTIONS,
    execution_event_has_exchange_identity,
)
from telegram_kol_research.models import (
    AuthoritativeExecutionAttempt,
    ExecutionBinding,
    ExecutionEvent,
    RawMessage,
    StrategyAlert,
)


#: The frozen status this tool reads. Nothing else is eligible.
UNCERTAIN_STATUS = "uncertain"

# :data:`CLOSED_NO_WRITE`, :data:`CLOSED_SETTLED_BINDING` and
# :data:`CLOSEOUT_STATUSES` are imported above rather than declared here:
# ``authoritative_execution_attempts`` declares every status this table can hold,
# so an online consumer never has to import this operator tool. This module is
# the only writer of either terminal.

#: Bucket labels in the operator's listing. ``NO_EXECUTION_EVENT`` and
#: ``NON_WRITING_EVENTS_ONLY`` both close as :data:`CLOSED_NO_WRITE`; they are
#: kept apart because "we recorded nothing" and "we recorded only notifications"
#: are different stories about the same conclusion.
BUCKET_NO_EXECUTION_EVENT = "no_execution_event"
BUCKET_NON_WRITING_EVENTS_ONLY = "non_writing_events_only"
BUCKET_EXCHANGE_WRITE = "exchange_write"

#: Why a row was refused instead of closed.
REFUSAL_BINDING_NOT_TERMINAL = "binding_not_terminal"
REFUSAL_NO_BINDING_TO_VERIFY = "no_binding_to_verify"

#: Binding statuses that mean the exposure is over. Byte-identical to
#: ``position_take_profit_orders._TERMINAL_BINDING_STATES`` and
#: ``historical_state_repair._TERMINAL_BINDING_STATES``; a test asserts all
#: three stay equal rather than importing a private name across modules.
TERMINAL_BINDING_STATES = frozenset(
    {"closed", "cancelled", "completed", "failed", "resolved", "superseded"}
)


class UncertainAttemptCloseoutRefused(RuntimeError):
    """Raised when the apply guard no longer matches what the dry run saw."""


def _bounded(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class UncertainAttemptCloseoutRow:
    """One ``uncertain`` attempt, its bucket, and the verdict on it."""

    attempt_id: int
    raw_message_id: int
    chat_id: int | None
    chat_title: str | None
    sender_name: str | None
    posted_at: str | None
    uncertain_at: str | None
    bucket: str
    event_types: tuple[str, ...]
    writing_event_types: tuple[str, ...]
    binding_ids: tuple[int, ...]
    live_binding_ids: tuple[int, ...]
    closeout_status: str | None
    refusal_reason: str | None

    @property
    def closeable(self) -> bool:
        return self.closeout_status is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "raw_message_id": self.raw_message_id,
            "chat_id": self.chat_id,
            "chat_title": self.chat_title,
            "sender_name": self.sender_name,
            "posted_at": self.posted_at,
            "uncertain_at": self.uncertain_at,
            "bucket": self.bucket,
            "event_types": list(self.event_types),
            "writing_event_types": list(self.writing_event_types),
            "binding_ids": list(self.binding_ids),
            "live_binding_ids": list(self.live_binding_ids),
            "closeout_status": self.closeout_status,
            "refusal_reason": self.refusal_reason,
        }


@dataclass(frozen=True, slots=True)
class UncertainAttemptCloseoutPlan:
    """Every ``uncertain`` attempt in the database, judged, nothing written."""

    rows: tuple[UncertainAttemptCloseoutRow, ...]
    manifest_sha256: str

    @property
    def closeable_rows(self) -> tuple[UncertainAttemptCloseoutRow, ...]:
        return tuple(row for row in self.rows if row.closeable)

    @property
    def refused_rows(self) -> tuple[UncertainAttemptCloseoutRow, ...]:
        return tuple(row for row in self.rows if not row.closeable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_count": len(self.rows),
            "closeable_count": len(self.closeable_rows),
            "refused_count": len(self.refused_rows),
            "manifest_sha256": self.manifest_sha256,
            "rows": [row.to_dict() for row in self.rows],
            # This command reads the exchange never and writes it never. Same
            # field, same value, as ``worker-command-reconcile`` and
            # ``expire-message-processing-backlog``.
            "exchange_write_count": 0,
        }


@dataclass(frozen=True, slots=True)
class UncertainAttemptCloseoutResult:
    plan: UncertainAttemptCloseoutPlan
    changed_count: int
    transaction_lock_seconds: float
    lock_acquisition_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.plan.to_dict(),
            "changed_count": self.changed_count,
            "transaction_lock_seconds": self.transaction_lock_seconds,
            "lock_acquisition_seconds": self.lock_acquisition_seconds,
        }


def _manifest_sha256(rows: tuple[UncertainAttemptCloseoutRow, ...]) -> str:
    canonical = json.dumps(
        [
            {
                "attempt_id": row.attempt_id,
                "raw_message_id": row.raw_message_id,
                "bucket": row.bucket,
                "closeout_status": row.closeout_status,
                "refusal_reason": row.refusal_reason,
            }
            for row in rows
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _judge_row(session, attempt) -> UncertainAttemptCloseoutRow:
    raw = session.get(RawMessage, int(attempt.raw_message_id))
    chat_id = int(raw.chat_id) if raw is not None else None
    message_id = int(raw.message_id) if raw is not None else None
    alert_title = None
    if chat_id is not None and message_id is not None:
        alert_title = (
            session.query(StrategyAlert.chat_title)
            .filter(
                StrategyAlert.chat_id == chat_id,
                StrategyAlert.message_id == message_id,
            )
            .limit(1)
            .scalar()
        )

    event_types: tuple[str, ...] = ()
    writing_event_types: tuple[str, ...] = ()
    binding_ids: set[int] = set()
    if chat_id is not None and message_id is not None:
        events = (
            session.query(
                ExecutionEvent.id,
                ExecutionEvent.action,
                ExecutionEvent.execution_binding_id,
            )
            .filter(
                ExecutionEvent.chat_id == chat_id,
                ExecutionEvent.message_id == message_id,
            )
            .order_by(ExecutionEvent.id.asc())
            .all()
        )
        event_types = tuple(sorted({str(row.action) for row in events}))
        writing_ids = {
            int(value)
            for (value,) in session.query(ExecutionEvent.id)
            .filter(
                ExecutionEvent.chat_id == chat_id,
                ExecutionEvent.message_id == message_id,
                or_(
                    execution_event_has_exchange_identity(),
                    ExecutionEvent.action.not_in(
                        NON_EXCHANGE_WRITING_EXECUTION_ACTIONS
                    ),
                ),
            )
            .all()
        }
        writing_event_types = tuple(
            sorted(
                {str(row.action) for row in events if int(row.id) in writing_ids}
            )
        )
        binding_ids = {
            int(row.execution_binding_id)
            for row in events
            if int(row.id) in writing_ids
            and row.execution_binding_id is not None
        }
        # Bindings are also looked up by the message itself: an event that
        # wrote without recording its binding id would otherwise leave nothing
        # to verify, and "found no binding" must not be the easy path to a
        # closeout.
        binding_ids.update(
            int(value)
            for (value,) in session.query(ExecutionBinding.id)
            .filter(
                ExecutionBinding.chat_id == chat_id,
                ExecutionBinding.message_id == message_id,
            )
            .all()
        )
    else:
        writing_ids = set()

    if not event_types:
        bucket = BUCKET_NO_EXECUTION_EVENT
    elif not writing_ids:
        bucket = BUCKET_NON_WRITING_EVENTS_ONLY
    else:
        bucket = BUCKET_EXCHANGE_WRITE

    ordered_binding_ids = tuple(sorted(binding_ids))
    # Reported for every bucket, gating only the write bucket. The design's rule
    # for the no-write buckets is the absence of a writing event, and a binding
    # under a message with no writing event at all is a ledger artefact rather
    # than exposure this attempt created -- live positions are watched by the
    # lifecycle/deletion-exit machinery, none of which reads attempt status. So
    # this is printed for the operator to see rather than turned into a refusal
    # the design did not ask for.
    live_binding_ids = tuple(
        int(row.id)
        for row in session.query(ExecutionBinding.id, ExecutionBinding.status)
        .filter(ExecutionBinding.id.in_(ordered_binding_ids or (0,)))
        .order_by(ExecutionBinding.id.asc())
        .all()
        if str(row.status or "").lower() not in TERMINAL_BINDING_STATES
    )
    closeout_status: str | None = None
    refusal_reason: str | None = None
    if bucket in {BUCKET_NO_EXECUTION_EVENT, BUCKET_NON_WRITING_EVENTS_ONLY}:
        closeout_status = CLOSED_NO_WRITE
    else:
        if not ordered_binding_ids:
            refusal_reason = REFUSAL_NO_BINDING_TO_VERIFY
        elif live_binding_ids:
            refusal_reason = REFUSAL_BINDING_NOT_TERMINAL
        else:
            closeout_status = CLOSED_SETTLED_BINDING

    return UncertainAttemptCloseoutRow(
        attempt_id=int(attempt.id),
        raw_message_id=int(attempt.raw_message_id),
        chat_id=chat_id,
        chat_title=_bounded(alert_title, 255),
        sender_name=_bounded(raw.sender_name, 255) if raw is not None else None,
        posted_at=(
            raw.posted_at.isoformat()
            if raw is not None and raw.posted_at is not None
            else None
        ),
        uncertain_at=(
            attempt.uncertain_at.isoformat()
            if attempt.uncertain_at is not None
            else None
        ),
        bucket=bucket,
        event_types=event_types,
        writing_event_types=writing_event_types,
        binding_ids=tuple(sorted(binding_ids)),
        live_binding_ids=live_binding_ids,
        closeout_status=closeout_status,
        refusal_reason=refusal_reason,
    )


def _build_plan_in_session(session) -> UncertainAttemptCloseoutPlan:
    attempts = (
        session.query(AuthoritativeExecutionAttempt)
        .filter(AuthoritativeExecutionAttempt.status == UNCERTAIN_STATUS)
        .order_by(AuthoritativeExecutionAttempt.id.asc())
        .all()
    )
    rows = tuple(_judge_row(session, attempt) for attempt in attempts)
    return UncertainAttemptCloseoutPlan(
        rows=rows, manifest_sha256=_manifest_sha256(rows)
    )


def build_uncertain_attempt_closeout_plan(
    session_factory,
) -> UncertainAttemptCloseoutPlan:
    """Judge every ``uncertain`` attempt without mutating anything."""

    require_recognition_execution_schema(session_factory)
    with session_factory() as session:
        return _build_plan_in_session(session)


def closeout_error_summary(
    existing: str | None, *, closeout_status: str, closed_at: datetime
) -> str:
    """Append the closeout reason and its date to an existing summary."""

    stamp = _naive_utc(closed_at).date().isoformat()
    suffix = f"closed_out={closeout_status}@{stamp}"
    text = str(existing or "").strip()
    combined = f"{text} {suffix}".strip()
    return combined[:512]


def apply_uncertain_attempt_closeout(
    session_factory,
    *,
    expected_count: int,
    closed_at: datetime,
) -> UncertainAttemptCloseoutResult:
    """Close exactly ``expected_count`` settled attempts in one transaction.

    The plan is rebuilt inside ``BEGIN IMMEDIATE`` and its closeable count must
    still equal ``expected_count``, so a database that changed between the
    operator's dry run and this call is refused instead of half-applied. Every
    update is a CAS on ``(id, status='uncertain')``.
    """

    require_recognition_execution_schema(session_factory)
    acquisition_started = perf_counter()
    with session_factory() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        lock_acquired_at = perf_counter()
        try:
            plan = _build_plan_in_session(session)
            if len(plan.closeable_rows) != int(expected_count):
                raise UncertainAttemptCloseoutRefused(
                    "expected_count_mismatch:"
                    f"expected={int(expected_count)}:"
                    f"observed={len(plan.closeable_rows)}"
                )
            normalized = _naive_utc(closed_at)
            changed = 0
            for row in plan.closeable_rows:
                attempt = session.get(
                    AuthoritativeExecutionAttempt, row.attempt_id
                )
                if attempt is None or str(attempt.status) != UNCERTAIN_STATUS:
                    raise UncertainAttemptCloseoutRefused(
                        f"attempt_no_longer_uncertain:{row.attempt_id}"
                    )
                result = session.execute(
                    update(AuthoritativeExecutionAttempt)
                    .where(
                        AuthoritativeExecutionAttempt.id == row.attempt_id,
                        AuthoritativeExecutionAttempt.status
                        == UNCERTAIN_STATUS,
                    )
                    .values(
                        status=row.closeout_status,
                        error_summary=closeout_error_summary(
                            attempt.error_summary,
                            closeout_status=str(row.closeout_status),
                            closed_at=normalized,
                        ),
                        updated_at=normalized,
                    )
                )
                if int(result.rowcount or 0) != 1:
                    raise UncertainAttemptCloseoutRefused(
                        f"closeout_cas_failed:{row.attempt_id}"
                    )
                changed += 1
            if changed != int(expected_count):
                raise UncertainAttemptCloseoutRefused("changed_count_mismatch")
            session.commit()
        except Exception:
            session.rollback()
            raise
        committed_at = perf_counter()
    return UncertainAttemptCloseoutResult(
        plan=plan,
        changed_count=changed,
        transaction_lock_seconds=committed_at - lock_acquired_at,
        lock_acquisition_seconds=lock_acquired_at - acquisition_started,
    )

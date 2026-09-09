"""Which strategy threads may be a management instruction's target.

A-7 tasks 1 and 2. Two production messages are the reason this exists:

* 峰哥 raw 15155 — context resolution answered ``unresolved / target_ambiguous``
  with two candidates, one of which was lifecycle 1081: an entry that had
  *failed*, which ``lifecycle_monitor`` had nonetheless simulated into
  ``entered``. A ghost. The instruction went nowhere and nobody was told.
* 大镖客 raw 15201 — resolved to lifecycle 1074, whose position had closed on
  2026-09-04, while the real holding was lifecycle 1097. The recogniser's own
  note said it had linked them "by price description and strategy activity",
  not by a reply.

Both are the same failure: a candidate set that contains lifecycles with no
verifiable position behind them. The fix is not smarter matching -- it is a
smaller candidate set. A lifecycle may be a management target only when it
carries an execution binding *and* that binding's position is in a recent,
complete exchange snapshot.

**The snapshot is read, never taken.** ``position_reconciliation_observations``
is what the reconcile loop already writes every round, so this adds no exchange
call and no new failure mode. It also carries the one thing a freshness rule
needs: when the observation was made.

**Staleness is not emptiness.** With no complete observation inside the window,
this returns ``None`` -- "unknown" -- and the caller must notify rather than
judge. Returning an empty set instead would read as "no position exists
anywhere", which would disqualify every candidate and silently turn a stale
snapshot into a confident refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from telegram_kol_research.models import (
    ExecutionBinding,
    MessageInstructionItem,
    PositionReconciliationObservation,
    RawMessage,
    StrategyLifecycle,
)


#: How old a complete positions snapshot may be and still settle the question.
#: Beyond this the answer is "unknown", which routes to a confirmation request.
DEFAULT_SNAPSHOT_MAX_AGE = timedelta(minutes=5)

VERIFIED = "verified"
NO_BINDING = "no_execution_binding"
POSITION_ABSENT = "position_absent_from_snapshot"
SNAPSHOT_STALE = "snapshot_stale"


@dataclass(frozen=True, slots=True)
class LifecycleTargetVerdict:
    lifecycle_id: int
    verified: bool
    reason: str
    execution_binding_id: int | None = None
    pos_ids: tuple[str, ...] = ()


def load_verified_position_ids(
    session,
    *,
    now: datetime,
    venue: str = "deepcoin",
    max_age: timedelta = DEFAULT_SNAPSHOT_MAX_AGE,
) -> frozenset[str] | None:
    """Position ids the exchange showed open recently, or ``None`` if stale.

    ``None`` and ``frozenset()`` mean different things and the caller must
    treat them differently: the first is "we do not know", the second is "we
    looked and there are no open positions".
    """

    cutoff = _naive(now) - max_age
    rows = (
        session.query(PositionReconciliationObservation)
        .filter(
            PositionReconciliationObservation.venue == venue,
            PositionReconciliationObservation.snapshot_complete.is_(True),
            PositionReconciliationObservation.observed_at >= cutoff,
        )
        .order_by(PositionReconciliationObservation.observed_at.desc())
        .all()
    )
    if not rows:
        return None
    open_ids: set[str] = set()
    seen: set[str] = set()
    for row in rows:
        pos_id = str(row.pos_id or "").strip()
        if not pos_id or pos_id in seen:
            # Newest observation per position wins; older rows for the same
            # position describe a state that has already been superseded.
            continue
        seen.add(pos_id)
        size = _decimal(row.size_text)
        if size is not None and size > 0:
            open_ids.add(pos_id)
    return frozenset(open_ids)


def verify_lifecycle_targets(
    session,
    lifecycle_ids: Iterable[int],
    *,
    verified_position_ids: frozenset[str] | None,
) -> dict[int, LifecycleTargetVerdict]:
    """Judge each lifecycle against the snapshot, one verdict per lifecycle."""

    ids = [int(value) for value in lifecycle_ids]
    if not ids:
        return {}
    verdicts: dict[int, LifecycleTargetVerdict] = {}
    lifecycles = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.id.in_(ids))
        .all()
    )
    binding_ids = [
        int(row.execution_binding_id)
        for row in lifecycles
        if row.execution_binding_id is not None
    ]
    pos_ids_by_binding: dict[int, tuple[str, ...]] = {}
    if binding_ids:
        for binding in (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.id.in_(binding_ids))
            .all()
        ):
            pos_ids_by_binding[int(binding.id)] = tuple(
                item.strip()
                for item in str(binding.pos_id or "").split(",")
                if item.strip()
            )
    for lifecycle in lifecycles:
        lifecycle_id = int(lifecycle.id)
        binding_id = (
            int(lifecycle.execution_binding_id)
            if lifecycle.execution_binding_id is not None
            else None
        )
        if binding_id is None:
            # The ghost case: entered on paper, never bound to an order.
            verdicts[lifecycle_id] = LifecycleTargetVerdict(
                lifecycle_id=lifecycle_id, verified=False, reason=NO_BINDING
            )
            continue
        pos_ids = pos_ids_by_binding.get(binding_id, ())
        if verified_position_ids is None:
            verdicts[lifecycle_id] = LifecycleTargetVerdict(
                lifecycle_id=lifecycle_id,
                verified=False,
                reason=SNAPSHOT_STALE,
                execution_binding_id=binding_id,
                pos_ids=pos_ids,
            )
            continue
        if pos_ids and set(pos_ids) & verified_position_ids:
            verdicts[lifecycle_id] = LifecycleTargetVerdict(
                lifecycle_id=lifecycle_id,
                verified=True,
                reason=VERIFIED,
                execution_binding_id=binding_id,
                pos_ids=pos_ids,
            )
            continue
        verdicts[lifecycle_id] = LifecycleTargetVerdict(
            lifecycle_id=lifecycle_id,
            verified=False,
            reason=POSITION_ABSENT,
            execution_binding_id=binding_id,
            pos_ids=pos_ids,
        )
    for lifecycle_id in ids:
        verdicts.setdefault(
            lifecycle_id,
            LifecycleTargetVerdict(
                lifecycle_id=lifecycle_id, verified=False, reason=NO_BINDING
            ),
        )
    return verdicts


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)

#: The instruction item state that says "a person has to choose". It is
#: deliberately not ``failed``: nothing went wrong and nothing was refused --
#: the instruction is real and still actionable, it just cannot be pointed at a
#: position without someone saying which one.
AWAITING_CONFIRMATION = "awaiting_user_confirmation"

AMBIGUOUS = "target_ambiguous"
NO_VERIFIABLE_TARGET = "no_verifiable_target"


def confirmation_reason_code(candidate_count: int, snapshot_stale: bool) -> str:
    if snapshot_stale:
        return SNAPSHOT_STALE
    return AMBIGUOUS if candidate_count > 1 else NO_VERIFIABLE_TARGET


def describe_candidates(candidates: Iterable[Any]) -> str:
    """One short line per candidate for the operator message.

    Group, symbol, side, entry and when it opened -- enough to tell two
    positions apart at a glance, and nothing that would let a reader mistake
    the message for an instruction to act.
    """

    parts: list[str] = []
    for candidate in candidates:
        summary = getattr(candidate, "lifecycle_summary", None) or {}
        parts.append(
            "lifecycle {id} {symbol} {side} entry {low}-{high} opened {entered}".format(
                id=summary.get("id", "?"),
                symbol=getattr(candidate, "symbol", "?"),
                side=getattr(candidate, "side", "?"),
                low=summary.get("entry_range_low", "?"),
                high=summary.get("entry_range_high", "?"),
                entered=str(summary.get("entered_at") or "?")[:19],
            )
        )
    return "; ".join(parts) if parts else "(no verifiable candidate)"


def request_management_target_confirmation(
    session_factory,
    *,
    raw_message_id: int,
    candidates: Iterable[Any],
    snapshot_stale: bool,
    now: datetime,
    capture: Any = None,
) -> tuple[int, ...]:
    """Park the message's instruction items and tell somebody, once.

    Returns the item ids moved. Idempotent by state: an item already awaiting
    confirmation is left alone, so a re-run of the same message does not
    produce a second alert.
    """

    described = list(candidates)
    reason_code = confirmation_reason_code(len(described), snapshot_stale)
    moved: list[int] = []
    chat_id = 0
    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        chat_id = int(raw_message.chat_id) if raw_message is not None else 0
        items = (
            session.query(MessageInstructionItem)
            .filter(
                MessageInstructionItem.raw_message_id == int(raw_message_id),
                MessageInstructionItem.retired_at.is_(None),
                MessageInstructionItem.status.in_(("pending", "executing")),
                MessageInstructionItem.instruction_kind != "entry",
            )
            .order_by(MessageInstructionItem.sequence, MessageInstructionItem.id)
            .all()
        )
        for item in items:
            item.status = AWAITING_CONFIRMATION
            item.last_progress_at = now
            item.updated_at = now
            moved.append(int(item.id))
        session.commit()
    if capture is None:
        from telegram_kol_research.runtime_incident_adapters import (
            capture_management_target_needs_confirmation,
            capture_runtime_incident_best_effort,
        )

        def capture(**kwargs: Any) -> None:
            capture_runtime_incident_best_effort(
                capture_management_target_needs_confirmation,
                session_factory,
                **kwargs,
            )

    capture(
        raw_message_id=int(raw_message_id),
        chat_id=chat_id,
        candidate_count=len(described),
        reason_code=reason_code,
        candidate_digest=describe_candidates(described),
        occurred_at=now,
    )
    return tuple(moved)


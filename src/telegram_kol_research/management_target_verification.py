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
carries an execution binding *and* the reconcile loop's latest round still
found that binding's position open on the exchange.

**The snapshot is read, never taken.** The reconcile loop already asks Deepcoin
for the live positions every round, so this adds no exchange call and no new
failure mode. The question is only which of its records still answers "is this
position open *now*".

**Not the observation table.** ``position_reconciliation_observations`` is
append-on-change and records only *non-zero* positions: a position that closes
simply stops producing rows, and its last row still shows the size it had while
it was open. Reading the newest row per position would therefore call every
closed position open -- lifecycle 1074's bug, rebuilt. Nor is a recent row a
freshness test: in production the newest row was four hours old while reconcile
ran every twenty seconds. The first deployment of this gate demanded one, found
none, and turned every auto_trade management instruction into a confirmation
request (incident 2082, raw 15628, ``snapshot_stale``).

**The binding rows.** Every round rewrites each binding from the live positions
list and stamps ``recovered_at``, so the binding carries both answers at once:

* **Is our view current?** The newest ``recovered_at`` is when the loop last
  ran. Older than ``max_age`` and the answer is "unknown".
* **What is open?** ``status == "active"`` with
  ``last_exchange_status == "position_ownership_verified"`` is the reconcile
  saying, this round, that it found this binding's position in the snapshot.
  When the position goes, the same round moves the binding to ``closed`` or
  ``stale``. A binding the loop skipped keeps an old ``recovered_at`` and is
  excluded by the same window.

**Staleness is not emptiness.** With the loop out of date this returns ``None``
-- "unknown" -- and the caller must notify rather than judge. Returning an empty
set instead would read as "we looked and nothing is open", which would
disqualify every candidate and turn a stale view into a confident refusal.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import func

from telegram_kol_research.models import (
    ExecutionBinding,
    MessageInstructionItem,
    RawMessage,
    StrategyLifecycle,
)


logger = logging.getLogger(__name__)

#: How long ago the reconcile loop may have run and still settle the question.
#: Beyond this the answer is "unknown", which routes to a confirmation request.
DEFAULT_SNAPSHOT_MAX_AGE = timedelta(minutes=5)

#: What the reconcile writes on a binding whose position it saw this round.
LIVE_OWNERSHIP_STATUS = "active"
LIVE_OWNERSHIP_EVIDENCE = "position_ownership_verified"

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
    last_round = (
        session.query(func.max(ExecutionBinding.recovered_at))
        .filter(ExecutionBinding.venue == venue)
        .scalar()
    )
    if last_round is None or _naive(last_round) < cutoff:
        # The reconcile loop has not looked at the exchange recently enough for
        # anything it recorded to settle the question.
        return None
    rows = (
        session.query(ExecutionBinding)
        .filter(
            ExecutionBinding.venue == venue,
            ExecutionBinding.status == LIVE_OWNERSHIP_STATUS,
            ExecutionBinding.last_exchange_status == LIVE_OWNERSHIP_EVIDENCE,
            ExecutionBinding.recovered_at >= cutoff,
        )
        .all()
    )
    open_ids: set[str] = set()
    for row in rows:
        open_ids.update(_split_pos_ids(row.pos_id))
    return frozenset(open_ids)


def _split_pos_ids(value: Any) -> tuple[str, ...]:
    """A binding may own several positions; they are stored comma-joined."""

    return tuple(item.strip() for item in str(value or "").split(",") if item.strip())


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
            pos_ids_by_binding[int(binding.id)] = _split_pos_ids(binding.pos_id)
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


def numbered_candidates(candidates: Iterable[Any]) -> tuple[dict[str, Any], ...]:
    """The offered candidates, in the order the operator will see them.

    A-9. The number is the whole point: ``/choose 15155 2`` is answerable from
    a phone, a lifecycle id is not. It is positional and recorded with the
    parked items, so the number the operator reads and the number the command
    resolves are the same list even if the candidates would be computed
    differently later.
    """

    numbered: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        summary = getattr(candidate, "lifecycle_summary", None) or {}
        numbered.append(
            {
                "number": index,
                "lifecycle_id": summary.get("id"),
                "symbol": getattr(candidate, "symbol", None),
                "side": getattr(candidate, "side", None),
                "entry_range_low": summary.get("entry_range_low"),
                "entry_range_high": summary.get("entry_range_high"),
                "entered_at": str(summary.get("entered_at") or "")[:19] or None,
            }
        )
    return tuple(numbered)


def describe_candidates(candidates: Iterable[Any]) -> str:
    """One short numbered line per candidate, plus how to answer.

    Group, symbol, side, entry and when it opened -- enough to tell two
    positions apart at a glance, and nothing that would let a reader mistake
    the message for an instruction to act. A-9 adds the number and the reply
    format, because before it the notification asked a question the operator
    had no way to answer.
    """

    parts = [
        "[{number}] lifecycle {id} {symbol} {side} entry {low}-{high} opened {entered}".format(
            number=item["number"],
            id=item["lifecycle_id"] if item["lifecycle_id"] is not None else "?",
            symbol=item["symbol"] or "?",
            side=item["side"] or "?",
            low=item["entry_range_low"] if item["entry_range_low"] is not None else "?",
            high=item["entry_range_high"] if item["entry_range_high"] is not None else "?",
            entered=item["entered_at"] or "?",
        )
        for item in numbered_candidates(candidates)
    ]
    return "; ".join(parts) if parts else "(no verifiable candidate)"


def reply_instructions(raw_message_id: int, candidate_count: int) -> str:
    """What the operator should type back."""

    if candidate_count <= 0:
        return f"/dismiss {int(raw_message_id)}"
    return (
        f"/choose {int(raw_message_id)} <1-{candidate_count}> "
        f"or /dismiss {int(raw_message_id)}"
    )


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

    **The question is only asked when something is actually waiting on it**
    (A-7b). Both confirmations production ever raised -- raw 15628 and raw
    15660 -- parked nothing, because neither message had an instruction item
    at all, and an operator who answers one of those gets told there is
    nothing awaiting confirmation. Asking before knowing anything is parkable
    produces a question nobody can answer, and A-8's inventory is a long
    account of what unanswerable alerts cost. With nothing parked the message
    is still recorded -- the decision row keeps its own reason, which for
    these is ``mimo_no_action`` or one of A-8's quiet codes -- it just does
    not page anybody.
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
        offered = numbered_candidates(described)
        for item in items:
            item.status = AWAITING_CONFIRMATION
            # The list the operator was shown is stored with the item, so
            # ``/choose N`` resolves against exactly what was offered rather
            # than against a set recomputed minutes later.
            payload = _result_payload(item.result_json)
            payload["confirmation_candidates"] = [dict(row) for row in offered]
            payload["confirmation_reason_code"] = reason_code
            item.result_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            item.last_progress_at = now
            item.updated_at = now
            moved.append(int(item.id))
        session.commit()
    if not moved:
        logger.info(
            "management target confirmation not raised, nothing was parked "
            "raw_message_id=%s reason_code=%s candidates=%s",
            int(raw_message_id),
            reason_code,
            len(described),
        )
        return ()
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
        candidate_digest=(
            f"{describe_candidates(described)} | reply: "
            f"{reply_instructions(int(raw_message_id), len(described))}"
        ),
        occurred_at=now,
    )
    return tuple(moved)


def _result_payload(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


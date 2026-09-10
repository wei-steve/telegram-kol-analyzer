"""One replacement sequence for protection orders, in two orders of operations.

``set-position-sltp`` **adds** a TPSL; it never edits one. Eighteen production
writes in the 6-pre-3 study produced eighteen order ids and left every earlier
order live. So "modify the stop" is always place-then-cancel or
cancel-then-place, and which one is correct depends on what two live orders of
that kind do to each other:

* **Stops replace new-first** (the A-5e order, already proven in
  ``stop_loss_size_convergence``): place the new stop, confirm it by read-back,
  cancel the old ones, confirm they are gone, and only then move the ledger. Two
  live stops are a transient over-protection -- the nearer one fires first and
  the position is still protected either way -- while a gap between cancel and
  place is a naked position. Nothing here ever cancels the *new* stop: if the
  old one will not go, a person is told and the position stays over-protected,
  which is the safe side of that trade.
* **Take profits replace old-first.** Two live take profits are not harmless:
  each closes its own size, so together they can reduce more than the position
  holds. The gap they leave is not a naked position -- the stop is untouched
  through the whole take-profit sequence -- so the safe order is the opposite
  one: cancel the old set, confirm it is gone, then place the new set. A place
  that then fails is an alert and a retry for take-profit convergence; it never
  freezes the stop.

When both groups change, the caller runs the stop group first: the position's
downside is settled before anything touches its upside.

This module owns the sequence and nothing else. What to place, and which old
orders are ours, are answered by :mod:`protection_authority` and passed in.
Every exchange call goes through ``position_mutation_gateway``, so the durable
intent, the authority revalidation, the ``sCode`` check and the
``DeepcoinTpslWriteLimiter`` all still apply exactly as before.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from telegram_kol_research.deepcoin_client import DeepcoinDefiniteRejection
from telegram_kol_research.models import (
    PositionProtectionIncident,
    PositionProtectionLedger,
)
from telegram_kol_research.position_mutation_gateway import (
    cancel_exact_position_sltp,
    submit_exact_position_sltp,
)

logger = logging.getLogger(__name__)

GROUP_STOP = "stop"
GROUP_TAKE_PROFIT = "take_profit"

#: The stop group could not finish its cancels. The new stop is live and at
#: least one old one is too: over-protected, never unprotected. Already in
#: ``config.ALWAYS_NOTIFIED_INCIDENT_TYPES`` -- an environment whitelist must
#: not be able to silence it, because nothing retries this path.
REPLACE_INCOMPLETE_INCIDENT_TYPE = "stop_resize_replace_incomplete"
#: The take-profit group cancelled its old orders and could not place the new
#: ones. The position keeps its stop, so this is an alert and a retry, not a
#: freeze.
TAKE_PROFIT_REPLACE_INCOMPLETE_INCIDENT_TYPE = "take_profit_replace_incomplete"

STATUS_SUCCEEDED = "succeeded"
STATUS_INCOMPLETE = "incomplete"
STATUS_REJECTED = "rejected"
STATUS_UNKNOWN = "unknown"
STATUS_SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class NewProtectionOrder:
    """One protection order to place, as the exact payload its caller built.

    The payload is not assembled here on purpose. Trigger type, order price and
    whole-position sizing are decisions the calling path already knows how to
    make, and this module's job is the order of operations, not the shape of a
    write.
    """

    purpose: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ProtectionReplacementPlan:
    venue: str
    pos_id: str
    instrument_id: str
    execution_binding_id: int
    execution_order_leg_id: int
    group: str
    new_orders: tuple[NewProtectionOrder, ...]
    old_order_ids: tuple[str, ...]
    idempotency_prefix: str


@dataclass(frozen=True, slots=True)
class ProtectionReplacementResult:
    plan: ProtectionReplacementPlan
    status: str
    reason_code: str | None = None
    new_order_ids: tuple[str, ...] = ()
    cancelled_order_ids: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status == STATUS_SUCCEEDED


#: Given the live pending rows and one order id, say whether that order is still
#: exactly what the caller resolved -- ``None`` if it is, a reason code if it is
#: not. **Required, not optional.** It was optional until 2026-09-10, and the
#: consequence was a claim written into a phase report ("the cancel goes through
#: the four-field read-back") that was not true of one of the two callers,
#: because that caller simply did not pass it. An argument that must be
#: remembered is a check that will eventually be skipped.
PreCancelCheck = Callable[[Sequence[Mapping[str, Any]], str], str | None]


def replace_stop_group(
    session_factory,
    *,
    plan: ProtectionReplacementPlan,
    deepcoin_client: Any,
    executed_at: datetime,
    live_execution_gate: Callable[[], bool],
    pre_cancel_check: PreCancelCheck,
) -> ProtectionReplacementResult:
    """Place the new stops, then retire the old ones. Never the other way."""

    if plan.group != GROUP_STOP:
        raise ValueError("protection_replacement_group_mismatch")
    if not plan.new_orders:
        return ProtectionReplacementResult(
            plan, STATUS_SKIPPED, "replacement_has_no_new_order"
        )

    new_order_ids: list[str] = []
    for index, new_order in enumerate(plan.new_orders):
        try:
            response = submit_exact_position_sltp(
                session_factory=session_factory,
                deepcoin_client=deepcoin_client,
                pos_id=plan.pos_id,
                payload=dict(new_order.payload),
                idempotency_key=f"{plan.idempotency_prefix}:set-stop:{index}",
                live_execution_gate=live_execution_gate,
                now_provider=lambda: executed_at,
                require_readback=True,
                ledger_purpose=new_order.purpose,
            )
        except DeepcoinDefiniteRejection as exc:
            # Nothing was placed for this one and the old stops are untouched:
            # the position is exactly as protected as it was.
            _record_incident(
                session_factory,
                plan=plan,
                incident_type=REPLACE_INCOMPLETE_INCIDENT_TYPE,
                reason_code="stop_replacement_rejected",
                detail=f"{type(exc).__name__}: {str(exc)[:120]}",
                created_at=executed_at,
                new_order_ids=tuple(new_order_ids),
            )
            return ProtectionReplacementResult(
                plan,
                STATUS_REJECTED,
                "stop_replacement_rejected",
                new_order_ids=tuple(new_order_ids),
            )
        except Exception as exc:
            _record_incident(
                session_factory,
                plan=plan,
                incident_type=REPLACE_INCOMPLETE_INCIDENT_TYPE,
                reason_code="stop_replacement_outcome_unknown",
                detail=f"{type(exc).__name__}: {str(exc)[:120]}",
                created_at=executed_at,
                new_order_ids=tuple(new_order_ids),
            )
            return ProtectionReplacementResult(
                plan,
                STATUS_UNKNOWN,
                "stop_replacement_outcome_unknown",
                new_order_ids=tuple(new_order_ids),
            )
        order_id = _response_order_id(response)
        if not order_id:
            _record_incident(
                session_factory,
                plan=plan,
                incident_type=REPLACE_INCOMPLETE_INCIDENT_TYPE,
                reason_code="stop_replacement_new_order_id_missing",
                detail="submit confirmed without an order id",
                created_at=executed_at,
                new_order_ids=tuple(new_order_ids),
            )
            return ProtectionReplacementResult(
                plan,
                STATUS_INCOMPLETE,
                "stop_replacement_new_order_id_missing",
                new_order_ids=tuple(new_order_ids),
            )
        new_order_ids.append(order_id)

    # From here the position is over-protected, never under. Nothing below
    # cancels a new stop: tidying up is exactly the write that could leave the
    # position naked.
    cancelled, failure = _cancel_and_confirm(
        session_factory,
        plan=plan,
        deepcoin_client=deepcoin_client,
        executed_at=executed_at,
        live_execution_gate=live_execution_gate,
        pre_cancel_check=pre_cancel_check,
    )
    if failure is not None:
        _record_incident(
            session_factory,
            plan=plan,
            incident_type=REPLACE_INCOMPLETE_INCIDENT_TYPE,
            reason_code=failure,
            detail=(
                f"new={','.join(new_order_ids)} "
                f"old_remaining={','.join(sorted(set(plan.old_order_ids) - set(cancelled)))}"
            ),
            created_at=executed_at,
            new_order_ids=tuple(new_order_ids),
        )
        return ProtectionReplacementResult(
            plan,
            STATUS_INCOMPLETE,
            failure,
            new_order_ids=tuple(new_order_ids),
            cancelled_order_ids=tuple(cancelled),
        )
    return ProtectionReplacementResult(
        plan,
        STATUS_SUCCEEDED,
        None,
        new_order_ids=tuple(new_order_ids),
        cancelled_order_ids=tuple(cancelled),
    )


def replace_take_profit_group(
    session_factory,
    *,
    plan: ProtectionReplacementPlan,
    deepcoin_client: Any,
    executed_at: datetime,
    live_execution_gate: Callable[[], bool],
    pre_cancel_check: PreCancelCheck,
) -> ProtectionReplacementResult:
    """Retire the old take profits first, so two of them never fire together."""

    if plan.group != GROUP_TAKE_PROFIT:
        raise ValueError("protection_replacement_group_mismatch")

    cancelled, failure = _cancel_and_confirm(
        session_factory,
        plan=plan,
        deepcoin_client=deepcoin_client,
        executed_at=executed_at,
        live_execution_gate=live_execution_gate,
        pre_cancel_check=pre_cancel_check,
    )
    if failure is not None:
        # The old take profits are still armed and nothing new was placed. That
        # is the state the position was already in, so it is reported and left
        # alone rather than half-replaced.
        _record_incident(
            session_factory,
            plan=plan,
            incident_type=TAKE_PROFIT_REPLACE_INCOMPLETE_INCIDENT_TYPE,
            reason_code=failure,
            detail=f"old_remaining={','.join(sorted(set(plan.old_order_ids) - set(cancelled)))}",
            created_at=executed_at,
        )
        return ProtectionReplacementResult(
            plan, STATUS_INCOMPLETE, failure, cancelled_order_ids=tuple(cancelled)
        )

    new_order_ids: list[str] = []
    for index, new_order in enumerate(plan.new_orders):
        try:
            response = submit_exact_position_sltp(
                session_factory=session_factory,
                deepcoin_client=deepcoin_client,
                pos_id=plan.pos_id,
                payload=dict(new_order.payload),
                idempotency_key=f"{plan.idempotency_prefix}:set-tp:{index}",
                live_execution_gate=live_execution_gate,
                now_provider=lambda: executed_at,
                require_readback=True,
                ledger_purpose=new_order.purpose,
            )
        except Exception as exc:
            _record_incident(
                session_factory,
                plan=plan,
                incident_type=TAKE_PROFIT_REPLACE_INCOMPLETE_INCIDENT_TYPE,
                reason_code="take_profit_replacement_place_failed",
                detail=f"{type(exc).__name__}: {str(exc)[:120]}",
                created_at=executed_at,
                new_order_ids=tuple(new_order_ids),
            )
            return ProtectionReplacementResult(
                plan,
                STATUS_INCOMPLETE,
                "take_profit_replacement_place_failed",
                new_order_ids=tuple(new_order_ids),
                cancelled_order_ids=tuple(cancelled),
            )
        order_id = _response_order_id(response)
        if not order_id:
            _record_incident(
                session_factory,
                plan=plan,
                incident_type=TAKE_PROFIT_REPLACE_INCOMPLETE_INCIDENT_TYPE,
                reason_code="take_profit_replacement_new_order_id_missing",
                detail="submit confirmed without an order id",
                created_at=executed_at,
                new_order_ids=tuple(new_order_ids),
            )
            return ProtectionReplacementResult(
                plan,
                STATUS_INCOMPLETE,
                "take_profit_replacement_new_order_id_missing",
                new_order_ids=tuple(new_order_ids),
                cancelled_order_ids=tuple(cancelled),
            )
        new_order_ids.append(order_id)
    return ProtectionReplacementResult(
        plan,
        STATUS_SUCCEEDED,
        None,
        new_order_ids=tuple(new_order_ids),
        cancelled_order_ids=tuple(cancelled),
    )


def _cancel_and_confirm(
    session_factory,
    *,
    plan: ProtectionReplacementPlan,
    deepcoin_client: Any,
    executed_at: datetime,
    live_execution_gate: Callable[[], bool],
    pre_cancel_check: PreCancelCheck,
) -> tuple[list[str], str | None]:
    """Cancel each old order and prove it left ``trigger-orders-pending``."""

    cancelled: list[str] = []
    if not plan.old_order_ids:
        return cancelled, None

    pending_before = _read_pending(deepcoin_client, plan.instrument_id)
    if pending_before is None:
        return cancelled, "protection_cancel_precheck_unreadable"

    for order_id in plan.old_order_ids:
        mismatch = pre_cancel_check(pending_before, order_id)
        if mismatch is not None:
            return cancelled, mismatch
        try:
            cancel_exact_position_sltp(
                session_factory=session_factory,
                deepcoin_client=deepcoin_client,
                pos_id=plan.pos_id,
                order_id=str(order_id),
                instrument_id=plan.instrument_id,
                idempotency_key=f"{plan.idempotency_prefix}:cancel:{order_id}",
                live_execution_gate=live_execution_gate,
                now_provider=lambda: executed_at,
            )
        except Exception:
            logger.warning(
                "protection replacement could not cancel old order pos_id=%s ord_id=%s",
                plan.pos_id,
                order_id,
                exc_info=True,
            )
            return cancelled, "protection_old_order_cancel_failed"
        cancelled.append(str(order_id))

    remaining = _orders_still_pending(
        deepcoin_client, instrument_id=plan.instrument_id, order_ids=plan.old_order_ids
    )
    if remaining is None:
        # An unreadable list is not proof of absence. Retiring the ledger rows
        # here would claim an order is gone that may still be armed.
        return cancelled, "protection_old_order_absence_unproven"
    if remaining:
        return cancelled, "protection_old_order_still_pending"
    _retire_ledger_rows(
        session_factory,
        venue=plan.venue,
        order_ids=plan.old_order_ids,
        cancelled_at=executed_at,
    )
    return cancelled, None


def _read_pending(
    deepcoin_client: Any, instrument_id: str
) -> list[Mapping[str, Any]] | None:
    lister = getattr(deepcoin_client, "list_trigger_orders_pending", None)
    if not callable(lister):
        return None
    try:
        rows = lister(inst_id=instrument_id)
    except Exception:
        logger.warning(
            "protection replacement could not read pending trigger orders inst_id=%s",
            instrument_id,
            exc_info=True,
        )
        return None
    if not isinstance(rows, list):
        return None
    return [row for row in rows if isinstance(row, Mapping)]


def _orders_still_pending(
    deepcoin_client: Any, *, instrument_id: str, order_ids: Sequence[str]
) -> tuple[str, ...] | None:
    """Which of ``order_ids`` are still listed. ``None`` means "could not tell"."""

    rows = _read_pending(deepcoin_client, instrument_id)
    if rows is None:
        return None
    listed = set()
    for row in rows:
        observed = str(
            row.get("ordId") or row.get("orderId") or row.get("order_id") or ""
        )
        if observed:
            listed.add(observed)
    return tuple(sorted(str(item) for item in order_ids if str(item) in listed))


def _retire_ledger_rows(
    session_factory, *, venue: str, order_ids: Sequence[str], cancelled_at: datetime
) -> None:
    with session_factory() as session:
        for order_id in order_ids:
            row = (
                session.query(PositionProtectionLedger)
                .filter_by(venue=str(venue).lower(), order_id=str(order_id))
                .one_or_none()
            )
            if row is not None:
                row.status = "cancelled"
                row.last_seen_at = cancelled_at
                row.updated_at = cancelled_at
        session.commit()


def _record_incident(
    session_factory,
    *,
    plan: ProtectionReplacementPlan,
    incident_type: str,
    reason_code: str,
    detail: str,
    created_at: datetime,
    new_order_ids: tuple[str, ...] = (),
) -> None:
    fingerprint = f"{incident_type}:{plan.idempotency_prefix}:{reason_code}"[:64]
    evidence = {
        "reason_code": reason_code,
        "detail": detail,
        "pos_id": plan.pos_id,
        "group": plan.group,
        "old_order_ids": list(plan.old_order_ids),
        "new_order_ids": list(new_order_ids),
        "manual_action": (
            "Cancel the old stop by hand: the new one is confirmed and the old "
            "one is still armed. Nothing retries this path."
            if incident_type == REPLACE_INCOMPLETE_INCIDENT_TYPE
            else "Read this position's take profits on the exchange and settle "
            "them by hand; the stop is untouched."
        ),
    }
    with session_factory() as session:
        exists = (
            session.query(PositionProtectionIncident.id)
            .filter(PositionProtectionIncident.fingerprint == fingerprint)
            .first()
        )
        if exists is not None:
            return
        session.add(
            PositionProtectionIncident(
                venue=plan.venue,
                execution_binding_id=plan.execution_binding_id,
                execution_order_leg_id=plan.execution_order_leg_id,
                pos_id=plan.pos_id,
                incident_type=incident_type,
                fingerprint=fingerprint,
                evidence_json=json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                delivery_status="pending",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.commit()
    if incident_type == REPLACE_INCOMPLETE_INCIDENT_TYPE:
        _capture_runtime_incident(
            session_factory,
            plan=plan,
            reason_code=reason_code,
            new_order_ids=new_order_ids,
            created_at=created_at,
        )


def _capture_runtime_incident(
    session_factory,
    *,
    plan: ProtectionReplacementPlan,
    reason_code: str,
    new_order_ids: tuple[str, ...],
    created_at: datetime,
) -> None:
    """The alert an environment whitelist must not be able to silence.

    Recorded twice on purpose, the same way A-5e does it: the protection
    incident keeps it beside the other replacement failures, and the runtime
    incident is the one that always reaches a person.
    """

    try:
        from telegram_kol_research.config import load_runtime_incident_config
        from telegram_kol_research.runtime_incident_adapters import (
            capture_stop_resize_replace_incomplete,
        )

        capture_stop_resize_replace_incomplete(
            session_factory,
            config=load_runtime_incident_config(),
            pos_id=plan.pos_id,
            old_order_id=",".join(plan.old_order_ids),
            new_order_id=(new_order_ids[0] if new_order_ids else None),
            reason_code=reason_code,
            occurred_at=created_at,
        )
    except Exception:  # pragma: no cover - never fails the caller
        logger.warning(
            "protection replacement incident capture failed pos_id=%s",
            plan.pos_id,
            exc_info=True,
        )


def _response_order_id(response: Any) -> str:
    if not isinstance(response, Mapping):
        return ""
    data = response.get("data")
    if isinstance(data, Mapping):
        return str(data.get("ordId") or data.get("orderId") or "")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, Mapping):
                order_id = str(item.get("ordId") or item.get("orderId") or "")
                if order_id:
                    return order_id
    return str(response.get("ordId") or response.get("orderId") or "")

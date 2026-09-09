"""Shrink an owned main stop to the size the position actually still has.

The second half of the convergence-222 failure. Once a staged take-profit fills
and the reduction is named (``partial_take_profit_explanation``), the exchange
holds a stop for ten lots against a five-lot position. Nothing corrects that on
its own, and the oversized stop is what made the next management batch's final
check refuse.

Three properties this module keeps, in order of importance:

* **It only ever shrinks, and only to the live size.** The new size must be
  strictly smaller than the ledger size and exactly equal to what the exchange
  says is still open. There is no path here that enlarges a stop, moves a
  trigger price, or touches a take-profit order.
* **It only acts on a reduction this system already explained.** The gap
  between the ledger size and the live size must be accounted for, to the lot,
  by take-profit orders of the same binding that carry a
  ``partial_take_profit_fill`` judgement. An unexplained gap is left alone --
  that case is a freeze, not a resize.
* **It does not retry.** The idempotency key names the exact target size, so a
  submitted-but-unconfirmed write is never sent twice: the gateway returns the
  existing intent instead. A read-back mismatch raises, the ledger keeps its old
  size, and an incident is filed for a person.

**Shrinking is a replacement, not an addition** (A-5e). ``set-position-sltp``
adds a TPSL rather than editing one: eighteen production writes produced
eighteen order ids, and the old stop stays live. Submitting the smaller stop
and stopping there leaves ten lots and five lots armed against the same five
lots, and at the same trigger price the oversized one can fire first -- which
is the opposite of what a resize is for. So the sequence is: place the new
stop, confirm it, cancel the old order id, confirm it is gone, and only then
retire the old ledger row.

The order matters in one direction only. The new stop goes on first so the
position is never without one, and nothing here ever cancels the new stop --
if the cancel of the old one fails, the position is over-protected and a
person is told, which is the safe side of that trade.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping

from telegram_kol_research.deepcoin_client import (
    DeepcoinDefiniteRejection,
    DeepcoinRequestOutcomeUnknown,
)
from telegram_kol_research.models import (
    PositionProtectionIncident,
    PositionProtectionLedger,
    PositionTakeProfitOrder,
)
from telegram_kol_research.position_mutation_gateway import (
    cancel_exact_position_sltp,
    exact_position_write_gate,
    submit_exact_position_sltp,
)


logger = logging.getLogger(__name__)

MAIN_STOP_PURPOSE = "stop_loss"
RESIZE_INCIDENT_TYPE = "stop_loss_resize_readback_mismatch"
#: A-5e. The new stop is on the exchange and the old one is still there too.
#: The position is over-protected, not unprotected, so nothing here is undone
#: automatically -- cancelling the new stop to tidy up would be the one change
#: that could leave the position naked. A person cancels the old order.
REPLACE_INCOMPLETE_INCIDENT_TYPE = "stop_resize_replace_incomplete"


@dataclass(frozen=True, slots=True)
class StopLossResizePlan:
    ledger_id: int
    venue: str
    execution_binding_id: int
    execution_order_leg_id: int
    pos_id: str
    instrument_id: str
    order_id: str
    trigger_price: str
    current_size: str
    new_size: str
    explained_order_ids: tuple[str, ...]

    @property
    def idempotency_key(self) -> str:
        return (
            f"sl-resize:{self.execution_binding_id}:{self.pos_id}:{self.new_size}"
        )


@dataclass(frozen=True, slots=True)
class StopLossResizeResult:
    plan: StopLossResizePlan
    status: str
    reason_code: str | None = None


def plan_stop_loss_resizes(
    session_factory,
    *,
    positions: Iterable[Mapping[str, Any]],
    venue: str = "deepcoin",
) -> list[StopLossResizePlan]:
    """Read-only: which owned main stops are larger than their position."""

    live_sizes = _live_sizes_by_position(positions)
    if not live_sizes:
        return []
    plans: list[StopLossResizePlan] = []
    with session_factory() as session:
        rows = (
            session.query(PositionProtectionLedger)
            .filter(
                PositionProtectionLedger.venue == venue,
                PositionProtectionLedger.purpose == MAIN_STOP_PURPOSE,
                PositionProtectionLedger.status == "verified",
                PositionProtectionLedger.pos_id.in_(sorted(live_sizes)),
            )
            .order_by(PositionProtectionLedger.id.asc())
            .all()
        )
        # ``set-position-sltp`` answers with a *new* order id, so a completed
        # resize leaves the pre-resize row in the ledger until the reconcile
        # retires it. Judge per position rather than per row: if any verified
        # stop already carries the live size the position is converged, and if
        # several disagree the owner is ambiguous and nothing is written.
        for pos_id, group in _rows_by_position(rows).items():
            live_size = live_sizes.get(pos_id)
            if live_size is None:
                continue
            if any(_decimal(item.size_text) == live_size for item in group):
                continue
            if len(group) != 1:
                logger.warning(
                    "stop-loss resize skipped: %s verified stops disagree pos_id=%s",
                    len(group),
                    pos_id,
                )
                continue
            row = group[0]
            ledger_size = _decimal(row.size_text)
            trigger_price = str(row.trigger_price or "").strip()
            order_id = str(row.order_id or "").strip()
            instrument_id = str(row.instrument_id or "").strip()
            if (
                live_size is None
                or ledger_size is None
                or not trigger_price
                or not order_id
                or not instrument_id
            ):
                continue
            # Only shrink, and never to nothing: a zero live size means the
            # position closed, which is a terminalization question, not a
            # resize one.
            if live_size <= 0 or ledger_size <= live_size:
                continue
            shortfall = ledger_size - live_size
            explained, explained_order_ids = _explained_take_profit_size(
                session,
                venue=venue,
                execution_binding_id=int(row.execution_binding_id),
                pos_id=pos_id,
            )
            if explained != shortfall:
                continue
            plans.append(
                StopLossResizePlan(
                    ledger_id=int(row.id),
                    venue=venue,
                    execution_binding_id=int(row.execution_binding_id),
                    execution_order_leg_id=int(row.execution_order_leg_id),
                    pos_id=pos_id,
                    instrument_id=instrument_id,
                    order_id=order_id,
                    trigger_price=trigger_price,
                    current_size=_text(ledger_size),
                    new_size=_text(live_size),
                    explained_order_ids=explained_order_ids,
                )
            )
    return plans


def execute_stop_loss_resize(
    session_factory,
    *,
    plan: StopLossResizePlan,
    deepcoin_client: Any,
    executed_at: datetime,
    live_execution_gate: Callable[[], bool] | None = None,
) -> StopLossResizeResult:
    """Submit exactly one shrink, confirmed by read-back before the ledger moves."""

    if _decimal(plan.new_size) is None or _decimal(plan.current_size) is None:
        return StopLossResizeResult(plan, "skipped", "resize_size_unreadable")
    if _decimal(plan.new_size) >= _decimal(plan.current_size):
        # Belt for the one rule that must never be violated by a code path
        # change upstream: this function shrinks or does nothing.
        return StopLossResizeResult(plan, "skipped", "resize_would_not_shrink")

    gate = live_execution_gate or (
        lambda: exact_position_write_gate(session_factory, pos_id=plan.pos_id)
    )
    payload = {
        "instType": "SWAP",
        "instId": plan.instrument_id,
        "posId": plan.pos_id,
        "slTriggerPx": plan.trigger_price,
        "slTriggerPxType": "last",
        "slOrdPx": "-1",
        "sz": plan.new_size,
    }
    try:
        response = submit_exact_position_sltp(
            session_factory=session_factory,
            deepcoin_client=deepcoin_client,
            pos_id=plan.pos_id,
            payload=payload,
            idempotency_key=plan.idempotency_key,
            live_execution_gate=gate,
            now_provider=lambda: executed_at,
            require_readback=True,
            ledger_purpose=MAIN_STOP_PURPOSE,
        )
    except DeepcoinDefiniteRejection as exc:
        _record_resize_incident(
            session_factory,
            plan=plan,
            reason_code="resize_rejected",
            detail=type(exc).__name__,
            created_at=executed_at,
        )
        return StopLossResizeResult(plan, "rejected", "resize_rejected")
    except DeepcoinRequestOutcomeUnknown as exc:
        # The read-back did not agree with what we asked for, or never came
        # back at all. The ledger still says the old size, which keeps the
        # protection frozen, and the idempotency key stops any later pass from
        # sending this write again.
        _record_resize_incident(
            session_factory,
            plan=plan,
            reason_code="resize_readback_mismatch",
            detail=str(exc)[:200],
            created_at=executed_at,
        )
        return StopLossResizeResult(plan, "unknown", "resize_readback_mismatch")
    except Exception as exc:  # pragma: no cover - defensive, mirrors gateway users
        _record_resize_incident(
            session_factory,
            plan=plan,
            reason_code="resize_failed",
            detail=type(exc).__name__,
            created_at=executed_at,
        )
        logger.exception(
            "stop-loss resize failed pos_id=%s new_size=%s",
            plan.pos_id,
            plan.new_size,
        )
        return StopLossResizeResult(plan, "failed", "resize_failed")

    # The new stop is confirmed on the exchange. From here the position is
    # over-protected, never under: nothing below cancels the new stop.
    new_order_id = _response_order_id(response)
    if not new_order_id:
        _record_replace_incomplete(
            session_factory,
            plan=plan,
            reason_code="resize_new_order_id_missing",
            detail="submit confirmed without an order id",
            created_at=executed_at,
        )
        return StopLossResizeResult(plan, "incomplete", "resize_new_order_id_missing")
    if _decimal(plan.current_size) in (None, Decimal(0)):
        # A whole-position stop (``sz`` absent or zero) is never cancelled
        # here: it covers whatever the position becomes, and retiring it is a
        # different decision than resizing an exact-size stop.
        _record_replace_incomplete(
            session_factory,
            plan=plan,
            reason_code="resize_old_stop_is_whole_position",
            detail=f"old order {plan.order_id} carries no exact size",
            created_at=executed_at,
        )
        return StopLossResizeResult(
            plan, "incomplete", "resize_old_stop_is_whole_position"
        )
    try:
        cancel_exact_position_sltp(
            session_factory=session_factory,
            deepcoin_client=deepcoin_client,
            pos_id=plan.pos_id,
            order_id=plan.order_id,
            instrument_id=plan.instrument_id,
            idempotency_key=f"{plan.idempotency_key}:cancel-stop:{plan.order_id}",
            live_execution_gate=gate,
            now_provider=lambda: executed_at,
        )
    except Exception as exc:
        _record_replace_incomplete(
            session_factory,
            plan=plan,
            reason_code="resize_old_stop_cancel_failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            created_at=executed_at,
            new_order_id=new_order_id,
        )
        return StopLossResizeResult(
            plan, "incomplete", "resize_old_stop_cancel_failed"
        )
    if not _old_stop_is_gone(deepcoin_client, plan=plan):
        # The cancel was accepted but the order is still listed, or the list
        # could not be read. Either way the ledger must not claim the old stop
        # is retired while it may still fire.
        _record_replace_incomplete(
            session_factory,
            plan=plan,
            reason_code="resize_old_stop_still_pending",
            detail=f"order {plan.order_id} still in trigger-orders-pending",
            created_at=executed_at,
            new_order_id=new_order_id,
        )
        return StopLossResizeResult(
            plan, "incomplete", "resize_old_stop_still_pending"
        )
    _mark_old_stop_cancelled(
        session_factory, order_id=plan.order_id, cancelled_at=executed_at
    )
    return StopLossResizeResult(plan, "succeeded", None)


def _old_stop_is_gone(deepcoin_client: Any, *, plan: StopLossResizePlan) -> bool:
    """Whether the cancelled order really left ``trigger-orders-pending``.

    A read that fails answers "not gone". The alternative -- treating an
    unreadable list as proof of absence -- would retire the ledger row for an
    order that may still be armed, which is the exact mistake this step exists
    to prevent.
    """

    lister = getattr(deepcoin_client, "list_trigger_orders_pending", None)
    if not callable(lister):
        return False
    try:
        rows = lister(inst_id=plan.instrument_id)
    except Exception:
        logger.warning(
            "stop-loss resize could not read pending trigger orders pos_id=%s",
            plan.pos_id,
            exc_info=True,
        )
        return False
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        observed = str(
            row.get("ordId") or row.get("orderId") or row.get("order_id") or ""
        )
        if observed == plan.order_id:
            return False
    return True


def _mark_old_stop_cancelled(
    session_factory,
    *,
    order_id: str,
    cancelled_at: datetime,
) -> None:
    """Retire the superseded ledger row, the same way break-even does."""

    with session_factory() as session:
        row = (
            session.query(PositionProtectionLedger)
            .filter_by(venue="deepcoin", order_id=order_id)
            .one_or_none()
        )
        if row is not None:
            row.status = "cancelled"
            row.last_seen_at = cancelled_at
            row.updated_at = cancelled_at
            session.commit()


def _record_replace_incomplete(
    session_factory,
    *,
    plan: StopLossResizePlan,
    reason_code: str,
    detail: str,
    created_at: datetime,
    new_order_id: str | None = None,
) -> None:
    """Two stops are live and only a person can settle which one goes.

    Recorded twice on purpose: the protection incident keeps it beside the
    other resize failures, and the runtime incident is the one that cannot be
    silenced by an environment whitelist -- an over-protected position can
    fire the wrong stop first, and nothing retries this path on its own.
    """

    _record_resize_incident(
        session_factory,
        plan=plan,
        reason_code=reason_code,
        detail=detail,
        created_at=created_at,
        incident_type=REPLACE_INCOMPLETE_INCIDENT_TYPE,
    )
    try:
        from telegram_kol_research.config import load_runtime_incident_config
        from telegram_kol_research.runtime_incident_adapters import (
            capture_stop_resize_replace_incomplete,
        )

        capture_stop_resize_replace_incomplete(
            session_factory,
            config=load_runtime_incident_config(),
            pos_id=plan.pos_id,
            old_order_id=plan.order_id,
            new_order_id=new_order_id,
            reason_code=reason_code,
            occurred_at=created_at,
        )
    except Exception:  # pragma: no cover - defensive, never fails the caller
        logger.warning(
            "stop resize replace incident capture failed pos_id=%s", plan.pos_id,
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


def _rows_by_position(rows) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for row in rows:
        pos_id = str(row.pos_id or "").strip()
        if pos_id:
            grouped.setdefault(pos_id, []).append(row)
    return grouped


def _explained_take_profit_size(
    session,
    *,
    venue: str,
    execution_binding_id: int,
    pos_id: str,
) -> tuple[Decimal, tuple[str, ...]]:
    """Total size of this binding's own take-profit fills, already proven."""

    rows = (
        session.query(PositionTakeProfitOrder)
        .filter(
            PositionTakeProfitOrder.venue == venue,
            PositionTakeProfitOrder.execution_binding_id == execution_binding_id,
            PositionTakeProfitOrder.pos_id == pos_id,
            PositionTakeProfitOrder.status == "completed",
        )
        .order_by(PositionTakeProfitOrder.id.asc())
        .all()
    )
    total = Decimal("0")
    order_ids: list[str] = []
    for row in rows:
        evidence = _load_json(row.evidence_json).get("partial_take_profit_fill")
        if not isinstance(evidence, dict) or not evidence.get("order_id"):
            continue
        size = _decimal(row.size_text)
        if size is None or size <= 0:
            continue
        total += size
        order_ids.append(str(row.order_id))
    return total, tuple(order_ids)


def _record_resize_incident(
    session_factory,
    *,
    plan: StopLossResizePlan,
    reason_code: str,
    detail: str,
    created_at: datetime,
    incident_type: str = RESIZE_INCIDENT_TYPE,
) -> None:
    fingerprint = f"{incident_type}:{plan.idempotency_key}:{reason_code}"[:64]
    evidence = {
        "reason_code": reason_code,
        "detail": detail,
        "pos_id": plan.pos_id,
        "order_id": plan.order_id,
        "ledger_size_text": plan.current_size,
        "requested_size_text": plan.new_size,
        "trigger_price": plan.trigger_price,
        "explained_take_profit_order_ids": list(plan.explained_order_ids),
        "manual_action": (
            "Cancel the old stop order by hand: the new one is confirmed and the "
            "old one is still armed. Nothing retries this path."
            if incident_type == REPLACE_INCOMPLETE_INCIDENT_TYPE
            else "Read the exchange stop for this position and reconcile it by hand; "
            "this resize is never retried automatically."
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


def _live_sizes_by_position(
    positions: Iterable[Mapping[str, Any]],
) -> dict[str, Decimal]:
    sizes: dict[str, Decimal] = {}
    ambiguous: set[str] = set()
    for row in positions:
        if not isinstance(row, Mapping):
            continue
        pos_id = ""
        for key in ("posId", "pos_id", "PositionID", "positionId", "position_id"):
            value = row.get(key)
            if value not in (None, ""):
                pos_id = str(value).strip()
                break
        if not pos_id:
            continue
        size = None
        for key in ("pos", "sz", "size", "availPos", "Po"):
            size = _decimal(row.get(key))
            if size is not None:
                break
        if size is None:
            ambiguous.add(pos_id)
            continue
        size = abs(size)
        if pos_id in sizes and sizes[pos_id] != size:
            ambiguous.add(pos_id)
            continue
        sizes[pos_id] = size
    for pos_id in ambiguous:
        sizes.pop(pos_id, None)
    return sizes


def _load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _text(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return "0" if normalized == "-0" else normalized

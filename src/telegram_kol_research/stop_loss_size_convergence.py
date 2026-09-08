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
    exact_position_write_gate,
    submit_exact_position_sltp,
)


logger = logging.getLogger(__name__)

MAIN_STOP_PURPOSE = "stop_loss"
RESIZE_INCIDENT_TYPE = "stop_loss_resize_readback_mismatch"


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
        for row in rows:
            pos_id = str(row.pos_id or "").strip()
            live_size = live_sizes.get(pos_id)
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
        submit_exact_position_sltp(
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
    return StopLossResizeResult(plan, "succeeded", None)


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
) -> None:
    fingerprint = f"{RESIZE_INCIDENT_TYPE}:{plan.idempotency_key}:{reason_code}"[:64]
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
            "Read the exchange stop for this position and reconcile it by hand; "
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
                incident_type=RESIZE_INCIDENT_TYPE,
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

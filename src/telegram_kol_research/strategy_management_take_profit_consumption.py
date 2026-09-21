"""Pure planning for consuming one exact owned take-profit stage.

Ownership here used to be decided by two readings the venue does not support,
and between them they refused every composite instruction ever planned
(batches 146, 150 and 153; see
``docs/plans/2026-09-21-composite-zero-success-root-cause.md`` section 3):

* a pending ``TPSL`` row had to carry ``posId``.  **It never does** -- those
  rows carry ``instId`` and ``posSide`` and nothing else that points at a
  position (ARCHITECTURE 4.8).  One resting take profit anywhere on the
  instrument therefore produced ``take_profit_order_identity_conflict``, three
  times in a row, and then ``take_profit_cancel_retry_exhausted``.  **No cancel
  was ever attempted.**
* every resting take profit on the *instrument* had to belong to this leg, so
  two legs on one instrument (binding 320) each refused because of the other's
  orders, as did any other strategy's or any manual take profit.

Ownership is now decided by ``protection_authority.resolve_protection_authority``
-- ordId → ledger row, or ``TU == posId`` -- which is the relation 6-pre-3
established and the one batch 173's successful path already goes through.  The
caller resolves it (it needs a session); this module stays pure and is handed
the result.

What did **not** change: an order this module cannot place is never cancelled.
Orders proven to belong to another position are skipped; an unplaceable order
on our own side freezes the component
(``take_profit_unattributable_pending_order``).  Our own take profits are still
compared to the ledger on price and size exactly.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram_kol_research.native_tpsl import (
    protection_order_position_sides,
    protection_order_sides_consistent,
)
from telegram_kol_research.position_take_profit_orders import (
    canonical_take_profit_evidence_rows,
)
from telegram_kol_research.protection_authority import (
    FREEZE_ORDER_UNATTRIBUTABLE,
    ProtectionAuthority,
)
from telegram_kol_research.strategy_management_contracts import (
    ManagementInstructionContract,
)
from telegram_kol_research.take_profit_fill_predicate import take_profit_fill_proven


#: A ledger row in one of these states names an order that is already gone. It
#: is history, so it is skipped rather than treated as a live stage -- a
#: break-even replacement leaves ``retired`` rows behind, and demanding that
#: they still be cancellable is how batch 153's leg refused.
HISTORY_LEDGER_STATUSES = frozenset({"retired", "cancelled", "canceled", "superseded"})

#: A ledger row in one of these states names an order this leg owns now, or
#: owned until it filled. ``protection_missing`` is here on purpose: that is
#: what ``protection_health`` wrote for every take profit that actually filled,
#: and the first-stage rule below still demands fill evidence before treating
#: any of them as consumed. Anything outside both sets fails closed.
OWNED_LEDGER_STATUSES = frozenset(
    {
        "verified",
        "protected",
        "active",
        "filled",
        "cancel_requested",
        "protection_missing",
    }
)

REFUSAL_UNATTRIBUTABLE_PENDING = "take_profit_unattributable_pending_order"


@dataclass(frozen=True, slots=True)
class TakeProfitConsumptionPlan:
    cancel_order_ids: tuple[str, ...] = ()
    cancel_actions: tuple[dict[str, str], ...] = ()
    proven_filled_quantity: str = "0"
    retained_rows: tuple[dict[str, str], ...] = ()
    resize_rows: tuple[dict[str, str], ...] = ()
    evidence_tier: str = "none"
    refusal_code: str | None = None


def plan_take_profit_consumption(
    *,
    contract: ManagementInstructionContract,
    target_leg,
    pending_orders,
    trigger_history,
    order_history,
    trade_fills,
    protection_ledger,
    trusted_start_size,
    target_remaining_size,
    protection_authority: ProtectionAuthority | None = None,
    pending_snapshot_complete: bool = True,
    recorded_order_statuses: dict[str, tuple[str, ...]] | None = None,
) -> TakeProfitConsumptionPlan:
    if contract.take_profit_consumption != "consume_first_stage":
        return _refusal("take_profit_consumption_policy_missing")
    try:
        trusted_start = _positive_decimal(trusted_start_size)
        target_remaining = _nonnegative_decimal(target_remaining_size)
    except ValueError:
        return _refusal("take_profit_consumption_size_invalid")
    if target_remaining > trusted_start:
        return _refusal("take_profit_consumption_size_invalid")

    target = _target_identity(target_leg)
    if target is None:
        return _refusal("take_profit_order_identity_conflict")

    authority_refusal = _authority_refusal(protection_authority, target)
    if authority_refusal is not None:
        return _refusal(authority_refusal)
    authority_take_profit_ids = {
        item.order_id for item in protection_authority.take_profit_orders
    }
    authority_order_ids = set(protection_authority.order_ids)

    ledger_rows = canonical_take_profit_evidence_rows(protection_ledger)
    all_ledger_ids = [_order_id(row) for row in ledger_rows]
    if any(order_id is None for order_id in all_ledger_ids) or any(
        count != 1 for count in Counter(all_ledger_ids).values()
    ):
        return _refusal("take_profit_order_identity_conflict")
    owned_rows = []
    for row in ledger_rows:
        if str(row.get("purpose") or "take_profit").lower() not in {
            "take_profit", "tp", "profit"
        }:
            continue
        status = str(row.get("status") or "").lower()
        if status in HISTORY_LEDGER_STATUSES:
            continue
        if not _ledger_owner_matches(row, target):
            return _refusal("take_profit_order_identity_conflict")
        try:
            size = _positive_decimal(row.get("size_text"))
        except ValueError:
            return _refusal("take_profit_order_identity_conflict")
        owned_rows.append({**row, "_size": size})
    owned_rows.sort(key=lambda row: _stage_sort_key(row, target["side"]))

    pending_tp_rows = [
        row
        for row in pending_orders
        if isinstance(row, dict) and _is_take_profit_row(row)
    ]
    pending_ids = [_order_id(row) for row in pending_tp_rows]
    if any(order_id is None for order_id in pending_ids) or any(
        count != 1 for count in Counter(pending_ids).values()
    ):
        return _refusal("take_profit_order_identity_conflict")
    owned_by_id = {str(row["order_id"]): row for row in owned_rows}
    pending_by_id: dict[str, dict[str, Any]] = {}
    for order_id, row in zip(pending_ids, pending_tp_rows, strict=True):
        if order_id in authority_take_profit_ids:
            pending_by_id[order_id] = row
            continue
        if order_id in authority_order_ids:
            # The chain places it on this position but not as a take profit --
            # a combined or reclassified order. Not something to cancel here.
            return _refusal("take_profit_order_identity_conflict")
        if order_id in owned_by_id:
            # Our ledger names it and the chain does not. The two records
            # disagree about the same order; cancelling would act on the
            # staler of them.
            return _refusal("take_profit_order_identity_conflict")
        # A *resolved* authority has already accounted for every ``TPSL`` row
        # on this instrument: anything it could not place froze it
        # (``protection_order_unattributable``, handled above), so a row that
        # reaches here was placed on some other position -- another leg of this
        # binding, another strategy, or a manual order. Skipping is not
        # claiming: nothing here reads, resizes or cancels it.
        continue

    for order_id, row in pending_by_id.items():
        if (
            order_id not in owned_by_id
            or _explicit_pos_id(row) not in ("", target["pos_id"])
            or str(row.get("instId") or row.get("instrument_id") or "").upper()
            != target["instrument_id"]
            or not protection_order_sides_consistent(row)
            or protection_order_position_sides(row) != {target["side"]}
            or not _same_decimal(
                row.get("sz") or row.get("size"), owned_by_id[order_id]["_size"]
            )
            or not _same_decimal(
                _take_profit_trigger_price(row),
                owned_by_id[order_id].get("trigger_price"),
            )
        ):
            return _refusal("take_profit_order_identity_conflict")

    if not owned_rows:
        # No live take-profit stage to consume. Every resting take profit on
        # this position has already been accounted for above, so there is
        # nothing to cancel and nothing unknown.
        return TakeProfitConsumptionPlan(evidence_tier="no_take_profit_ledger_row")

    first = owned_rows[0]
    first_order_id = str(first["order_id"])
    first_size = first["_size"]

    cancel_ids: list[str] = []
    filled_quantity = Decimal("0")
    if first_order_id in pending_by_id:
        cancel_ids.append(first_order_id)
        evidence_tier = "exact_pending_owned_order"
    else:
        verdict = take_profit_fill_proven(
            order_id=first_order_id,
            recorded_order_statuses=(recorded_order_statuses or {}).get(
                first_order_id, ()
            ),
            trigger_history=trigger_history,
            pending_snapshot_complete=pending_snapshot_complete,
        )
        if verdict.proven:
            exact_fills = [
                row
                for row in trade_fills
                if isinstance(row, dict) and _order_id(row) == first_order_id
            ]
            if exact_fills and any(
                not _fill_owner_matches(row, target) for row in exact_fills
            ):
                return _refusal("take_profit_order_identity_conflict")
            try:
                filled_quantity = (
                    sum(
                        (_positive_decimal(_fill_size(row)) for row in exact_fills),
                        Decimal("0"),
                    )
                    if exact_fills
                    else first_size
                )
            except ValueError:
                return _refusal("take_profit_order_identity_conflict")
            if filled_quantity > first_size:
                return _refusal("take_profit_order_identity_conflict")
            evidence_tier = verdict.evidence_tier or "exact_terminal_fill"
        elif _terminal_no_fill(first_order_id, order_history):
            evidence_tier = "exact_terminal_no_fill"
        else:
            return _refusal("take_profit_terminal_state_unknown")

    retained = []
    for row in owned_rows[1:]:
        order_id = str(row["order_id"])
        if order_id not in pending_by_id:
            # A later stage disappearing can change the final size. Resolve it
            # before any close rather than guessing from the position delta.
            return _refusal("take_profit_terminal_state_unknown")
        retained.append(
            {
                "order_id": order_id,
                "current_size": _decimal_text(row["_size"]),
                "desired_size": _decimal_text(row["_size"]),
            }
        )

    retained_total = sum(
        (Decimal(row["desired_size"]) for row in retained), Decimal("0")
    )
    excess = max(Decimal("0"), retained_total - target_remaining)
    resize_rows = []
    bounded_retained = []
    for row in retained:
        current = Decimal(row["current_size"])
        if excess >= current:
            cancel_ids.append(row["order_id"])
            excess -= current
            continue
        if excess > 0:
            # Deepcoin has no atomic resize. Cancelling the whole exact owned
            # TP is conservative and avoids a cancel/create gap or duplicate
            # reduce-only exposure. Later stages remain bounded below size.
            cancel_ids.append(row["order_id"])
            excess = Decimal("0")
            continue
        bounded_retained.append(row)

    cancel_actions = tuple(
        {
            "order_id": order_id,
            "pos_id": target["pos_id"],
            "size": _decimal_text(owned_by_id[order_id]["_size"]),
        }
        for order_id in cancel_ids
    )
    return TakeProfitConsumptionPlan(
        cancel_order_ids=tuple(cancel_ids),
        cancel_actions=cancel_actions,
        proven_filled_quantity=_decimal_text(filled_quantity),
        retained_rows=tuple(bounded_retained),
        resize_rows=tuple(resize_rows),
        evidence_tier=evidence_tier,
    )


def _refusal(code: str) -> TakeProfitConsumptionPlan:
    return TakeProfitConsumptionPlan(refusal_code=code)


def _target_identity(value) -> dict[str, Any] | None:
    result = {
        key: _value(value, key)
        for key in (
            "execution_binding_id",
            "execution_order_leg_id",
            "pos_id",
            "instrument_id",
            "side",
        )
    }
    if any(result[key] in (None, "") for key in result):
        return None
    result["execution_binding_id"] = int(result["execution_binding_id"])
    result["execution_order_leg_id"] = int(result["execution_order_leg_id"])
    result["pos_id"] = str(result["pos_id"])
    result["instrument_id"] = str(result["instrument_id"]).upper()
    result["side"] = str(result["side"]).lower()
    return result


def _authority_refusal(
    authority: ProtectionAuthority | None, target: dict[str, Any]
) -> str | None:
    """Whether the resolved protection set may be used to scope this leg.

    An unresolved authority is never "no protection": it is the caller being
    told to stop. ``protection_order_unattributable`` gets its own code so the
    component's reason says what a person has to look at.
    """

    if authority is None:
        return "take_profit_protection_authority_unavailable"
    if not authority.resolved:
        return (
            REFUSAL_UNATTRIBUTABLE_PENDING
            if authority.reason_code == FREEZE_ORDER_UNATTRIBUTABLE
            else str(authority.reason_code or "take_profit_protection_authority_unavailable")
        )
    if (
        str(authority.pos_id or "") != target["pos_id"]
        or str(authority.instrument_id or "").upper() != target["instrument_id"]
        or str(authority.side or "").lower() != target["side"]
    ):
        return "take_profit_order_identity_conflict"
    return None


def _ledger_owner_matches(row: dict[str, Any], target: dict[str, Any]) -> bool:
    return (
        int(row.get("execution_binding_id") or 0) == target["execution_binding_id"]
        and int(row.get("execution_order_leg_id") or 0) == target["execution_order_leg_id"]
        and str(row.get("pos_id") or "") == target["pos_id"]
        and str(row.get("instrument_id") or target["instrument_id"]).upper()
        == target["instrument_id"]
        and str(row.get("side") or target["side"]).lower() == target["side"]
        and str(row.get("status") or "").lower() in OWNED_LEDGER_STATUSES
    )


def _explicit_pos_id(row: dict[str, Any]) -> str:
    """The row's own position id, or ``""``.

    A ``trigger-orders-pending`` TPSL row never carries one, so this is empty
    for every real row and non-empty only for one some local store persisted.
    Where it *is* present it must still be ours.
    """

    return str(
        row.get("posId") or row.get("pos_id") or row.get("positionId") or ""
    ).strip()


def _take_profit_trigger_price(row: dict[str, Any]):
    for key in ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice"):
        value = row.get(key)
        if value in (None, ""):
            continue
        parsed = _decimal_or_none(value)
        if parsed is not None and parsed != 0:
            return value
    return None


def _terminal_no_fill(order_id: str, order_history) -> bool:
    """An ordinary-order row saying this exact order was cancelled or expired.

    ``/trade/orders-history`` does carry ``state``; ``trigger-orders-history``
    does not, so this reads only the ordinary history the caller supplies.
    """

    states = {
        _terminal_state(row)
        for row in order_history
        if isinstance(row, dict) and _order_id(row) == order_id
    }
    states.discard(None)
    return bool(states) and states.issubset({"cancelled", "expired"})


def _fill_owner_matches(row: dict[str, Any], target: dict[str, Any]) -> bool:
    pos_id = str(row.get("posId") or row.get("pos_id") or "")
    return pos_id == target["pos_id"]


def _is_take_profit_row(row: dict[str, Any]) -> bool:
    return (
        str(row.get("triggerOrderType") or "TPSL").upper() == "TPSL"
        and any(
            row.get(key) not in (None, "", "0", 0)
            for key in ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")
        )
    )


def _stage_sort_key(row: dict[str, Any], side: str):
    stage = row.get("stage_index")
    try:
        return (0, int(stage), "", str(row.get("order_id")))
    except (TypeError, ValueError):
        pass
    created = str(row.get("created_at") or "")
    price = _decimal_or_none(row.get("trigger_price")) or Decimal("0")
    price_key = price if side == "long" else -price
    return (1, created, price_key, str(row.get("order_id")))


def _terminal_state(row: dict[str, Any]) -> str | None:
    state = str(
        row.get("state") or row.get("status") or row.get("ordState") or ""
    ).lower()
    if state in {"filled", "success", "executed"}:
        return "filled"
    if state in {"cancelled", "canceled"}:
        return "cancelled"
    if state in {"expired", "failed", "rejected"}:
        return "expired"
    return None


def _order_id(row: dict[str, Any]) -> str | None:
    value = row.get("order_id") or row.get("ordId") or row.get("orderId")
    return str(value) if value not in (None, "") else None


def _fill_size(row: dict[str, Any]):
    return row.get("fillSz") or row.get("fill_size") or row.get("sz")


def _value(value, key: str):
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def _positive_decimal(value) -> Decimal:
    parsed = _decimal_or_none(value)
    if parsed is None or parsed <= 0:
        raise ValueError("positive decimal required")
    return parsed


def _nonnegative_decimal(value) -> Decimal:
    parsed = _decimal_or_none(value)
    if parsed is None or parsed < 0:
        raise ValueError("nonnegative decimal required")
    return parsed


def _decimal_or_none(value) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _same_decimal(left, right) -> bool:
    left_value = _decimal_or_none(left)
    right_value = _decimal_or_none(right)
    return left_value is not None and left_value == right_value


def _decimal_text(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return "0" if normalized in {"", "-0"} else normalized

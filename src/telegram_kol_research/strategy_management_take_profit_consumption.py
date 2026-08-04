"""Pure planning for consuming one exact owned take-profit stage."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from telegram_kol_research.position_take_profit_orders import (
    canonical_take_profit_evidence_rows,
)
from telegram_kol_research.strategy_management_contracts import (
    ManagementInstructionContract,
)


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
        if not _ledger_owner_matches(row, target):
            return _refusal("take_profit_order_identity_conflict")
        try:
            size = _positive_decimal(row.get("size_text"))
        except ValueError:
            return _refusal("take_profit_order_identity_conflict")
        owned_rows.append({**row, "_size": size})
    if not owned_rows:
        return _refusal("take_profit_terminal_state_unknown")
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
    pending_by_id = dict(zip(pending_ids, pending_tp_rows, strict=True))
    owned_by_id = {str(row["order_id"]): row for row in owned_rows}
    if any(
        order_id not in owned_by_id
        or not _pending_owner_matches(row, target)
        or not _same_decimal(row.get("sz") or row.get("size"), owned_by_id[order_id]["_size"])
        or not _same_decimal(
            row.get("tpTriggerPx")
            or row.get("tpTriggerPrice")
            or row.get("closeTPTriggerPrice"),
            owned_by_id[order_id].get("trigger_price"),
        )
        for order_id, row in pending_by_id.items()
    ):
        return _refusal("take_profit_order_identity_conflict")

    first = owned_rows[0]
    first_order_id = str(first["order_id"])
    first_size = first["_size"]
    terminal_rows = [
        row
        for row in (*trigger_history, *order_history)
        if isinstance(row, dict) and _order_id(row) == first_order_id
    ]
    terminal_states = {_terminal_state(row) for row in terminal_rows}
    terminal_states.discard(None)
    if len(terminal_states) > 1:
        return _refusal("take_profit_order_identity_conflict")
    terminal_state = next(iter(terminal_states), None)

    cancel_ids: list[str] = []
    filled_quantity = Decimal("0")
    evidence_tier = "none"
    if terminal_state == "filled":
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
        evidence_tier = "exact_terminal_fill"
    elif first_order_id in pending_by_id:
        cancel_ids.append(first_order_id)
        evidence_tier = "exact_pending_owned_order"
    elif terminal_state in {"cancelled", "expired"}:
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
            desired = current - excess
            resize_rows.append(
                {
                    "order_id": row["order_id"],
                    "from_size": _decimal_text(current),
                    "to_size": _decimal_text(desired),
                }
            )
            row = {**row, "desired_size": _decimal_text(desired)}
            excess = Decimal("0")
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


def _ledger_owner_matches(row: dict[str, Any], target: dict[str, Any]) -> bool:
    return (
        int(row.get("execution_binding_id") or 0) == target["execution_binding_id"]
        and int(row.get("execution_order_leg_id") or 0) == target["execution_order_leg_id"]
        and str(row.get("pos_id") or "") == target["pos_id"]
        and str(row.get("instrument_id") or target["instrument_id"]).upper()
        == target["instrument_id"]
        and str(row.get("side") or target["side"]).lower() == target["side"]
        and str(row.get("status") or "").lower()
        in {"verified", "active", "filled", "cancel_requested", "cancelled"}
    )


def _pending_owner_matches(row: dict[str, Any], target: dict[str, Any]) -> bool:
    side = str(row.get("posSide") or row.get("side") or "").lower()
    side = {"buy": "long", "sell": "short"}.get(side, side)
    return (
        str(row.get("posId") or row.get("pos_id") or "") == target["pos_id"]
        and str(row.get("instId") or row.get("instrument_id") or "").upper()
        == target["instrument_id"]
        and side == target["side"]
    )


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

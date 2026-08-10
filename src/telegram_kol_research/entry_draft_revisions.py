"""Immutable, risk-preserving revisions of approved entry order drafts."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Literal

from telegram_kol_research.deepcoin_order_builder import (
    deepcoin_order_draft_fingerprint,
)


UNKNOWN_LEG_OUTCOMES = frozenset(
    {"unknown", "submit_unknown", "unknown_exchange_outcome", "ambiguous"}
)


class EntryDraftRevisionError(ValueError):
    """A proposed operator revision violates original entry economics."""


def revise_entry_draft(
    original_draft: dict[str, object],
    *,
    operation: Literal["market_first_leg", "market_due_legs"],
    market_price: Decimal,
    authorized_leg_indices: tuple[int, ...],
) -> dict[str, object]:
    """Create a child draft while preserving every unauthorized leg and risk cap."""

    original = deepcopy(original_draft)
    legs = _legs(original)
    authorized = _authorized_indices(authorized_leg_indices, leg_count=len(legs))
    if operation not in {"market_first_leg", "market_due_legs"}:
        raise EntryDraftRevisionError("unsupported_revision_operation")
    if operation == "market_first_leg" and authorized != (1,):
        raise EntryDraftRevisionError("market_first_leg_requires_leg_1_only")
    _validate_deadline(original)
    _reject_unknown_outcomes(legs)
    normalized_market_price = _positive_decimal(
        market_price, error="invalid_market_price"
    )
    _validate_market_stop_side(
        position_side=original.get("position_side") or legs[0].get("position_side"),
        market_price=normalized_market_price,
        stop_loss=original.get("stop_loss"),
    )
    parent_fingerprint = deepcoin_order_draft_fingerprint(original)
    revised = deepcopy(original)
    revised_legs = _legs(revised)
    revision_seed = (
        f"{parent_fingerprint}:{operation}:{','.join(map(str, authorized))}:"
        f"{normalized_market_price}"
    )
    revision_id = hashlib.sha256(revision_seed.encode("utf-8")).hexdigest()
    for index in authorized:
        original_leg = legs[index - 1]
        changed = deepcopy(original_leg)
        changed["order_type"] = "market"
        changed["price"] = float(normalized_market_price)
        changed["client_order_id"] = _revision_client_order_id(
            revision_id=revision_id,
            leg_index=index,
        )
        changed["draft_revision_id"] = revision_id
        changed["parent_client_order_id"] = original_leg.get("client_order_id")
        _preserve_leg_risk_quantity(
            changed,
            original_leg=original_leg,
            market_price=normalized_market_price,
            stop_loss=original.get("stop_loss"),
            contract_spec=original.get("contract_spec"),
        )
        revised_legs[index - 1] = changed
    revised["order_legs"] = revised_legs
    revised["selected_entry_leg_indices"] = list(authorized)
    revised["selected_entry_leg_count"] = len(authorized)
    revised["revision_operation"] = operation
    revised["revision_id"] = revision_id
    revised["authorized_leg_indices"] = list(authorized)
    revised["parent_draft_fingerprint"] = parent_fingerprint
    revised["draft_fingerprint"] = deepcoin_order_draft_fingerprint(revised)
    validate_entry_draft_revision(
        original,
        revised,
        authorized_leg_indices=authorized,
    )
    return revised


def validate_entry_draft_revision(
    original_draft: dict[str, object],
    revised_draft: dict[str, object],
    *,
    authorized_leg_indices: tuple[int, ...],
) -> None:
    """Fail closed if a child draft changes identity or original economics."""

    original = deepcopy(original_draft)
    revised = deepcopy(revised_draft)
    original_legs = _legs(original)
    revised_legs = _legs(revised)
    authorized = _authorized_indices(
        authorized_leg_indices,
        leg_count=len(original_legs),
    )
    _validate_deadline(original)
    _reject_unknown_outcomes(original_legs)
    if len(revised_legs) != len(original_legs):
        raise EntryDraftRevisionError("leg_count_changed")
    if revised.get("stop_loss") != original.get("stop_loss"):
        raise EntryDraftRevisionError("stop_loss_changed")
    if revised.get("take_profit_legs") != original.get("take_profit_legs"):
        raise EntryDraftRevisionError("take_profit_changed")
    for field in (
        "venue",
        "strategy_instance_id",
        "instrument_id",
        "symbol",
        "position_side",
        "risk_budget_usdt",
    ):
        if revised.get(field) != original.get(field):
            raise EntryDraftRevisionError(f"{field}_changed")
    client_order_ids = [
        str(leg.get("client_order_id") or "") for leg in revised_legs
    ]
    if any(not value for value in client_order_ids) or len(set(client_order_ids)) != len(
        client_order_ids
    ):
        raise EntryDraftRevisionError("duplicate_client_order_id")
    original_budget = _decimal(original.get("risk_budget_usdt"))
    revised_risk = sum(
        (_decimal(leg.get("risk_budget_usdt")) for leg in revised_legs),
        Decimal("0"),
    )
    if revised_risk > original_budget:
        raise EntryDraftRevisionError("aggregate_risk_increased")
    for index, (before, after) in enumerate(
        zip(original_legs, revised_legs, strict=True),
        start=1,
    ):
        if index not in authorized and after != before:
            raise EntryDraftRevisionError("unauthorized_leg_changed")
        if _decimal(after.get("risk_budget_usdt")) != _decimal(
            before.get("risk_budget_usdt")
        ):
            raise EntryDraftRevisionError("leg_risk_budget_changed")
        for field in ("allocation_pct", "side", "position_side"):
            if after.get(field) != before.get(field):
                raise EntryDraftRevisionError(f"leg_{field}_changed")
    expected_parent = deepcoin_order_draft_fingerprint(original)
    if revised.get("parent_draft_fingerprint") != expected_parent:
        raise EntryDraftRevisionError("parent_draft_fingerprint_mismatch")


def _legs(draft: dict[str, object]) -> list[dict[str, object]]:
    rows = draft.get("order_legs")
    if not isinstance(rows, list) or not rows or any(
        not isinstance(row, dict) for row in rows
    ):
        raise EntryDraftRevisionError("invalid_order_legs")
    return rows


def _authorized_indices(values: tuple[int, ...], *, leg_count: int) -> tuple[int, ...]:
    if (
        not isinstance(values, tuple)
        or not values
        or any(type(value) is not int or value < 1 or value > leg_count for value in values)
        or len(set(values)) != len(values)
    ):
        raise EntryDraftRevisionError("invalid_authorized_leg_indices")
    return tuple(sorted(values))


def _reject_unknown_outcomes(legs: list[dict[str, object]]) -> None:
    for leg in legs:
        outcome = str(
            leg.get("execution_outcome") or leg.get("execution_status") or ""
        ).lower()
        if outcome in UNKNOWN_LEG_OUTCOMES:
            raise EntryDraftRevisionError("unknown_leg_outcome")


def _validate_deadline(draft: dict[str, object]) -> None:
    value = draft.get("execution_deadline_at") or draft.get("deadline_at")
    if value in {None, ""}:
        raise EntryDraftRevisionError("execution_deadline_missing")
    try:
        deadline = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except ValueError as exc:
        raise EntryDraftRevisionError("invalid_execution_deadline") from exc
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    if datetime.now(UTC) >= deadline.astimezone(UTC):
        raise EntryDraftRevisionError("execution_deadline_expired")


def _preserve_leg_risk_quantity(
    changed: dict[str, object],
    *,
    original_leg: dict[str, object],
    market_price: Decimal,
    stop_loss: object,
    contract_spec: object,
) -> None:
    changed["risk_budget_usdt"] = original_leg.get("risk_budget_usdt")
    changed["estimated_stop_loss_usdt"] = original_leg.get(
        "estimated_stop_loss_usdt",
        original_leg.get("risk_budget_usdt"),
    )
    old_price = _positive_decimal(original_leg.get("price"), error="invalid_leg_price")
    stop = _positive_decimal(stop_loss, error="invalid_stop_loss")
    old_distance = abs(old_price - stop)
    new_distance = abs(market_price - stop)
    old_quantity = _positive_decimal(
        original_leg.get("quantity"), error="invalid_leg_quantity"
    )
    if old_distance <= 0 or new_distance <= 0:
        raise EntryDraftRevisionError("invalid_market_stop_distance")
    quantity = old_quantity * old_distance / new_distance
    spec = contract_spec if isinstance(contract_spec, dict) else {}
    step = _positive_decimal(
        spec.get("quantity_step", "0.000001"),
        error="invalid_quantity_step",
    )
    quantity = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
    minimum = _positive_decimal(
        spec.get("min_quantity", step),
        error="invalid_minimum_quantity",
    )
    if quantity < minimum:
        raise EntryDraftRevisionError("revised_quantity_below_minimum")
    changed["quantity"] = float(quantity)
    original_estimated_risk = _decimal(
        original_leg.get(
            "estimated_stop_loss_usdt",
            original_leg.get("risk_budget_usdt"),
        )
    )
    risk_multiplier = original_estimated_risk / (old_distance * old_quantity)
    if original_leg.get("quantity_unit") == "contracts":
        risk_multiplier = _positive_decimal(
            spec.get("contract_value"),
            error="missing_contract_value",
        )
    estimated_risk = new_distance * quantity * risk_multiplier
    leg_budget = _decimal(original_leg.get("risk_budget_usdt"))
    if estimated_risk > leg_budget:
        raise EntryDraftRevisionError("revised_leg_risk_increased")
    changed["estimated_stop_loss_usdt"] = float(estimated_risk)


def _validate_market_stop_side(
    *,
    position_side: object,
    market_price: Decimal,
    stop_loss: object,
) -> None:
    side = str(position_side or "").lower()
    stop = _positive_decimal(stop_loss, error="invalid_stop_loss")
    if side == "long" and market_price <= stop:
        raise EntryDraftRevisionError("market_price_beyond_stop_loss")
    if side == "short" and market_price >= stop:
        raise EntryDraftRevisionError("market_price_beyond_stop_loss")
    if side not in {"long", "short"}:
        raise EntryDraftRevisionError("invalid_position_side")


def _revision_client_order_id(*, revision_id: str, leg_index: int) -> str:
    digest = revision_id[:15].upper()
    return f"R{int(leg_index)}{digest}"[:20]


def _decimal(value: object) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EntryDraftRevisionError("invalid_risk_budget") from exc
    if not normalized.is_finite() or normalized < 0:
        raise EntryDraftRevisionError("invalid_risk_budget")
    return normalized


def _positive_decimal(value: object, *, error: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EntryDraftRevisionError(error) from exc
    if not normalized.is_finite() or normalized <= 0:
        raise EntryDraftRevisionError(error)
    return normalized

"""Pure verified-position risk assessment for an entry revision."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN


class EntryRevisionRiskError(ValueError):
    """Raised when exact revision risk cannot be calculated safely."""


@dataclass(frozen=True, slots=True)
class EntryRevisionRiskDecision:
    action: str
    filled_risk_usdt: Decimal
    target_risk_usdt: Decimal
    remaining_risk_usdt: Decimal
    target_quantity: Decimal
    reduce_quantity: Decimal


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EntryRevisionRiskError("entry_revision_risk_input_invalid") from exc
    if not parsed.is_finite():
        raise EntryRevisionRiskError("entry_revision_risk_input_invalid")
    return parsed


def assess_revision_risk(
    *,
    quantity: object,
    average_entry: object,
    stop_loss: object,
    contract_value: object,
    side: str,
    target_risk_usdt: object,
    quantity_step: object,
) -> EntryRevisionRiskDecision:
    """Compare exact filled stop risk with the immutable target budget."""

    size = _decimal(quantity)
    average = _decimal(average_entry)
    stop = _decimal(stop_loss)
    contract = _decimal(contract_value)
    target = _decimal(target_risk_usdt)
    step = _decimal(quantity_step)
    normalized_side = str(side).lower()
    if (
        size <= 0
        or average <= 0
        or stop <= 0
        or contract <= 0
        or target <= 0
        or step <= 0
        or normalized_side not in {"long", "short"}
    ):
        raise EntryRevisionRiskError("entry_revision_risk_input_invalid")
    if (normalized_side == "long" and stop >= average) or (
        normalized_side == "short" and stop <= average
    ):
        raise EntryRevisionRiskError("entry_revision_stop_side_invalid")
    risk_per_quantity = abs(average - stop) * contract
    filled_risk = size * risk_per_quantity
    if filled_risk < target:
        return EntryRevisionRiskDecision(
            action="retain_and_use_headroom",
            filled_risk_usdt=filled_risk,
            target_risk_usdt=target,
            remaining_risk_usdt=target - filled_risk,
            target_quantity=size,
            reduce_quantity=Decimal("0"),
        )
    if filled_risk == target:
        return EntryRevisionRiskDecision(
            action="retain_at_target",
            filled_risk_usdt=filled_risk,
            target_risk_usdt=target,
            remaining_risk_usdt=Decimal("0"),
            target_quantity=size,
            reduce_quantity=Decimal("0"),
        )
    target_size = (target / risk_per_quantity / step).to_integral_value(
        rounding=ROUND_DOWN
    ) * step
    if target_size <= 0 or target_size >= size:
        raise EntryRevisionRiskError("entry_revision_target_size_not_executable")
    return EntryRevisionRiskDecision(
        action="reduce_to_target",
        filled_risk_usdt=filled_risk,
        target_risk_usdt=target,
        remaining_risk_usdt=Decimal("0"),
        target_quantity=target_size,
        reduce_quantity=size - target_size,
    )

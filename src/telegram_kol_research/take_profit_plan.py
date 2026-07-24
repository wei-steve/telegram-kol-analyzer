"""Pure validation and allocation for one through five take-profit stages."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
from decimal import ROUND_FLOOR
from typing import Iterable


class TakeProfitPlanError(ValueError):
    """Raised when a complete staged take-profit plan cannot be built safely."""


@dataclass(frozen=True, slots=True)
class TakeProfitLeg:
    price: str
    allocation_pct: str
    quantity: str | None = None


@dataclass(frozen=True, slots=True)
class TakeProfitPlan:
    legs: tuple[TakeProfitLeg, ...]


def build_take_profit_plan(
    *,
    prices: Iterable[object],
    side: str,
    configured_allocations: Iterable[object] | None,
    quantity: object | None = None,
    quantity_step: object | None = None,
    minimum_quantity: object | None = None,
) -> TakeProfitPlan:
    """Return a complete ordered TP plan, optionally with step-safe quantities."""

    normalized_side = _side(side)
    normalized_prices = _prices(prices)
    allocations = _allocations(configured_allocations, len(normalized_prices))
    quantities = _quantities(
        quantity=quantity,
        quantity_step=quantity_step,
        minimum_quantity=minimum_quantity,
        allocations=allocations,
    )
    ordered = sorted(normalized_prices, reverse=normalized_side == "short")
    return TakeProfitPlan(tuple(
        TakeProfitLeg(
            price=_text(price), allocation_pct=_text(allocation),
            quantity=_text(quantities[index]) if quantities is not None else None,
        )
        for index, (price, allocation) in enumerate(zip(ordered, allocations))
    ))


def _side(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in {"long", "short"}:
        raise TakeProfitPlanError("take-profit side must be long or short")
    return normalized


def _prices(values: Iterable[object]) -> tuple[Decimal, ...]:
    result = tuple(_positive(value, "take-profit price") for value in values)
    if not result or len(result) > 5:
        raise TakeProfitPlanError("take-profit plan must contain one through five targets")
    if len(set(result)) != len(result):
        raise TakeProfitPlanError("take-profit prices must be unique")
    return result


def _allocations(values: Iterable[object] | None, count: int) -> tuple[Decimal, ...]:
    raw_values = (
        values.replace("/", ",").replace("-", ",").split(",")
        if isinstance(values, str)
        else (values or ())
    )
    configured = tuple(_positive(value, "take-profit allocation") for value in raw_values)
    if len(configured) == count and sum(configured) == Decimal("100"):
        return configured
    if count == 1:
        return (Decimal("100"),)
    if count == 2:
        return (Decimal("50"), Decimal("50"))
    if count == 3:
        return (Decimal("40"), Decimal("30"), Decimal("30"))
    if count == 4:
        return (Decimal("40"), Decimal("20"), Decimal("20"), Decimal("20"))
    return (Decimal("40"), Decimal("15"), Decimal("15"), Decimal("15"), Decimal("15"))


def _quantities(*, quantity, quantity_step, minimum_quantity, allocations):
    supplied = (quantity, quantity_step, minimum_quantity)
    if all(value is None for value in supplied):
        return None
    if any(value is None for value in supplied):
        raise TakeProfitPlanError("quantity, step, and minimum quantity are required together")
    total = _positive(quantity, "position quantity")
    step = _positive(quantity_step, "quantity step")
    minimum = _positive(minimum_quantity, "minimum quantity")
    if (total / step) != (total / step).to_integral_value():
        raise TakeProfitPlanError("position quantity must align to quantity step")
    remaining = total
    result: list[Decimal] = []
    for index, allocation in enumerate(allocations):
        proposed = (
            remaining
            if index == len(allocations) - 1
            else _round_down(total * allocation / Decimal("100"), step)
        )
        if proposed < minimum:
            raise TakeProfitPlanError("take-profit stage is below minimum quantity")
        result.append(proposed)
        remaining -= proposed
    if remaining != 0 or sum(result) != total:
        raise TakeProfitPlanError("take-profit quantities do not sum to position quantity")
    return tuple(result)


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _positive(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TakeProfitPlanError(f"{label} must be positive") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise TakeProfitPlanError(f"{label} must be positive")
    return parsed


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")

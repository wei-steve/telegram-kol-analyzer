"""Pure two-round policy and contract-step sizing for management closes."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Iterable


class ManagementSizingError(ValueError):
    """Raised when a close cannot be represented safely for every position."""


def entry_revision_risk_reduction_delta(
    *,
    current_size: object,
    target_size: object,
    quantity_step: object,
    min_quantity: object,
) -> str:
    """Route an entry-revision reduction through the exact management delta rule."""

    return target_remaining_close_delta(
        trusted_start_size=current_size,
        target_remaining_size=target_size,
        current_size=current_size,
        quantity_step=quantity_step,
        min_quantity=min_quantity,
    )


def target_remaining_close_delta(
    *,
    trusted_start_size: object,
    target_remaining_size: object,
    current_size: object,
    quantity_step: object,
    min_quantity: object,
) -> str:
    """Return only the exact unresolved close delta for an immutable target."""

    try:
        trusted = Decimal(str(trusted_start_size))
        target = Decimal(str(target_remaining_size))
        current = Decimal(str(current_size))
        step = Decimal(str(quantity_step))
        minimum = Decimal(str(min_quantity))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ManagementSizingError("management_size_invalid") from exc
    if (
        any(not value.is_finite() for value in (trusted, target, current, step, minimum))
        or trusted <= 0
        or target < 0
        or target > trusted
        or current <= 0
        or step <= 0
        or minimum <= 0
    ):
        raise ManagementSizingError("management_size_invalid")
    if current > trusted:
        raise ManagementSizingError("position_size_increased_after_snapshot")
    if current < target:
        raise ManagementSizingError("position_below_target_remaining")
    delta = current - target
    if delta == 0:
        return "0"
    if delta < minimum or (delta / step) != (delta / step).to_integral_value():
        raise ManagementSizingError("target_remaining_delta_not_executable")
    return _format_decimal(delta)


def effective_action(
    *, round_before: int, fraction: float | None
) -> tuple[str, float]:
    """Resolve one partial instruction under the finite two-round policy."""

    if round_before == 0:
        return "partial_close", 0.5 if fraction is None else float(fraction)
    if round_before == 1:
        return "full_close", 1.0
    raise ManagementSizingError("partial_round_invalid")


def allocate_close_sizes(
    sizes: Iterable[object],
    *,
    fraction: object,
    quantity_step: object,
    min_quantity: object,
) -> tuple[str, ...]:
    """Allocate one aggregate close target across stable position order."""

    try:
        current = tuple(Decimal(str(value)) for value in sizes)
        close_fraction = Decimal(str(fraction))
        step = Decimal(str(quantity_step))
        minimum = Decimal(str(min_quantity))
    except (InvalidOperation, ValueError) as exc:
        raise ManagementSizingError("management_size_invalid") from exc

    if (
        not current
        or any(not value.is_finite() or value <= 0 for value in current)
        or not close_fraction.is_finite()
        or not Decimal("0") < close_fraction <= Decimal("1")
        or not step.is_finite()
        or step <= 0
        or not minimum.is_finite()
        or minimum <= 0
    ):
        raise ManagementSizingError("management_size_invalid")

    if close_fraction == 1 and any(
        (value / step) != (value / step).to_integral_value() for value in current
    ):
        raise ManagementSizingError("full_close_not_step_aligned")

    total = sum(current, Decimal("0"))
    aggregate_target = total * close_fraction
    target_steps = (aggregate_target / step).to_integral_value(
        rounding=ROUND_FLOOR
    )
    minimum_steps = (minimum / step).to_integral_value(rounding=ROUND_CEILING)
    capacity_steps = tuple(
        (value / step).to_integral_value(rounding=ROUND_FLOOR)
        for value in current
    )
    if (
        target_steps <= 0
        or target_steps < minimum_steps * len(current)
        or target_steps > sum(capacity_steps, Decimal("0"))
        or any(capacity < minimum_steps for capacity in capacity_steps)
    ):
        raise ManagementSizingError("aggregate_target_below_minimum")

    ideal_step_counts = [value * close_fraction / step for value in current]
    extra_capacities = tuple(
        capacity - minimum_steps for capacity in capacity_steps
    )
    deficit_weights = tuple(
        max(ideal - minimum_steps, Decimal("0"))
        for ideal in ideal_step_counts
    )
    exact_extras = _bulk_extra_step_quotas(
        target_steps - minimum_steps * len(current),
        capacities=extra_capacities,
        weights=deficit_weights,
    )
    planned_steps = [
        minimum_steps + extra.to_integral_value(rounding=ROUND_FLOOR)
        for extra in exact_extras
    ]
    remainder_steps = int(target_steps - sum(planned_steps, Decimal("0")))
    ranked_indexes = sorted(
        (
            index
            for index, capacity in enumerate(capacity_steps)
            if planned_steps[index] < capacity
        ),
        key=lambda index: (
            -(exact_extras[index] % 1),
            index,
        ),
    )
    if remainder_steps > len(ranked_indexes):
        raise ManagementSizingError("aggregate_target_not_allocatable")
    for index in ranked_indexes[:remainder_steps]:
        planned_steps[index] += 1

    planned = tuple(step_count * step for step_count in planned_steps)
    if (
        any(value < minimum for value in planned)
        or any(value > size for value, size in zip(planned, current))
        or sum(planned, Decimal("0")) > aggregate_target
    ):
        raise ManagementSizingError("position_close_size_unsafe")
    return tuple(_format_decimal(value) for value in planned)


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _bulk_extra_step_quotas(
    target: Decimal,
    *,
    capacities: tuple[Decimal, ...],
    weights: tuple[Decimal, ...],
) -> tuple[Decimal, ...]:
    """Apportion integer-step capacity in at most one pass per leg."""

    quotas = [Decimal("0") for _ in capacities]
    active = [index for index, capacity in enumerate(capacities) if capacity > 0]
    remaining = target
    for _ in range(len(capacities)):
        if remaining == 0:
            break
        if not active:
            raise ManagementSizingError("aggregate_target_not_allocatable")
        total_weight = sum((weights[index] for index in active), Decimal("0"))
        allocation_weights = weights if total_weight > 0 else capacities
        total_weight = sum(
            (allocation_weights[index] for index in active), Decimal("0")
        )
        tentative = {
            index: remaining * allocation_weights[index] / total_weight
            for index in active
        }
        saturated = [
            index
            for index in active
            if tentative[index] > capacities[index]
        ]
        if not saturated:
            for index in active:
                quotas[index] = tentative[index]
            remaining = Decimal("0")
            break
        for index in saturated:
            quotas[index] = capacities[index]
            remaining -= capacities[index]
        saturated_indexes = set(saturated)
        active = [index for index in active if index not in saturated_indexes]
    if remaining != 0:
        raise ManagementSizingError("aggregate_target_not_allocatable")
    return tuple(quotas)

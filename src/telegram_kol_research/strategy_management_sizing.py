"""Pure two-round policy and contract-step sizing for management closes."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Iterable


class ManagementSizingError(ValueError):
    """Raised when a close cannot be represented safely for every position."""


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
        or any(capacity < minimum_steps for capacity in capacity_steps)
    ):
        raise ManagementSizingError("aggregate_target_below_minimum")

    ideal_step_counts = [value * close_fraction / step for value in current]
    planned_steps = [minimum_steps for _ in current]
    remaining_steps = int(target_steps - sum(planned_steps, Decimal("0")))
    while remaining_steps:
        eligible = [
            index
            for index, capacity in enumerate(capacity_steps)
            if planned_steps[index] < capacity
        ]
        if not eligible:
            raise ManagementSizingError("aggregate_target_not_allocatable")
        index = max(
            eligible,
            key=lambda candidate: (
                ideal_step_counts[candidate] - planned_steps[candidate],
                -candidate,
            ),
        )
        planned_steps[index] += 1
        remaining_steps -= 1

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

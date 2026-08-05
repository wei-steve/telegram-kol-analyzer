"""Pure operation-specific authorization for exact-position management."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PositionManagementCapabilities:
    may_cancel_owned_protection: bool
    may_replace_owned_protection: bool
    may_add_exact_backup_stop: bool
    may_add_exact_take_profit: bool
    may_reduce_exact_position: bool
    may_close_exact_position: bool
    reason_codes: tuple[str, ...]


def evaluate_position_management_capabilities(
    *,
    exact_position_verified: bool,
    native_stop_owned: bool,
    exact_owned_stop: bool,
    conflicting_unknown_take_profit: bool,
    retained_take_profit_safe: bool,
    snapshot_complete: bool,
    active_or_unknown_mutation: bool = False,
) -> PositionManagementCapabilities:
    """Authorize each write independently without weakening ownership proof."""

    reasons: list[str] = []
    if not exact_position_verified:
        reasons.append("exact_position_not_verified")
    if not snapshot_complete:
        reasons.append("position_management_snapshot_incomplete")
    if active_or_unknown_mutation:
        reasons.append("position_mutation_unresolved")
    if not native_stop_owned:
        reasons.append("native_stop_not_owned")
    if not exact_owned_stop:
        reasons.append("exact_owned_stop_missing")
    if conflicting_unknown_take_profit:
        reasons.append("conflicting_unknown_take_profit")
    if not retained_take_profit_safe:
        reasons.append("retained_take_profit_overflow")

    base_write = (
        exact_position_verified
        and snapshot_complete
        and not active_or_unknown_mutation
    )
    additive_take_profit_safe = (
        base_write
        and exact_owned_stop
        and not conflicting_unknown_take_profit
        and retained_take_profit_safe
    )
    partial_reduction_safe = (
        base_write
        and not conflicting_unknown_take_profit
        and retained_take_profit_safe
    )
    return PositionManagementCapabilities(
        may_cancel_owned_protection=base_write and native_stop_owned,
        may_replace_owned_protection=base_write and native_stop_owned,
        may_add_exact_backup_stop=base_write and not exact_owned_stop,
        may_add_exact_take_profit=additive_take_profit_safe,
        may_reduce_exact_position=partial_reduction_safe,
        may_close_exact_position=base_write,
        reason_codes=tuple(reasons),
    )

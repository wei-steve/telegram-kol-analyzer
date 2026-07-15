"""Pure planning for evidence-backed historical position-attribution cleanup."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from telegram_kol_research.position_attribution import (
    TERMINAL_ENTRY_LEG_STATES,
    classify_leg_exchange_state,
    has_authoritative_persisted_position,
)


_CLOSE_EVENT_ACTIONS = frozenset(
    {
        "close_position_market",
        "close_bound_position",
        "close_bound_position_market",
        "manual_close_position",
    }
)
_SUCCESS_EVENT_STATES = frozenset({"completed", "success", "succeeded", "filled"})
_COMPLETED_RESERVATION_STATES = frozenset({"completed", "closed", "succeeded"})
_ACTION_ORDER = {
    "clear_redundant_historical_position": 10,
    "terminalize_historical_entry_leg": 20,
    "close_historical_binding": 30,
    "exit_historical_lifecycle": 40,
    "install_position_ownership_unique_index": 50,
}


@dataclass(frozen=True, slots=True)
class HistoricalCleanupAction:
    action: str
    binding_id: int | None
    leg_id: int | None
    lifecycle_id: int | None
    venue: str
    old_pos_id: str | None
    new_pos_id: str | None
    old_state: str | None
    new_state: str | None
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HistoricalCleanupConflict:
    reason: str
    binding_ids: tuple[int, ...]
    leg_ids: tuple[int, ...]
    lifecycle_ids: tuple[int, ...]
    pos_ids: tuple[str, ...]
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HistoricalCleanupDecision:
    actions: tuple[HistoricalCleanupAction, ...]
    conflicts: tuple[HistoricalCleanupConflict, ...]


def plan_historical_attribution_cleanup(
    *,
    bindings,
    legs,
    lifecycles,
    events,
    reservations,
    snapshot,
) -> HistoricalCleanupDecision:
    """Return stable evidence-backed historical actions without mutating rows."""

    if snapshot.errors:
        return HistoricalCleanupDecision(
            actions=(),
            conflicts=(
                HistoricalCleanupConflict(
                    reason="historical_evidence_unavailable",
                    binding_ids=(),
                    leg_ids=(),
                    lifecycle_ids=(),
                    pos_ids=(),
                    evidence={"errors": dict(sorted(snapshot.errors.items()))},
                ),
            ),
        )

    bindings_by_id = {int(row.id): row for row in bindings}
    legs_by_binding: dict[int, list[Any]] = defaultdict(list)
    legs_by_position: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for leg in legs:
        binding_id = int(leg.execution_binding_id)
        legs_by_binding[binding_id].append(leg)
        pos_id = _string(leg.pos_id)
        if pos_id:
            legs_by_position[(str(leg.venue or "deepcoin"), pos_id)].append(leg)

    lifecycles_by_binding: dict[int, list[Any]] = defaultdict(list)
    for lifecycle in lifecycles:
        if lifecycle.execution_binding_id is not None:
            lifecycles_by_binding[int(lifecycle.execution_binding_id)].append(lifecycle)
    events_by_binding: dict[int, list[Any]] = defaultdict(list)
    for event in events:
        if event.execution_binding_id is not None:
            events_by_binding[int(event.execution_binding_id)].append(event)
    reservations_by_binding: dict[int, list[Any]] = defaultdict(list)
    for reservation in reservations:
        reservations_by_binding[int(reservation.execution_binding_id)].append(reservation)

    exchange_active_ids = exchange_active_position_ids(snapshot)
    actions: list[HistoricalCleanupAction] = []
    conflicts: list[HistoricalCleanupConflict] = []
    terminalized_leg_ids: set[int] = set()

    for (venue, pos_id), component in sorted(legs_by_position.items()):
        component = sorted(component, key=lambda row: int(row.id))
        component_binding_ids = tuple(
            sorted({int(row.execution_binding_id) for row in component})
        )
        component_lifecycles = [
            lifecycle
            for binding_id in component_binding_ids
            for lifecycle in lifecycles_by_binding.get(binding_id, [])
        ]
        if pos_id in exchange_active_ids:
            conflicts.append(
                _conflict(
                    "historical_position_still_exchange_active",
                    component,
                    component_lifecycles,
                    pos_ids=(pos_id,),
                )
            )
            continue

        terminal_evidence = {
            binding_id: terminal_evidence_for_binding(
                bindings_by_id.get(binding_id),
                pos_id=pos_id,
                lifecycles=lifecycles_by_binding.get(binding_id, []),
                events=events_by_binding.get(binding_id, []),
                reservations=reservations_by_binding.get(binding_id, []),
                history_rows=[*snapshot.order_history, *snapshot.trigger_history],
                entry_order_ids=_entry_order_ids(legs_by_binding.get(binding_id, [])),
            )
            for binding_id in component_binding_ids
        }
        exact_owners = [
            leg for leg in component if has_authoritative_persisted_position(leg)
        ]
        if len(component) > 1:
            if len(exact_owners) != 1:
                conflicts.append(
                    _conflict(
                        "historical_owner_ambiguous",
                        component,
                        component_lifecycles,
                        pos_ids=(pos_id,),
                        evidence={"authoritative_leg_ids": [int(row.id) for row in exact_owners]},
                    )
                )
                continue
            owner = exact_owners[0]
            competing = [leg for leg in component if int(leg.id) != int(owner.id)]
            if any(
                terminal_evidence.get(int(leg.execution_binding_id)) is None
                for leg in competing
            ):
                conflicts.append(
                    _conflict(
                        "historical_terminal_evidence_missing",
                        component,
                        component_lifecycles,
                        pos_ids=(pos_id,),
                    )
                )
                continue
            for leg in competing:
                actions.append(
                    HistoricalCleanupAction(
                        action="clear_redundant_historical_position",
                        binding_id=int(leg.execution_binding_id),
                        leg_id=int(leg.id),
                        lifecycle_id=None,
                        venue=venue,
                        old_pos_id=pos_id,
                        new_pos_id=None,
                        old_state=str(leg.attribution_status or "unassigned"),
                        new_state="unassigned",
                        evidence={
                            "authoritative_leg_id": int(owner.id),
                            "terminal_evidence": terminal_evidence[
                                int(leg.execution_binding_id)
                            ],
                        },
                    )
                )

        for leg in component:
            if str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES:
                continue
            evidence = terminal_evidence.get(int(leg.execution_binding_id))
            if evidence is None:
                conflicts.append(
                    _conflict(
                        "historical_terminal_evidence_missing",
                        [leg],
                        lifecycles_by_binding.get(int(leg.execution_binding_id), []),
                        pos_ids=(pos_id,),
                    )
                )
                continue
            terminalized_leg_ids.add(int(leg.id))
            actions.append(
                HistoricalCleanupAction(
                    action="terminalize_historical_entry_leg",
                    binding_id=int(leg.execution_binding_id),
                    leg_id=int(leg.id),
                    lifecycle_id=None,
                    venue=venue,
                    old_pos_id=pos_id,
                    new_pos_id=pos_id,
                    old_state=str(leg.status),
                    new_state=_terminal_leg_state(evidence),
                    evidence={"terminal_evidence": evidence},
                )
            )

    affected_binding_ids = {
        int(action.binding_id)
        for action in actions
        if action.binding_id is not None
        and action.action in {
            "clear_redundant_historical_position",
            "terminalize_historical_entry_leg",
        }
    }
    for binding_id in sorted(affected_binding_ids):
        binding = bindings_by_id[binding_id]
        binding_legs = legs_by_binding.get(binding_id, [])
        all_terminal = bool(binding_legs) and all(
            int(leg.id) in terminalized_leg_ids
            or str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES
            for leg in binding_legs
        )
        if not all_terminal:
            continue
        if str(binding.status or "").lower() != "closed":
            actions.append(
                HistoricalCleanupAction(
                    action="close_historical_binding",
                    binding_id=binding_id,
                    leg_id=None,
                    lifecycle_id=None,
                    venue=str(binding.venue or "deepcoin"),
                    old_pos_id=_string(binding.pos_id),
                    new_pos_id=None,
                    old_state=str(binding.status),
                    new_state="closed",
                    evidence={"reason": "historical_entry_legs_terminal"},
                )
            )
        evidence = terminal_evidence_for_binding(
            binding,
            pos_id=None,
            lifecycles=lifecycles_by_binding.get(binding_id, []),
            events=events_by_binding.get(binding_id, []),
            reservations=reservations_by_binding.get(binding_id, []),
            history_rows=[*snapshot.order_history, *snapshot.trigger_history],
            entry_order_ids=_entry_order_ids(binding_legs),
        )
        for lifecycle in sorted(
            lifecycles_by_binding.get(binding_id, []), key=lambda row: int(row.id)
        ):
            if str(lifecycle.lifecycle_status or "").lower() != "entered":
                continue
            if evidence is None or evidence.get("source") == "lifecycle_terminal":
                continue
            actions.append(
                HistoricalCleanupAction(
                    action="exit_historical_lifecycle",
                    binding_id=binding_id,
                    leg_id=None,
                    lifecycle_id=int(lifecycle.id),
                    venue=str(binding.venue or "deepcoin"),
                    old_pos_id=None,
                    new_pos_id=None,
                    old_state=str(lifecycle.lifecycle_status),
                    new_state="exited",
                    evidence={"terminal_evidence": evidence},
                )
            )

    return HistoricalCleanupDecision(
        actions=tuple(sorted(_dedupe_actions(actions), key=_action_sort_key)),
        conflicts=tuple(sorted(_dedupe_conflicts(conflicts), key=_conflict_sort_key)),
    )


def terminal_evidence_for_binding(
    binding,
    *,
    pos_id: str | None,
    lifecycles,
    events,
    reservations,
    history_rows,
    entry_order_ids: set[str],
) -> dict[str, object] | None:
    """Return one exact terminal proof or None; position absence is never proof."""

    for lifecycle in sorted(lifecycles, key=lambda row: int(row.id), reverse=True):
        state = str(lifecycle.lifecycle_status or "").lower()
        terminal_at = lifecycle.exited_at
        if state in {"exited", "cancelled"} and lifecycle.exit_reason and terminal_at:
            return {
                "source": "lifecycle_terminal",
                "lifecycle_id": int(lifecycle.id),
                "state": state,
                "reason": str(lifecycle.exit_reason),
                "terminal_at": str(terminal_at),
            }
    for reservation in sorted(reservations, key=lambda row: int(row.id), reverse=True):
        if pos_id and _string(reservation.pos_id) != pos_id:
            continue
        if str(reservation.status or "").lower() in _COMPLETED_RESERVATION_STATES:
            return {
                "source": "close_reservation",
                "reservation_id": int(reservation.id),
                "pos_id": _string(reservation.pos_id),
                "status": str(reservation.status),
                "terminal_at": str(reservation.updated_at),
            }
    for event in sorted(events, key=lambda row: int(row.id), reverse=True):
        if pos_id and _string(event.pos_id) not in {None, pos_id}:
            continue
        if (
            str(event.action or "").lower() in _CLOSE_EVENT_ACTIONS
            and str(event.status or "").lower() in _SUCCESS_EVENT_STATES
        ):
            return {
                "source": "execution_close_event",
                "event_id": int(event.id),
                "action": str(event.action),
                "pos_id": _string(event.pos_id),
                "terminal_at": str(event.exchange_event_time or event.created_at),
            }
    binding_order_ids = entry_order_ids | {
        _string(item)
        for item in (
            getattr(binding, "order_id", None),
            getattr(binding, "client_order_id", None),
        )
        if _string(item)
    }
    for row in history_rows:
        row_ids = _row_strings(row, "ordId", "orderId", "clOrdId", "clientOrderId")
        if not binding_order_ids or not (binding_order_ids & row_ids):
            continue
        state = classify_leg_exchange_state(row)
        if state in {"manually_cancelled", "exchange_cancelled"}:
            return {
                "source": "exchange_cancel_history",
                "state": state,
                "order_id": next(iter(row_ids - {None}), None),
            }
    return None


def exchange_active_position_ids(snapshot) -> set[str]:
    """Return exact IDs from live positions and pending position-linked rows."""

    result: set[str] = set()
    for row in snapshot.positions:
        pos_id = _row_string(row, "posId", "pos_id", "id")
        if pos_id and _has_nonzero_size(row):
            result.add(pos_id)
    for row in [*snapshot.open_orders, *snapshot.pending_trigger_orders]:
        pos_id = _row_string(row, "posId", "pos_id", "positionId")
        if pos_id:
            result.add(pos_id)
    return result


def _terminal_leg_state(evidence: dict[str, object]) -> str:
    if evidence.get("source") == "exchange_cancel_history":
        return "exchange_cancelled"
    return "manually_closed"


def _conflict(
    reason: str,
    legs: Iterable[Any],
    lifecycles: Iterable[Any],
    *,
    pos_ids: tuple[str, ...],
    evidence: dict[str, object] | None = None,
) -> HistoricalCleanupConflict:
    leg_rows = list(legs)
    lifecycle_rows = list(lifecycles)
    return HistoricalCleanupConflict(
        reason=reason,
        binding_ids=tuple(
            sorted({int(row.execution_binding_id) for row in leg_rows})
        ),
        leg_ids=tuple(sorted(int(row.id) for row in leg_rows)),
        lifecycle_ids=tuple(sorted(int(row.id) for row in lifecycle_rows)),
        pos_ids=tuple(sorted(pos_ids)),
        evidence=dict(evidence or {}),
    )


def _dedupe_actions(actions: list[HistoricalCleanupAction]) -> list[HistoricalCleanupAction]:
    result: list[HistoricalCleanupAction] = []
    seen: set[tuple[str, int | None, int | None, int | None]] = set()
    for action in actions:
        identity = (
            action.action,
            action.binding_id,
            action.leg_id,
            action.lifecycle_id,
        )
        if identity not in seen:
            seen.add(identity)
            result.append(action)
    return result


def _dedupe_conflicts(
    conflicts: list[HistoricalCleanupConflict],
) -> list[HistoricalCleanupConflict]:
    result: list[HistoricalCleanupConflict] = []
    seen: set[tuple[str, tuple[int, ...], tuple[str, ...]]] = set()
    for conflict in conflicts:
        identity = (conflict.reason, conflict.leg_ids, conflict.pos_ids)
        if identity not in seen:
            seen.add(identity)
            result.append(conflict)
    return result


def _action_sort_key(action: HistoricalCleanupAction):
    return (
        int(action.binding_id or 0),
        _ACTION_ORDER.get(action.action, 999),
        int(action.leg_id or 0),
        int(action.lifecycle_id or 0),
    )


def _conflict_sort_key(conflict: HistoricalCleanupConflict):
    return (
        conflict.binding_ids,
        conflict.leg_ids,
        conflict.pos_ids,
        conflict.reason,
    )


def _has_nonzero_size(row: dict[str, Any]) -> bool:
    value = row.get("pos") or row.get("size") or row.get("sz")
    try:
        return float(value or 0) != 0
    except (TypeError, ValueError):
        return bool(value)


def _row_string(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _string(row.get(key))
        if value:
            return value
    return None


def _row_strings(row: dict[str, Any], *keys: str) -> set[str]:
    return {
        value
        for key in keys
        if (value := _string(row.get(key))) is not None
    }


def _entry_order_ids(legs: Iterable[Any]) -> set[str]:
    return {
        value
        for leg in legs
        for raw in (leg.order_id, leg.client_order_id)
        if (value := _string(raw)) is not None
    }


def _string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None

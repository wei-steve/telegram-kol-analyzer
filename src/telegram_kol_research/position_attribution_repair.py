"""Dry-run-first, audited repair of persisted Deepcoin position ownership."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from typing import Any

from sqlalchemy.exc import IntegrityError

from telegram_kol_research.db import (
    POSITION_OWNERSHIP_UNIQUE_INDEX_NAME,
    ensure_position_ownership_unique_index,
)
from telegram_kol_research.execution_bindings import (
    _exchange_row_matches_leg,
    _leg_has_successful_fill_evidence,
    _leg_evidence,
    _load_reconcile_snapshot,
    _post_entry_protection_mutated_binding_ids,
    build_position_evidence,
    _snapshot_fill_evidence,
)
from telegram_kol_research.historical_attribution_cleanup import (
    HistoricalCleanupAction,
    plan_historical_attribution_cleanup,
)
from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionAttributionAudit,
    StrategyLifecycle,
)
from telegram_kol_research.position_attribution import (
    TERMINAL_ENTRY_LEG_STATES,
    classify_leg_exchange_state,
    has_authoritative_persisted_position,
    match_entry_legs_to_positions,
)


class PositionAttributionRepairError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PositionAttributionRepairAction:
    action: str
    binding_id: int
    leg_id: int
    leg_index: int
    old_pos_id: str | None
    new_pos_id: str | None
    old_status: str
    new_status: str
    old_attribution_status: str
    new_attribution_status: str
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PositionAttributionRepairPlan:
    created_at: datetime
    live_position_ids: tuple[str, ...]
    exchange_evidence_fingerprint: str
    actions: tuple[PositionAttributionRepairAction, ...]
    unresolved_conflicts: list[dict[str, object]]
    database_fingerprint: str
    fingerprint: str
    historical_actions: tuple[HistoricalCleanupAction, ...] = ()

    @property
    def has_actions(self) -> bool:
        return bool(self.actions or self.historical_actions)


@dataclass(frozen=True, slots=True)
class PositionAttributionRepairResult:
    applied: int
    already_applied: bool = False


_HISTORICAL_TERMINAL_EVENT_ACTIONS = (
    "close_position_market",
    "close_bound_position",
    "close_bound_position_market",
    "manual_close_position",
)


def _load_local_repair_evidence(session):
    bindings = (
        session.query(ExecutionBinding)
        .filter(ExecutionBinding.venue == "deepcoin")
        .order_by(ExecutionBinding.id)
        .all()
    )
    legs = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.venue == "deepcoin")
        .order_by(ExecutionOrderLeg.id)
        .all()
    )
    binding_ids = [int(row.id) for row in bindings]
    if not binding_ids:
        return bindings, legs, [], [], []
    lifecycles = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.execution_binding_id.in_(binding_ids))
        .order_by(StrategyLifecycle.id)
        .all()
    )
    terminal_events = (
        session.query(ExecutionEvent)
        .filter(ExecutionEvent.execution_binding_id.in_(binding_ids))
        .filter(ExecutionEvent.action.in_(_HISTORICAL_TERMINAL_EVENT_ACTIONS))
        .order_by(ExecutionEvent.id)
        .all()
    )
    close_reservations = (
        session.query(BoundPositionCloseReservation)
        .filter(BoundPositionCloseReservation.execution_binding_id.in_(binding_ids))
        .order_by(BoundPositionCloseReservation.id)
        .all()
    )
    return bindings, legs, lifecycles, terminal_events, close_reservations


def build_position_attribution_repair_plan(
    session_factory,
    *,
    deepcoin_client,
    now: datetime | None = None,
) -> PositionAttributionRepairPlan:
    created_at = now or datetime.now(UTC)
    with session_factory() as session:
        bindings, legs, lifecycles, terminal_events, close_reservations = (
            _load_local_repair_evidence(session)
        )
        database_fingerprint = _database_fingerprint(
            bindings,
            legs,
            lifecycles=lifecycles,
            terminal_events=terminal_events,
            close_reservations=close_reservations,
        )
        bindings_by_id = {int(row.id): row for row in bindings}
        instruments = {
            f"{str(binding.symbol).upper()}-USDT-SWAP" for binding in bindings
        }
        snapshot = _load_reconcile_snapshot(
            deepcoin_client,
            instruments=instruments,
        )
        if snapshot.errors:
            unresolved = [{"evidence_source_errors": dict(sorted(snapshot.errors.items()))}]
            return _build_plan(
                created_at=created_at,
                live_position_ids=_live_position_ids(snapshot.positions),
                exchange_evidence_fingerprint=_exchange_evidence_fingerprint(snapshot),
                actions=(),
                historical_actions=(),
                unresolved_conflicts=unresolved,
                database_fingerprint=database_fingerprint,
            )

        position_rows = [
            row
            for raw in snapshot.positions
            if (row := build_position_evidence(raw))
        ]
        fill_rows = _snapshot_fill_evidence(
            snapshot,
            legs=legs,
            bindings_by_id=bindings_by_id,
        )
        mutated_binding_ids = _post_entry_protection_mutated_binding_ids(
            session, binding_ids=set(bindings_by_id)
        )
        leg_rows = [
            _leg_evidence(
                leg,
                binding=bindings_by_id[int(leg.execution_binding_id)],
                has_successful_entry_evidence=_leg_has_successful_fill_evidence(
                    leg, fill_rows, legs=legs
                ),
                protection_mutated=(
                    int(leg.execution_binding_id) in mutated_binding_ids
                ),
            )
            for leg in legs
        ]
        attribution = match_entry_legs_to_positions(
            leg_rows,
            position_rows,
            fill_rows,
            allow_equivalent_permutation=True,
        )
        conflict_leg_ids = {
            int(leg_id)
            for conflict in attribution.conflicts
            for leg_id in conflict.get("leg_ids", [])
        }
        legs_by_id = {int(leg.id): leg for leg in legs}
        clear_actions: list[PositionAttributionRepairAction] = []
        terminal_actions: list[PositionAttributionRepairAction] = []
        assign_actions: list[PositionAttributionRepairAction] = []

        assigned_leg_ids = {int(leg_id) for leg_id in attribution.assignments}
        for leg in legs:
            if (
                str(leg.attribution_status or "") == "verified"
                and leg.pos_id
                and not has_authoritative_persisted_position(leg)
                and int(leg.id) not in assigned_leg_ids
            ):
                clear_actions.append(
                    _action(
                        "clear_untrusted_verified_position",
                        leg,
                        new_pos_id=None,
                        new_status=str(leg.status),
                        new_attribution_status="unassigned",
                        evidence={"reason": "legacy_weak_verified_evidence"},
                    )
                )

        for target_leg_id, pos_id in sorted(attribution.assignments.items()):
            if int(target_leg_id) in conflict_leg_ids:
                continue
            target_leg = legs_by_id[int(target_leg_id)]
            existing_owners = [
                leg
                for leg in legs
                if leg.pos_id == pos_id and int(leg.id) != int(target_leg_id)
            ]
            if any(
                str(leg.attribution_status or "") == "verified"
                and has_authoritative_persisted_position(leg)
                for leg in existing_owners
            ):
                continue
            for old_leg in existing_owners:
                clear_actions.append(
                    _action(
                        "clear_stale_position",
                        old_leg,
                        new_pos_id=None,
                        new_status=str(old_leg.status),
                        new_attribution_status="unassigned",
                        evidence={"replacement_leg_id": int(target_leg_id)},
                    )
                )
            if (
                target_leg.pos_id != pos_id
                or str(target_leg.attribution_status) != "verified"
                or not has_authoritative_persisted_position(target_leg)
            ):
                assign_actions.append(
                    _action(
                        "assign_verified_position",
                        target_leg,
                        new_pos_id=pos_id,
                        new_status="active",
                        new_attribution_status="verified",
                        evidence=attribution.evidence_by_leg.get(int(target_leg_id), {}),
                    )
                )

        pending_rows = [*snapshot.open_orders, *snapshot.pending_trigger_orders]
        history_rows = [*snapshot.order_history, *snapshot.trigger_history]
        for leg in legs:
            if str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES:
                continue
            if any(_exchange_row_matches_leg(row, leg) for row in pending_rows):
                continue
            matched_history = [
                row for row in history_rows if _exchange_row_matches_leg(row, leg)
            ]
            if not any(
                classify_leg_exchange_state(row) in {"manually_cancelled", "exchange_cancelled"}
                for row in matched_history
            ):
                continue
            terminal_actions.append(
                _action(
                    "terminal_cancelled_leg",
                    leg,
                    new_pos_id=None,
                    new_status="manually_cancelled",
                    new_attribution_status="unassigned",
                    evidence={"order_state": "cancelled"},
                )
            )

        terminal_leg_ids = {action.leg_id for action in terminal_actions}
        assign_actions = [
            action for action in assign_actions if action.leg_id not in terminal_leg_ids
        ]
        actions = _dedupe_actions([*clear_actions, *terminal_actions, *assign_actions])
        historical = plan_historical_attribution_cleanup(
            bindings=bindings,
            legs=legs,
            lifecycles=lifecycles,
            events=terminal_events,
            reservations=close_reservations,
            snapshot=snapshot,
        )
        historical_actions = [*historical.actions]
        index_action = _plan_unique_position_index_action(
            session,
            legs=legs,
            historical_actions=historical_actions,
            historical_conflicts=historical.conflicts,
        )
        if index_action is not None:
            historical_actions.append(index_action)
        return _build_plan(
            created_at=created_at,
            live_position_ids=_live_position_ids(snapshot.positions),
            exchange_evidence_fingerprint=_exchange_evidence_fingerprint(snapshot),
            actions=tuple(actions),
            historical_actions=tuple(historical_actions),
            unresolved_conflicts=[
                *list(attribution.conflicts),
                *(
                    asdict(conflict)
                    for conflict in historical.conflicts
                    if conflict.reason
                    != "historical_position_still_exchange_active"
                ),
            ],
            database_fingerprint=database_fingerprint,
        )


def apply_position_attribution_repair_plan(
    session_factory,
    plan: PositionAttributionRepairPlan,
    *,
    deepcoin_client=None,
    expected_fingerprint: str | None = None,
) -> PositionAttributionRepairResult:
    if plan.unresolved_conflicts:
        raise PositionAttributionRepairError(
            "repair plan contains unresolved attribution conflicts"
        )
    if expected_fingerprint is not None and expected_fingerprint != plan.fingerprint:
        raise PositionAttributionRepairError(
            "reviewed fingerprint does not match the repair plan"
        )
    if plan.has_actions and expected_fingerprint is None:
        raise PositionAttributionRepairError(
            "reviewed fingerprint is required for a nonempty repair plan"
        )
    if plan.has_actions and deepcoin_client is None:
        raise PositionAttributionRepairError(
            "Deepcoin client is required for a nonempty repair plan"
        )
    all_actions = [*plan.actions, *plan.historical_actions]
    with session_factory() as session:
        applied_audits = {
            row.fingerprint: row
            for row in session.query(PositionAttributionAudit)
            .filter(
                PositionAttributionAudit.event_type.in_(
                    ["historical_repair", "historical_cleanup"]
                )
            )
            .all()
        }
    expected_audit_fingerprints = {
        _repair_audit_fingerprint(plan, action) for action in all_actions
    }
    already_applied = bool(all_actions) and expected_audit_fingerprints.issubset(
        applied_audits
    )

    if deepcoin_client is not None:
        current_plan = build_position_attribution_repair_plan(
            session_factory,
            deepcoin_client=deepcoin_client,
            now=plan.created_at,
        )
        if any(
            "evidence_source_errors" in conflict
            for conflict in current_plan.unresolved_conflicts
        ):
            raise PositionAttributionRepairError(
                "current exchange evidence unavailable"
            )
        if current_plan.live_position_ids != plan.live_position_ids:
            raise PositionAttributionRepairError(
                "live positions changed since repair plan"
            )
        if (
            current_plan.exchange_evidence_fingerprint
            != plan.exchange_evidence_fingerprint
        ):
            raise PositionAttributionRepairError(
                "stale repair plan: exchange evidence changed"
            )
        if not already_applied and current_plan.fingerprint != plan.fingerprint:
            raise PositionAttributionRepairError(
                "stale repair plan: exchange or database evidence changed"
            )

    with session_factory() as session:
        bindings, legs, lifecycles, terminal_events, close_reservations = (
            _load_local_repair_evidence(session)
        )
        current_database_fingerprint = _database_fingerprint(
            bindings,
            legs,
            lifecycles=lifecycles,
            terminal_events=terminal_events,
            close_reservations=close_reservations,
        )
        if already_applied:
            applied_state_fingerprints = {
                _repair_audit_applied_database_fingerprint(
                    applied_audits[fingerprint]
                )
                for fingerprint in expected_audit_fingerprints
            }
            if (
                None in applied_state_fingerprints
                or len(applied_state_fingerprints) != 1
                or current_database_fingerprint
                not in applied_state_fingerprints
            ):
                raise PositionAttributionRepairError(
                    "stale repair plan: database evidence changed"
                )
            return PositionAttributionRepairResult(
                applied=0, already_applied=True
            )
        if current_database_fingerprint != plan.database_fingerprint:
            raise PositionAttributionRepairError("stale repair plan: database evidence changed")
        legs_by_id = {int(row.id): row for row in legs}
        bindings_by_id = {int(row.id): row for row in bindings}
        lifecycles_by_id = {int(row.id): row for row in lifecycles}
        try:
            for action in plan.actions:
                leg = legs_by_id.get(action.leg_id)
                if leg is None:
                    raise PositionAttributionRepairError("stale repair plan: leg missing")
                leg.pos_id = action.new_pos_id
                leg.status = action.new_status
                leg.attribution_status = action.new_attribution_status
                leg.attribution_evidence_json = json.dumps(
                    {
                        "evidence_type": "historical_repair",
                        "repair_plan_fingerprint": plan.fingerprint,
                        **action.evidence,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if action.new_attribution_status == "verified":
                    leg.last_verified_at = plan.created_at
                if action.action == "terminal_cancelled_leg":
                    leg.terminal_reason = "historical_exchange_cancelled"
            _derive_repaired_bindings(
                bindings,
                list(legs_by_id.values()),
                plan.created_at,
                affected_binding_ids={action.binding_id for action in plan.actions},
                live_position_ids=set(plan.live_position_ids),
            )
            for action in plan.historical_actions:
                _apply_historical_cleanup_action(
                    session,
                    action=action,
                    plan=plan,
                    legs_by_id=legs_by_id,
                    bindings_by_id=bindings_by_id,
                    lifecycles_by_id=lifecycles_by_id,
                )
            session.flush()
            applied_database_fingerprint = _database_fingerprint(
                bindings,
                legs,
                lifecycles=lifecycles,
                terminal_events=terminal_events,
                close_reservations=close_reservations,
            )
            for action in plan.actions:
                _insert_repair_audit(
                    session,
                    plan=plan,
                    action=action,
                    leg=legs_by_id[action.leg_id],
                    applied_database_fingerprint=applied_database_fingerprint,
                )
            for action in plan.historical_actions:
                _insert_historical_cleanup_audit(
                    session,
                    plan=plan,
                    action=action,
                    applied_database_fingerprint=applied_database_fingerprint,
                )
            session.commit()
        except PositionAttributionRepairError:
            session.rollback()
            raise
        except (IntegrityError, RuntimeError) as exc:
            session.rollback()
            raise PositionAttributionRepairError("repair transaction failed") from exc
        except Exception:
            session.rollback()
            raise
    return PositionAttributionRepairResult(applied=len(all_actions))


def _action(
    action: str,
    leg: ExecutionOrderLeg,
    *,
    new_pos_id: str | None,
    new_status: str,
    new_attribution_status: str,
    evidence: dict[str, object],
) -> PositionAttributionRepairAction:
    return PositionAttributionRepairAction(
        action=action,
        binding_id=int(leg.execution_binding_id),
        leg_id=int(leg.id),
        leg_index=int(leg.leg_index),
        old_pos_id=leg.pos_id,
        new_pos_id=new_pos_id,
        old_status=str(leg.status),
        new_status=new_status,
        old_attribution_status=str(leg.attribution_status or "unassigned"),
        new_attribution_status=new_attribution_status,
        evidence=dict(evidence),
    )


def _plan_unique_position_index_action(
    session,
    *,
    legs,
    historical_actions,
    historical_conflicts,
) -> HistoricalCleanupAction | None:
    index_names = {
        str(row[1])
        for row in session.connection()
        .exec_driver_sql("PRAGMA index_list(execution_order_legs)")
        .fetchall()
    }
    if POSITION_OWNERSHIP_UNIQUE_INDEX_NAME in index_names or historical_conflicts:
        return None
    projected = {
        int(leg.id): (str(leg.venue or "deepcoin"), str(leg.pos_id))
        for leg in legs
        if leg.pos_id
    }
    for action in historical_actions:
        if action.leg_id is None:
            continue
        if action.action == "clear_redundant_historical_position":
            projected.pop(int(action.leg_id), None)
    owners: dict[tuple[str, str], int] = {}
    for key in projected.values():
        owners[key] = owners.get(key, 0) + 1
    if any(count > 1 for count in owners.values()):
        return None
    return HistoricalCleanupAction(
        action="install_position_ownership_unique_index",
        binding_id=None,
        leg_id=None,
        lifecycle_id=None,
        venue="deepcoin",
        old_pos_id=None,
        new_pos_id=None,
        old_state="absent",
        new_state="present",
        evidence={"index_name": POSITION_OWNERSHIP_UNIQUE_INDEX_NAME},
    )


def _dedupe_actions(
    actions: list[PositionAttributionRepairAction],
) -> list[PositionAttributionRepairAction]:
    result: list[PositionAttributionRepairAction] = []
    seen: set[tuple[str, int]] = set()
    for action in actions:
        identity = (action.action, action.leg_id)
        if identity not in seen:
            result.append(action)
            seen.add(identity)
    return result


def _build_plan(
    *,
    created_at: datetime,
    live_position_ids: tuple[str, ...],
    exchange_evidence_fingerprint: str,
    actions: tuple[PositionAttributionRepairAction, ...],
    historical_actions: tuple[HistoricalCleanupAction, ...],
    unresolved_conflicts: list[dict[str, object]],
    database_fingerprint: str,
) -> PositionAttributionRepairPlan:
    payload = {
        "live_position_ids": live_position_ids,
        "exchange_evidence_fingerprint": exchange_evidence_fingerprint,
        "actions": [asdict(action) for action in actions],
        "historical_actions": [asdict(action) for action in historical_actions],
        "unresolved_conflicts": unresolved_conflicts,
        "database_fingerprint": database_fingerprint,
    }
    return PositionAttributionRepairPlan(
        created_at=created_at,
        live_position_ids=live_position_ids,
        exchange_evidence_fingerprint=exchange_evidence_fingerprint,
        actions=actions,
        historical_actions=historical_actions,
        unresolved_conflicts=unresolved_conflicts,
        database_fingerprint=database_fingerprint,
        fingerprint=_hash(payload),
    )


def _database_fingerprint(
    bindings,
    legs,
    *,
    lifecycles=(),
    terminal_events=(),
    close_reservations=(),
) -> str:
    return _hash(
        {
            "bindings": [
                {
                    "id": int(row.id),
                    "strategy_instance_id": row.strategy_instance_id,
                    "chat_id": int(row.chat_id),
                    "message_id": int(row.message_id),
                    "symbol": row.symbol,
                    "side": row.side,
                    "margin_mode": row.margin_mode,
                    "position_mode": row.position_mode,
                    "payload_json": row.payload_json,
                    "pos_id": row.pos_id,
                    "status": row.status,
                    "last_exchange_status": row.last_exchange_status,
                }
                for row in bindings
            ],
            "legs": [
                {
                    "id": int(row.id),
                    "binding_id": int(row.execution_binding_id),
                    "strategy_instance_id": row.strategy_instance_id,
                    "leg_index": int(row.leg_index),
                    "purpose": row.purpose,
                    "order_kind": row.order_kind,
                    "venue": row.venue,
                    "order_id": row.order_id,
                    "client_order_id": row.client_order_id,
                    "pos_id": row.pos_id,
                    "status": row.status,
                    "attribution_status": row.attribution_status,
                    "attribution_evidence_json": row.attribution_evidence_json,
                    "request_json": row.request_json,
                    "response_json": row.response_json,
                    "terminal_reason": row.terminal_reason,
                    "last_verified_at": _fingerprint_datetime(row.last_verified_at),
                }
                for row in legs
            ],
            "lifecycles": [
                {
                    "id": int(row.id),
                    "execution_binding_id": (
                        int(row.execution_binding_id)
                        if row.execution_binding_id is not None
                        else None
                    ),
                    "lifecycle_status": row.lifecycle_status,
                    "exit_reason": row.exit_reason,
                    "entered_at": _fingerprint_datetime(row.entered_at),
                    "exited_at": _fingerprint_datetime(row.exited_at),
                    "updated_at": _fingerprint_datetime(row.updated_at),
                }
                for row in lifecycles
            ],
            "terminal_events": [
                {
                    "id": int(row.id),
                    "execution_binding_id": (
                        int(row.execution_binding_id)
                        if row.execution_binding_id is not None
                        else None
                    ),
                    "action": row.action,
                    "status": row.status,
                    "pos_id": row.pos_id,
                    "related_order_id": row.related_order_id,
                    "exchange_event_time": _fingerprint_datetime(
                        row.exchange_event_time
                    ),
                    "created_at": _fingerprint_datetime(row.created_at),
                }
                for row in terminal_events
            ],
            "close_reservations": [
                {
                    "id": int(row.id),
                    "pos_id": row.pos_id,
                    "execution_binding_id": int(row.execution_binding_id),
                    "status": row.status,
                    "created_at": _fingerprint_datetime(row.created_at),
                    "updated_at": _fingerprint_datetime(row.updated_at),
                }
                for row in close_reservations
            ],
        }
    )


def _fingerprint_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _live_position_ids(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(row.get("posId") or row.get("pos_id") or row.get("id"))
            for row in rows
            if row.get("posId") or row.get("pos_id") or row.get("id")
        )
    )


def _exchange_evidence_fingerprint(snapshot) -> str:
    def stable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            rows,
            key=lambda row: json.dumps(
                row,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )

    return _hash(
        {
            "positions": stable_rows(snapshot.positions),
            "open_orders": stable_rows(snapshot.open_orders),
            "pending_trigger_orders": stable_rows(snapshot.pending_trigger_orders),
            "order_history": stable_rows(snapshot.order_history),
            "trade_fills": stable_rows(snapshot.trade_fills),
            "trigger_history": stable_rows(snapshot.trigger_history),
            "errors": dict(sorted(snapshot.errors.items())),
        }
    )


def _apply_historical_cleanup_action(
    session,
    *,
    action: HistoricalCleanupAction,
    plan: PositionAttributionRepairPlan,
    legs_by_id,
    bindings_by_id,
    lifecycles_by_id,
) -> None:
    updated_at = _naive_utc(plan.created_at)
    if action.action == "clear_redundant_historical_position":
        leg = legs_by_id.get(int(action.leg_id or 0))
        if leg is None:
            raise PositionAttributionRepairError("stale repair plan: leg missing")
        if (
            leg.pos_id != action.old_pos_id
            or str(leg.attribution_status or "unassigned") != action.old_state
        ):
            raise PositionAttributionRepairError(
                "stale repair plan: historical leg changed"
            )
        leg.pos_id = action.new_pos_id
        leg.attribution_status = str(action.new_state or "unassigned")
        leg.attribution_evidence_json = json.dumps(
            {
                "evidence_type": "historical_cleanup",
                "repair_plan_fingerprint": plan.fingerprint,
                "prior_pos_id": action.old_pos_id,
                **action.evidence,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        leg.updated_at = updated_at
        return
    if action.action == "terminalize_historical_entry_leg":
        leg = legs_by_id.get(int(action.leg_id or 0))
        if leg is None:
            raise PositionAttributionRepairError("stale repair plan: leg missing")
        if leg.pos_id != action.old_pos_id or str(leg.status) != action.old_state:
            raise PositionAttributionRepairError(
                "stale repair plan: historical leg changed"
            )
        leg.status = str(action.new_state)
        source = str(
            (action.evidence.get("terminal_evidence") or {}).get("source")
            or "terminal_evidence"
        )
        leg.terminal_reason = f"historical_{source}"[:64]
        leg.updated_at = updated_at
        return
    if action.action == "close_historical_binding":
        binding = bindings_by_id.get(int(action.binding_id or 0))
        if binding is None:
            raise PositionAttributionRepairError("stale repair plan: binding missing")
        if binding.pos_id != action.old_pos_id or str(binding.status) != action.old_state:
            raise PositionAttributionRepairError(
                "stale repair plan: historical binding changed"
            )
        binding.pos_id = action.new_pos_id
        binding.status = str(action.new_state)
        binding.last_exchange_status = "historical_cleanup_terminal"
        binding.updated_at = updated_at
        return
    if action.action == "exit_historical_lifecycle":
        lifecycle = lifecycles_by_id.get(int(action.lifecycle_id or 0))
        if lifecycle is None:
            raise PositionAttributionRepairError("stale repair plan: lifecycle missing")
        if str(lifecycle.lifecycle_status) != action.old_state:
            raise PositionAttributionRepairError(
                "stale repair plan: historical lifecycle changed"
            )
        lifecycle.lifecycle_status = str(action.new_state)
        lifecycle.exit_reason = _historical_exit_reason(action.evidence)
        lifecycle.exited_at = updated_at
        lifecycle.updated_at = updated_at
        return
    if action.action == "install_position_ownership_unique_index":
        session.flush()
        ensure_position_ownership_unique_index(session.connection())
        return
    raise PositionAttributionRepairError(
        f"unsupported historical cleanup action: {action.action}"
    )


def _historical_exit_reason(evidence: dict[str, object]) -> str:
    terminal = evidence.get("terminal_evidence") or {}
    if isinstance(terminal, dict):
        source = str(terminal.get("source") or "")
        if source in {"close_reservation", "execution_close_event"}:
            return "manual"
        reason = str(terminal.get("reason") or "")
        if reason in {
            "manual",
            "stop_loss",
            "take_profit",
            "kol_signal",
            "expired",
            "context_invalidated",
        }:
            return reason
    return "manual"


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _insert_repair_audit(
    session, *, plan, action, leg, applied_database_fingerprint: str
) -> None:
    fingerprint = _repair_audit_fingerprint(plan, action)
    if session.query(PositionAttributionAudit.id).filter_by(fingerprint=fingerprint).first():
        return
    session.add(
        PositionAttributionAudit(
            execution_binding_id=action.binding_id,
            execution_order_leg_id=action.leg_id,
            venue="deepcoin",
            pos_id=action.new_pos_id or action.old_pos_id,
            event_type="historical_repair",
            prior_state=action.old_attribution_status,
            new_state=action.new_attribution_status,
            fingerprint=fingerprint,
            evidence_json=json.dumps(
                {
                    "plan": plan.fingerprint,
                    "applied_database_fingerprint": applied_database_fingerprint,
                    **action.evidence,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            created_at=plan.created_at,
        )
    )


def _insert_historical_cleanup_audit(
    session,
    *,
    plan: PositionAttributionRepairPlan,
    action: HistoricalCleanupAction,
    applied_database_fingerprint: str,
) -> None:
    fingerprint = _repair_audit_fingerprint(plan, action)
    if session.query(PositionAttributionAudit.id).filter_by(fingerprint=fingerprint).first():
        return
    session.add(
        PositionAttributionAudit(
            execution_binding_id=action.binding_id,
            execution_order_leg_id=action.leg_id,
            venue=action.venue,
            pos_id=action.new_pos_id or action.old_pos_id,
            event_type="historical_cleanup",
            prior_state=action.old_state,
            new_state=str(action.new_state or action.action),
            fingerprint=fingerprint,
            evidence_json=json.dumps(
                {
                    "action": action.action,
                    "plan": plan.fingerprint,
                    "lifecycle_id": action.lifecycle_id,
                    "old_pos_id": action.old_pos_id,
                    "new_pos_id": action.new_pos_id,
                    "applied_database_fingerprint": applied_database_fingerprint,
                    **action.evidence,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            created_at=_naive_utc(plan.created_at),
        )
    )


def _repair_audit_applied_database_fingerprint(audit) -> str | None:
    try:
        evidence = json.loads(audit.evidence_json or "{}")
    except (TypeError, ValueError):
        return None
    value = evidence.get("applied_database_fingerprint")
    return str(value) if value else None


def _repair_audit_fingerprint(plan, action) -> str:
    return _hash({"plan": plan.fingerprint, "action": asdict(action)})


def _derive_repaired_bindings(
    bindings,
    legs,
    updated_at,
    *,
    affected_binding_ids: set[int],
    live_position_ids: set[str],
) -> None:
    for binding in bindings:
        if int(binding.id) not in affected_binding_ids:
            continue
        binding_legs = [
            leg for leg in legs if int(leg.execution_binding_id) == int(binding.id)
        ]
        binding_is_terminal = str(binding.status or "").lower() in {
            "closed",
            "cancelled",
            "expired",
            "invalidated",
        } or str(binding.last_exchange_status or "").lower().startswith(
            "manual_closed"
        )
        verified = [
            str(leg.pos_id)
            for leg in sorted(binding_legs, key=lambda row: row.leg_index)
            if (
                leg.pos_id
                and str(leg.pos_id) in live_position_ids
                and str(leg.attribution_status) == "verified"
                and str(leg.status or "").lower() not in TERMINAL_ENTRY_LEG_STATES
            )
        ]
        if binding_is_terminal:
            binding.pos_id = None
            binding.status = "closed"
        elif verified:
            binding.pos_id = ",".join(dict.fromkeys(verified))
            binding.status = "active"
            binding.last_exchange_status = "position_ownership_verified_by_repair"
        elif binding_legs and all(
            str(leg.status).lower() in TERMINAL_ENTRY_LEG_STATES for leg in binding_legs
        ):
            binding.pos_id = None
            binding.status = "closed"
            binding.last_exchange_status = "entry_legs_terminal_by_repair"
        else:
            binding.pos_id = None
            binding.status = "open" if any(
                str(leg.status).lower() in {"open", "pending", "submitted"}
                for leg in binding_legs
            ) else "stale"
            binding.last_exchange_status = "position_ownership_unassigned_by_repair"
        binding.updated_at = updated_at


def _hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()

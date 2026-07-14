"""Dry-run-first, audited repair of persisted Deepcoin position ownership."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from typing import Any

from sqlalchemy.exc import IntegrityError

from telegram_kol_research.execution_bindings import (
    _exchange_row_matches_leg,
    _leg_evidence,
    _load_reconcile_snapshot,
    _position_evidence,
    _snapshot_fill_evidence,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionAttributionAudit,
)
from telegram_kol_research.position_attribution import (
    TERMINAL_ENTRY_LEG_STATES,
    classify_leg_exchange_state,
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
    actions: tuple[PositionAttributionRepairAction, ...]
    unresolved_conflicts: list[dict[str, object]]
    database_fingerprint: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PositionAttributionRepairResult:
    applied: int
    already_applied: bool = False


def build_position_attribution_repair_plan(
    session_factory,
    *,
    deepcoin_client,
    now: datetime | None = None,
) -> PositionAttributionRepairPlan:
    created_at = now or datetime.now(UTC)
    with session_factory() as session:
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
        database_fingerprint = _database_fingerprint(bindings, legs)
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
                actions=(),
                unresolved_conflicts=unresolved,
                database_fingerprint=database_fingerprint,
            )

        position_rows = [row for raw in snapshot.positions if (row := _position_evidence(raw))]
        fill_rows = _snapshot_fill_evidence(
            snapshot,
            legs=legs,
            bindings_by_id=bindings_by_id,
        )
        attribution = match_entry_legs_to_positions(
            [
                _leg_evidence(leg, binding=bindings_by_id[int(leg.execution_binding_id)])
                for leg in legs
            ],
            position_rows,
            fill_rows,
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

        for target_leg_id, pos_id in sorted(attribution.assignments.items()):
            if int(target_leg_id) in conflict_leg_ids:
                continue
            target_leg = legs_by_id[int(target_leg_id)]
            existing_owners = [
                leg
                for leg in legs
                if leg.pos_id == pos_id and int(leg.id) != int(target_leg_id)
            ]
            if any(str(leg.attribution_status or "") == "verified" for leg in existing_owners):
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
            if target_leg.pos_id != pos_id or str(target_leg.attribution_status) != "verified":
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

        actions = _dedupe_actions([*clear_actions, *terminal_actions, *assign_actions])
        return _build_plan(
            created_at=created_at,
            live_position_ids=_live_position_ids(snapshot.positions),
            actions=tuple(actions),
            unresolved_conflicts=list(attribution.conflicts),
            database_fingerprint=database_fingerprint,
        )


def apply_position_attribution_repair_plan(
    session_factory,
    plan: PositionAttributionRepairPlan,
    *,
    deepcoin_client=None,
) -> PositionAttributionRepairResult:
    with session_factory() as session:
        applied_fingerprints = {
            row.fingerprint
            for row in session.query(PositionAttributionAudit)
            .filter(PositionAttributionAudit.event_type == "historical_repair")
            .all()
        }
        if plan.actions and all(
            _repair_audit_fingerprint(plan, action) in applied_fingerprints
            for action in plan.actions
        ):
            return PositionAttributionRepairResult(applied=0, already_applied=True)

    if deepcoin_client is not None:
        current_live_ids = _live_position_ids(deepcoin_client.list_positions())
        if current_live_ids != plan.live_position_ids:
            raise PositionAttributionRepairError("live positions changed since repair plan")

    with session_factory() as session:
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
        if _database_fingerprint(bindings, legs) != plan.database_fingerprint:
            raise PositionAttributionRepairError("stale repair plan: database evidence changed")
        legs_by_id = {int(row.id): row for row in legs}
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
                _insert_repair_audit(session, plan=plan, action=action, leg=leg)
            _derive_repaired_bindings(bindings, list(legs_by_id.values()), plan.created_at)
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise PositionAttributionRepairError("repair transaction failed") from exc
        except Exception:
            session.rollback()
            raise
    return PositionAttributionRepairResult(applied=len(plan.actions))


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
    actions: tuple[PositionAttributionRepairAction, ...],
    unresolved_conflicts: list[dict[str, object]],
    database_fingerprint: str,
) -> PositionAttributionRepairPlan:
    payload = {
        "live_position_ids": live_position_ids,
        "actions": [asdict(action) for action in actions],
        "unresolved_conflicts": unresolved_conflicts,
        "database_fingerprint": database_fingerprint,
    }
    return PositionAttributionRepairPlan(
        created_at=created_at,
        live_position_ids=live_position_ids,
        actions=actions,
        unresolved_conflicts=unresolved_conflicts,
        database_fingerprint=database_fingerprint,
        fingerprint=_hash(payload),
    )


def _database_fingerprint(bindings, legs) -> str:
    return _hash(
        {
            "bindings": [
                {
                    "id": int(row.id),
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
                    "order_id": row.order_id,
                    "client_order_id": row.client_order_id,
                    "pos_id": row.pos_id,
                    "status": row.status,
                    "attribution_status": row.attribution_status,
                }
                for row in legs
            ],
        }
    )


def _live_position_ids(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(row.get("posId") or row.get("pos_id") or row.get("id"))
            for row in rows
            if row.get("posId") or row.get("pos_id") or row.get("id")
        )
    )


def _insert_repair_audit(session, *, plan, action, leg) -> None:
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
                {"plan": plan.fingerprint, **action.evidence},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            created_at=plan.created_at,
        )
    )


def _repair_audit_fingerprint(plan, action) -> str:
    return _hash({"plan": plan.fingerprint, "action": asdict(action)})


def _derive_repaired_bindings(bindings, legs, updated_at) -> None:
    for binding in bindings:
        binding_legs = [
            leg for leg in legs if int(leg.execution_binding_id) == int(binding.id)
        ]
        verified = [
            str(leg.pos_id)
            for leg in sorted(binding_legs, key=lambda row: row.leg_index)
            if leg.pos_id and str(leg.attribution_status) == "verified"
        ]
        if verified:
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

"""Plan or idempotently adopt strategy-wide automatic break-even work."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionReconciliationObservation,
    StrategyBreakEvenConvergence,
    StrategyBreakEvenConvergenceLeg,
    StrategyLifecycle,
)


class BreakEvenConvergencePlanningError(RuntimeError):
    """Raised when exact strategy-wide targets cannot be frozen safely."""


@dataclass(frozen=True, slots=True)
class BreakEvenConvergenceLegRecord:
    id: int
    execution_order_leg_id: int
    pos_id: str
    preflight_size: str
    avg_entry_price: str
    old_protection_json: str
    status: str
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class BreakEvenConvergenceRecord:
    id: int
    strategy_instance_id: str
    trigger_type: str
    trigger_identity: str
    execution_mode: str
    status: str
    reason_code: str | None
    target_snapshot_json: str
    legs: tuple[BreakEvenConvergenceLegRecord, ...]


def plan_or_adopt_break_even_convergence(
    session_factory: sessionmaker,
    *,
    trigger_type: str,
    trigger_identity: str,
    trigger_evidence: dict[str, Any],
    strategy_instance_id: str,
    planned_at: datetime,
    execution_mode: str,
) -> BreakEvenConvergenceRecord:
    """Freeze all exact live/deferred entry legs for one proven trigger."""

    normalized_trigger = str(trigger_type or "").strip()
    normalized_identity = str(trigger_identity or "").strip()
    normalized_strategy = str(strategy_instance_id or "").strip()
    normalized_mode = str(execution_mode or "").strip().lower()
    if normalized_trigger not in {"tp1_fill", "confirmed_partial_close"}:
        raise BreakEvenConvergencePlanningError("break_even_trigger_type_invalid")
    if not normalized_identity or not normalized_strategy:
        raise BreakEvenConvergencePlanningError("break_even_trigger_identity_invalid")
    if normalized_mode not in {"disabled", "shadow", "live"}:
        raise BreakEvenConvergencePlanningError("break_even_execution_mode_invalid")
    evidence_json = _json(trigger_evidence)

    with session_factory() as session:
        existing = _load_existing(
            session,
            strategy_instance_id=normalized_strategy,
            trigger_type=normalized_trigger,
            trigger_identity=normalized_identity,
        )
        if existing is not None:
            if (
                existing.trigger_evidence_json != evidence_json
                or existing.execution_mode != normalized_mode
            ):
                raise BreakEvenConvergencePlanningError(
                    "break_even_trigger_replay_conflict"
                )
            return _to_record(session, existing)

        binding_rows = (
            session.query(ExecutionBinding)
            .filter_by(
                venue="deepcoin",
                strategy_instance_id=normalized_strategy,
            )
            .filter(ExecutionBinding.status.in_(["active", "open"]))
            .all()
        )
        if len(binding_rows) != 1:
            raise BreakEvenConvergencePlanningError(
                "break_even_binding_not_unique"
            )
        binding = binding_rows[0]
        lifecycle_rows = (
            session.query(StrategyLifecycle)
            .filter_by(execution_binding_id=binding.id)
            .filter(
                StrategyLifecycle.lifecycle_status.in_(
                    ["entered", "holding", "open", "managing"]
                )
            )
            .all()
        )
        if len(lifecycle_rows) != 1:
            raise BreakEvenConvergencePlanningError(
                "break_even_lifecycle_not_unique"
            )
        lifecycle = lifecycle_rows[0]
        entry_legs = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding.id, purpose="entry")
            .order_by(ExecutionOrderLeg.leg_index.asc(), ExecutionOrderLeg.id.asc())
            .all()
        )
        live_legs = [
            leg
            for leg in entry_legs
            if str(leg.status or "").lower() in {"active", "partially_filled"}
            and str(leg.attribution_status or "").lower() == "verified"
            and str(leg.pos_id or "").strip()
        ]
        if not live_legs:
            raise BreakEvenConvergencePlanningError("break_even_live_leg_missing")
        deferred_legs = [
            leg
            for leg in entry_legs
            if not str(leg.pos_id or "").strip()
            and str(leg.status or "").lower()
            in {"pending", "open", "submitted", "partially_filled", "partial"}
        ]

        live_targets: list[tuple[ExecutionOrderLeg, PositionReconciliationObservation]] = []
        for leg in live_legs:
            observations = (
                session.query(PositionReconciliationObservation)
                .filter_by(
                    venue="deepcoin",
                    execution_order_leg_id=leg.id,
                    pos_id=str(leg.pos_id),
                )
                .order_by(
                    PositionReconciliationObservation.observed_at.desc(),
                    PositionReconciliationObservation.id.desc(),
                )
                .limit(1)
                .all()
            )
            if (
                len(observations) != 1
                or not observations[0].snapshot_complete
                or not observations[0].avg_entry_price
            ):
                raise BreakEvenConvergencePlanningError(
                    "break_even_live_observation_incomplete"
                )
            live_targets.append((leg, observations[0]))

        target_snapshot = {
            "version": 1,
            "strategy_instance_id": normalized_strategy,
            "execution_binding_id": int(binding.id),
            "target_lifecycle_id": int(lifecycle.id),
            "live_positions": [
                {
                    "execution_order_leg_id": int(leg.id),
                    "pos_id": str(observation.pos_id),
                    "size_text": str(observation.size_text),
                    "avg_entry_price": str(observation.avg_entry_price),
                    "observation_id": int(observation.id),
                    "snapshot_fingerprint": observation.snapshot_fingerprint,
                }
                for leg, observation in live_targets
            ],
            "deferred_entry_leg_ids": [int(leg.id) for leg in deferred_legs],
            "deferred_entries": [
                {
                    "execution_order_leg_id": int(leg.id),
                    "order_id": leg.order_id,
                    "client_order_id": leg.client_order_id,
                    "status": leg.status,
                }
                for leg in deferred_legs
            ],
            "planned_at": planned_at.isoformat(),
        }
        status = "blocked" if normalized_mode == "disabled" else "planned"
        reason_code = (
            "automatic_break_even_disabled"
            if normalized_mode == "disabled"
            else None
        )
        convergence = StrategyBreakEvenConvergence(
            venue="deepcoin",
            strategy_instance_id=normalized_strategy,
            execution_binding_id=int(binding.id),
            target_lifecycle_id=int(lifecycle.id),
            trigger_type=normalized_trigger,
            trigger_identity=normalized_identity,
            trigger_evidence_json=evidence_json,
            target_snapshot_json=_json(target_snapshot),
            execution_mode=normalized_mode,
            status=status,
            reason_code=reason_code,
            planned_at=planned_at,
            created_at=planned_at,
            updated_at=planned_at,
        )
        session.add(convergence)
        try:
            session.flush()
            if normalized_mode != "disabled":
                for leg, observation in live_targets:
                    session.add(
                        StrategyBreakEvenConvergenceLeg(
                            convergence_id=int(convergence.id),
                            execution_order_leg_id=int(leg.id),
                            pos_id=str(observation.pos_id),
                            preflight_size=str(observation.size_text),
                            avg_entry_price=str(observation.avg_entry_price),
                            old_protection_json=str(observation.pending_tpsl_json),
                            decision_json="{}",
                            status="planned",
                            created_at=planned_at,
                            updated_at=planned_at,
                        )
                    )
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = _load_existing(
                session,
                strategy_instance_id=normalized_strategy,
                trigger_type=normalized_trigger,
                trigger_identity=normalized_identity,
            )
            if existing is None:
                raise BreakEvenConvergencePlanningError(
                    "break_even_trigger_reservation_conflict"
                ) from None
            if (
                existing.trigger_evidence_json != evidence_json
                or existing.execution_mode != normalized_mode
            ):
                raise BreakEvenConvergencePlanningError(
                    "break_even_trigger_replay_conflict"
                ) from None
            return _to_record(session, existing)
        return _to_record(session, convergence)


def _load_existing(
    session,
    *,
    strategy_instance_id: str,
    trigger_type: str,
    trigger_identity: str,
) -> StrategyBreakEvenConvergence | None:
    return (
        session.query(StrategyBreakEvenConvergence)
        .filter_by(
            venue="deepcoin",
            strategy_instance_id=strategy_instance_id,
            trigger_type=trigger_type,
            trigger_identity=trigger_identity,
        )
        .one_or_none()
    )


def _to_record(session, row: StrategyBreakEvenConvergence) -> BreakEvenConvergenceRecord:
    legs = (
        session.query(StrategyBreakEvenConvergenceLeg)
        .filter_by(convergence_id=row.id)
        .order_by(StrategyBreakEvenConvergenceLeg.id.asc())
        .all()
    )
    return BreakEvenConvergenceRecord(
        id=int(row.id),
        strategy_instance_id=str(row.strategy_instance_id),
        trigger_type=str(row.trigger_type),
        trigger_identity=str(row.trigger_identity),
        execution_mode=str(row.execution_mode),
        status=str(row.status),
        reason_code=row.reason_code,
        target_snapshot_json=str(row.target_snapshot_json),
        legs=tuple(
            BreakEvenConvergenceLegRecord(
                id=int(leg.id),
                execution_order_leg_id=int(leg.execution_order_leg_id),
                pos_id=str(leg.pos_id),
                preflight_size=str(leg.preflight_size),
                avg_entry_price=str(leg.avg_entry_price),
                old_protection_json=str(leg.old_protection_json),
                status=str(leg.status),
                reason_code=leg.reason_code,
            )
            for leg in legs
        ),
    )


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

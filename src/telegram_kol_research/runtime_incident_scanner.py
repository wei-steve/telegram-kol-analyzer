"""Closed contracts for the dormant proactive invariant scanner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import json
from math import isfinite
import re
from types import MappingProxyType
from typing import Literal, Mapping
from sqlalchemy import Integer, cast, exists, select

from telegram_kol_research.config import RUNTIME_SCANNER_RULE_IDS
from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionAttributionAudit,
    PositionMutationIntent,
    PositionProtectionLedger,
    PositionProtectionLeg,
    PositionProtectionHealthObservation,
    RuntimeIncidentObservation,
    StrategyManagementBatch,
    StrategyManagementLeg,
    TriggerProtectionStopRescue,
)

_REFERENCE = re.compile(r"[a-z][a-z0-9_-]{0,63}:[A-Za-z0-9_.:-]{1,255}")
_SAFE_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SENSITIVE = ("token", "secret", "password", "api_key", "credential")

_LIVE_EXPOSURE_STATES = frozenset(
    {"active", "partially_filled", "protection_recovery_pending"}
)
_PENDING_OR_VERIFIED_PRIMARY_STATES = frozenset(
    {"submitting", "submitted", "pending_readback", "verified"}
)
DORMANT_POSITION_COMPLIANCE_RULE_IDS = frozenset(
    {
        "terminal_high_risk_management_without_instruction_v1",
        "verified_replacement_role_gap_v1",
        "admitted_target_item_nonterminal_after_deadline_v1",
        "management_target_batch_state_inconsistent_v1",
    }
)


def _critical_unprotected_positions_in_session(
    session,
    *,
    chat_id: int | None = None,
    pos_id: str | None = None,
    prefer_unobserved: bool = False,
    limit: int = 100,
) -> tuple[dict[str, str | int | None], ...]:
    """Return bounded exact positions that are live but have no primary stop."""

    query = (
        session.query(ExecutionOrderLeg, ExecutionBinding)
        .join(ExecutionBinding, ExecutionBinding.id == ExecutionOrderLeg.execution_binding_id)
        .filter(ExecutionBinding.venue == "deepcoin")
        .filter(ExecutionBinding.status.in_(("open", "active", "stale")))
        .filter(ExecutionOrderLeg.purpose == "entry")
        .filter(ExecutionOrderLeg.attribution_status == "verified")
        .filter(ExecutionOrderLeg.status.in_(tuple(_LIVE_EXPOSURE_STATES)))
        .filter(ExecutionOrderLeg.pos_id.is_not(None))
        .filter(ExecutionOrderLeg.pos_id != "")
        .filter(~exists().where(
            (PositionProtectionLedger.execution_order_leg_id == ExecutionOrderLeg.id)
            & (PositionProtectionLedger.pos_id == ExecutionOrderLeg.pos_id)
            & PositionProtectionLedger.purpose.in_(("stop_loss", "combined"))
            & PositionProtectionLedger.status.in_(("verified", "active", "protected"))
        ))
        .filter(~exists().where(
            (PositionProtectionLeg.execution_order_leg_id == ExecutionOrderLeg.id)
            & (PositionProtectionLeg.pos_id == ExecutionOrderLeg.pos_id)
            & (PositionProtectionLeg.role == "primary_stop")
            & PositionProtectionLeg.status.in_(tuple(_PENDING_OR_VERIFIED_PRIMARY_STATES))
        ))
        .filter(~exists().where(
            (BoundPositionCloseReservation.pos_id == ExecutionOrderLeg.pos_id)
            & BoundPositionCloseReservation.status.in_((
                "reserved", "submitted", "submit_unknown", "recovery_required",
            ))
        ))
        .filter(~exists().where(
            (PositionMutationIntent.pos_id == ExecutionOrderLeg.pos_id)
            & PositionMutationIntent.status.in_((
                "reserved", "submitted", "recovery_required",
            ))
        ))
        .filter(~exists().where(
            (StrategyManagementLeg.pos_id == ExecutionOrderLeg.pos_id)
            & (StrategyManagementLeg.management_batch_id == StrategyManagementBatch.id)
            & StrategyManagementBatch.status.not_in(("succeeded", "blocked", "resolved"))
        ))
    )
    if chat_id is not None:
        query = query.filter(ExecutionBinding.chat_id == int(chat_id))
    if pos_id is not None:
        query = query.filter(ExecutionOrderLeg.pos_id == str(pos_id))
    order_columns = []
    if prefer_unobserved:
        previously_observed = exists().where(
            (RuntimeIncidentObservation.rule_id == "active_position_missing_protection_v1")
            & (RuntimeIncidentObservation.object_id == ExecutionOrderLeg.pos_id)
        )
        order_columns.append(previously_observed.asc())
    order_columns.append(ExecutionOrderLeg.id.asc())
    candidates = query.order_by(*order_columns).limit(max(1, min(limit, 100))).all()
    results: list[dict[str, str | int | None]] = []
    for leg, binding in candidates:
        pos_id = str(leg.pos_id)
        primary = (
            session.query(PositionProtectionLeg)
            .filter(PositionProtectionLeg.venue == "deepcoin")
            .filter(PositionProtectionLeg.execution_order_leg_id == int(leg.id))
            .filter(PositionProtectionLeg.role == "primary_stop")
            .order_by(PositionProtectionLeg.id.asc())
            .first()
        )
        rescue = (
            session.query(TriggerProtectionStopRescue)
            .filter(TriggerProtectionStopRescue.execution_order_leg_id == int(leg.id))
            .order_by(TriggerProtectionStopRescue.id.desc())
            .first()
        )
        first_exposure = (
            session.query(PositionAttributionAudit.created_at)
            .filter(PositionAttributionAudit.execution_order_leg_id == int(leg.id))
            .filter(PositionAttributionAudit.event_type == "ownership_verified")
            .order_by(PositionAttributionAudit.created_at.asc())
            .first()
        )
        started_at = first_exposure[0] if first_exposure is not None else leg.created_at
        results.append({
            "chat_id": int(binding.chat_id),
            "strategy_instance_id": str(binding.strategy_instance_id or ""),
            "execution_binding_id": int(binding.id),
            "execution_order_leg_id": int(leg.id),
            "pos_id": pos_id,
            "planned_stop": (
                str(primary.planned_trigger_price)
                if primary is not None and primary.planned_trigger_price is not None
                else None
            ),
            "exposure_started_at": started_at.isoformat(),
            "rescue_state": str(rescue.status) if rescue is not None else "not_planned",
        })
    return tuple(results)


def list_critical_unprotected_positions(
    session_factory,
    *,
    chat_id: int | None = None,
    pos_id: str | None = None,
    prefer_unobserved: bool = False,
    limit: int = 100,
) -> tuple[dict[str, str | int | None], ...]:
    with session_factory() as session:
        return _critical_unprotected_positions_in_session(
            session,
            chat_id=chat_id,
            pos_id=pos_id,
            prefer_unobserved=prefer_unobserved,
            limit=limit,
        )


@dataclass(frozen=True, slots=True)
class InvariantObservation:
    rule_id: str
    rule_version: str
    object_kind: str
    object_id: str
    severity: str
    outcome: Literal["normal", "abnormal", "evidence_insufficient"]
    evidence_references: tuple[str, ...]
    evidence_fingerprint: str
    summary: Mapping[str, str | int | bool | float | None]

    def __post_init__(self) -> None:
        if self.rule_id not in RUNTIME_SCANNER_RULE_IDS:
            raise ValueError("unknown scanner rule")
        if self.rule_version != "1":
            raise ValueError("unsupported scanner rule version")
        if not 1 <= len(self.object_kind) <= 64 or not 1 <= len(self.object_id) <= 255:
            raise ValueError("invalid scanner object")
        if self.severity not in {"critical", "high", "medium", "low"}:
            raise ValueError("unsupported scanner severity")
        if self.outcome not in {"normal", "abnormal", "evidence_insufficient"}:
            raise ValueError("unsupported scanner outcome")
        if not 1 <= len(self.evidence_references) <= 16 or any(
            not _REFERENCE.fullmatch(value) for value in self.evidence_references
        ):
            raise ValueError("invalid evidence reference")
        if not re.fullmatch(r"[0-9a-f]{64}", self.evidence_fingerprint):
            raise ValueError("invalid evidence fingerprint")
        if len(self.summary) > 16:
            raise ValueError("unbounded scanner summary")
        clean = dict(self.summary)
        for key, value in clean.items():
            if not _SAFE_KEY.fullmatch(key) or any(part in key for part in _SENSITIVE):
                raise ValueError("sensitive scanner summary")
            if isinstance(value, str) and len(value) > 256:
                raise ValueError("unbounded scanner summary")
            if isinstance(value, float) and not isfinite(value):
                raise ValueError("non-finite scanner summary")
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise ValueError("unsupported scanner summary")
        object.__setattr__(self, "evidence_references", tuple(self.evidence_references))
        object.__setattr__(self, "summary", MappingProxyType(clean))


def build_scanner_facts(session_factory, *, rules: frozenset[str], observed_at):
    """Build only deployed projections; dormant catalog rules yield no facts."""
    result: dict[str, tuple[Mapping, ...]] = {}
    if "active_position_missing_protection_v1" in rules:
        risks = list_critical_unprotected_positions(
            session_factory, prefer_unobserved=True, limit=100
        )
        risk_by_pos_id = {str(row["pos_id"]): row for row in risks}
        with session_factory() as session:
            prior = (
                session.query(RuntimeIncidentObservation)
                .filter(
                    RuntimeIncidentObservation.rule_id
                    == "active_position_missing_protection_v1"
                )
                .filter(RuntimeIncidentObservation.state.in_(("observing", "shadow_confirmed")))
                .order_by(RuntimeIncidentObservation.last_observed_at.asc())
                .limit(100)
                .all()
            )
            prior_pos_ids = {str(observation.object_id) for observation in prior}
            new_risks = [
                row for row in risks if str(row["pos_id"]) not in prior_pos_ids
            ]
            projected: list[Mapping] = [
                _active_position_missing_protection_facts(row)
                for row in new_risks[:50]
            ]
            for row in new_risks[:50]:
                risk_by_pos_id.pop(str(row["pos_id"]), None)
            prior_budget = min(50, 100 - len(projected))
            for observation in prior[:prior_budget]:
                prior_pos_id = str(observation.object_id)
                row = risk_by_pos_id.pop(prior_pos_id, None)
                if row is not None:
                    projected.append(_active_position_missing_protection_facts(row))
                    continue
                projected.append(
                    _project_prior_unprotected_position(
                        session, pos_id=prior_pos_id
                    )
                )
        remaining = max(0, 100 - len(projected))
        projected.extend(
            _active_position_missing_protection_facts(row)
            for row in list(risk_by_pos_id.values())[:remaining]
        )
        result["active_position_missing_protection_v1"] = tuple(projected)
    if "cancel_outcome_stale_unknown_v1" in rules:
        with session_factory() as session:
            cutoff = observed_at.replace(tzinfo=None) - timedelta(minutes=10)
            unknown_statuses = {
                "reserved", "submitting", "submitted", "recovery_required"
            }
            recovery_pairs = (
                session.query(RuntimeIncidentObservation, PositionMutationIntent)
                .join(
                    PositionMutationIntent,
                    cast(RuntimeIncidentObservation.object_id, Integer)
                    == PositionMutationIntent.id,
                )
                .filter(
                    RuntimeIncidentObservation.rule_id
                    == "cancel_outcome_stale_unknown_v1",
                    RuntimeIncidentObservation.state == "shadow_confirmed",
                    PositionMutationIntent.operation.in_((
                        "cancel_trigger_order", "cancel_position_sltp"
                    )),
                    PositionMutationIntent.status.not_in(tuple(unknown_statuses)),
                )
                .order_by(RuntimeIncidentObservation.last_observed_at.asc())
                .limit(100)
                .all()
            )
            priority_rows = [
                source for _, source in recovery_pairs
            ]

            remaining = max(0, 100 - len(priority_rows))
            observing_pairs = (
                session.query(RuntimeIncidentObservation, PositionMutationIntent)
                .join(
                    PositionMutationIntent,
                    cast(RuntimeIncidentObservation.object_id, Integer)
                    == PositionMutationIntent.id,
                )
                .filter(
                    RuntimeIncidentObservation.rule_id
                    == "cancel_outcome_stale_unknown_v1",
                    RuntimeIncidentObservation.state == "observing",
                    PositionMutationIntent.operation.in_((
                        "cancel_trigger_order", "cancel_position_sltp"
                    )),
                )
                .order_by(RuntimeIncidentObservation.last_observed_at.asc())
                .limit(remaining)
                .all()
                if remaining
                else []
            )
            priority_rows.extend(source for _, source in observing_pairs)
            remaining = max(0, 100 - len(priority_rows))
            observed_subquery = select(
                cast(RuntimeIncidentObservation.object_id, Integer)
            ).where(
                RuntimeIncidentObservation.rule_id
                == "cancel_outcome_stale_unknown_v1"
            )
            candidates = (
                session.query(PositionMutationIntent)
                .filter(PositionMutationIntent.operation.in_(("cancel_trigger_order", "cancel_position_sltp")))
                .filter(PositionMutationIntent.status.in_(tuple(unknown_statuses)))
                .filter(PositionMutationIntent.updated_at <= cutoff)
                .filter(PositionMutationIntent.id.not_in(observed_subquery))
                .order_by(PositionMutationIntent.id.asc())
                .limit(remaining)
                .all()
                if remaining
                else []
            )
            rows = [*priority_rows, *candidates]
            result["cancel_outcome_stale_unknown_v1"] = tuple(
                {
                    "complete": True,
                    "object_id": str(row.id),
                    "cancel_unknown": row.status in unknown_statuses,
                    "transition_window_expired": row.updated_at <= cutoff,
                    "evidence_references": [f"mutation-intent:{row.id}"],
                }
                for row in rows
            )
    if "management_safety_gate_divergence_v1" in rules:
        result["management_safety_gate_divergence_v1"] = (
            _management_safety_gate_divergence_facts(
                session_factory,
                observed_at=observed_at,
                limit=100,
            )
        )
    return result


def _management_safety_gate_divergence_facts(
    session_factory,
    *,
    observed_at,
    limit: int,
) -> tuple[Mapping, ...]:
    """Project only exact, zero-write historical refusals with later healthy evidence."""

    bounded_limit = max(1, min(int(limit), 100))
    with session_factory() as session:
        batches = (
            session.query(StrategyManagementBatch)
            .filter(StrategyManagementBatch.status == "blocked")
            .filter(
                StrategyManagementBatch.reason_code
                == "protection_recovery_required"
            )
            .filter(StrategyManagementBatch.started_at.is_(None))
            .filter(
                ~exists().where(
                    StrategyManagementLeg.management_batch_id
                    == StrategyManagementBatch.id
                )
            )
            .filter(
                ~exists().where(
                    PositionMutationIntent.execution_binding_id
                    == StrategyManagementBatch.execution_binding_id
                )
            )
            .order_by(StrategyManagementBatch.id.asc())
            .limit(bounded_limit)
            .all()
        )
        projected: list[Mapping] = []
        for batch in batches:
            health_rows = (
                session.query(PositionProtectionHealthObservation)
                .filter(
                    PositionProtectionHealthObservation.execution_binding_id
                    == batch.execution_binding_id,
                    PositionProtectionHealthObservation.observed_at
                    >= batch.planned_at,
                    PositionProtectionHealthObservation.observed_at
                    <= observed_at.replace(tzinfo=None),
                )
                .order_by(
                    PositionProtectionHealthObservation.observed_at.desc(),
                    PositionProtectionHealthObservation.id.desc(),
                )
                .limit(100)
                .all()
            )
            latest_by_scope = {}
            for health in health_rows:
                scope = (int(health.execution_order_leg_id), str(health.pos_id))
                latest_by_scope.setdefault(scope, health)
            latest = tuple(latest_by_scope.values())
            snapshot_fingerprints = {
                str(row.exchange_snapshot_fingerprint) for row in latest
            }
            if not 1 <= len(latest) <= 4 or len(snapshot_fingerprints) != 1 or any(
                row.classification != "healthy_current_evidence"
                or not re.fullmatch(r"[0-9a-f]{64}", row.evidence_fingerprint or "")
                or not re.fullmatch(
                    r"[0-9a-f]{64}", row.exchange_snapshot_fingerprint or ""
                )
                or not _bounded_incident_ids(row.source_incident_ids_json)
                for row in latest
            ):
                continue
            health = latest[0]
            scope_fingerprint = _scope_health_fingerprint(latest)
            references = [
                f"management-batch:{batch.id}",
                f"binding:{batch.execution_binding_id}",
            ]
            for row in sorted(latest, key=lambda item: int(item.id)):
                references.extend(
                    (
                        f"protection-health:{row.id}",
                        f"entry-leg:{row.execution_order_leg_id}",
                        f"position:{row.pos_id}",
                    )
                )
            projected.append(
                {
                    "complete": True,
                    "object_id": str(batch.id),
                    "historical_only_refusal": True,
                    "current_protection_healthy": True,
                    "exact_scope_match": True,
                    "fingerprint_generation_match": True,
                    "hard_reason_present": False,
                    "management_batch_id": int(batch.id),
                    "execution_binding_id": int(batch.execution_binding_id),
                    "health_observation_id": int(health.id),
                    "execution_order_leg_id": int(health.execution_order_leg_id),
                    "pos_id": str(health.pos_id),
                    "refusal_reason_code": str(batch.reason_code),
                    "target_fingerprint": str(batch.target_fingerprint),
                    "health_evidence_fingerprint": str(health.evidence_fingerprint),
                    "health_scope_fingerprint": scope_fingerprint,
                    "exchange_snapshot_fingerprint": str(
                        health.exchange_snapshot_fingerprint
                    ),
                    "refused_at": batch.planned_at.isoformat(),
                    "health_observed_at": health.observed_at.isoformat(),
                    "evidence_references": tuple(references),
                }
            )
        return tuple(projected)


def _bounded_incident_ids(value: str) -> bool:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(decoded, list)
        and 1 <= len(decoded) <= 100
        and all(isinstance(item, int) and item > 0 for item in decoded)
    )


def _scope_health_fingerprint(rows) -> str:
    material = [
        (
            int(row.execution_order_leg_id),
            str(row.pos_id),
            str(row.evidence_fingerprint),
            str(row.exchange_snapshot_fingerprint),
        )
        for row in sorted(
            rows,
            key=lambda item: (int(item.execution_order_leg_id), str(item.pos_id)),
        )
    ]
    return sha256(
        json.dumps(material, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _active_position_missing_protection_facts(row: Mapping) -> Mapping:
    return {
        "complete": True,
        "object_id": str(row["pos_id"]),
        "position_present": True,
        "primary_protection_verified": False,
        "chat_id": int(row["chat_id"]),
        "strategy_instance_id": str(row["strategy_instance_id"]),
        "execution_binding_id": int(row["execution_binding_id"]),
        "execution_order_leg_id": int(row["execution_order_leg_id"]),
        "planned_stop": row["planned_stop"],
        "exposure_started_at": row["exposure_started_at"],
        "rescue_state": row["rescue_state"],
        "evidence_references": (
            f"binding:{row['execution_binding_id']}",
            f"entry-leg:{row['execution_order_leg_id']}",
            f"position:{row['pos_id']}",
        ),
    }


def _project_prior_unprotected_position(session, *, pos_id: str) -> Mapping:
    current = _critical_unprotected_positions_in_session(
        session, pos_id=pos_id, limit=1
    )
    if current:
        return _active_position_missing_protection_facts(current[0])
    pair = (
        session.query(ExecutionOrderLeg, ExecutionBinding)
        .join(ExecutionBinding, ExecutionBinding.id == ExecutionOrderLeg.execution_binding_id)
        .filter(ExecutionOrderLeg.purpose == "entry")
        .filter(ExecutionOrderLeg.pos_id == str(pos_id))
        .order_by(ExecutionOrderLeg.id.desc())
        .first()
    )
    references = (f"position:{pos_id}",)
    if pair is None:
        return {
            "complete": False,
            "object_id": pos_id,
            "position_present": False,
            "primary_protection_verified": False,
            "evidence_references": references,
        }
    leg, binding = pair
    references = (
        f"binding:{binding.id}", f"entry-leg:{leg.id}", f"position:{pos_id}"
    )
    terminal_leg = str(leg.status or "").lower() in {
        "cancelled", "closed", "filled_closed", "manually_closed", "rejected",
    }
    terminal_binding = str(binding.status or "").lower() in {
        "cancelled", "closed", "filled",
    }
    if terminal_leg or terminal_binding:
        return {
            "complete": True,
            "object_id": pos_id,
            "position_present": False,
            "primary_protection_verified": True,
            "evidence_references": references,
        }
    if (
        str(leg.attribution_status or "") != "verified"
        or str(binding.status or "") not in {"open", "active", "stale"}
        or str(leg.status or "") not in _LIVE_EXPOSURE_STATES
    ):
        return {
            "complete": False,
            "object_id": pos_id,
            "position_present": True,
            "primary_protection_verified": False,
            "evidence_references": references,
        }
    # An exact live verified leg omitted by the critical query is currently
    # protected or owned by a close/mutation workflow.
    return {
        "complete": True,
        "object_id": pos_id,
        "position_present": True,
        "primary_protection_verified": True,
        "evidence_references": references,
    }


def run_scanner_cycle(*, session_factory, config, facts_by_rule: Mapping[str, tuple[Mapping, ...]], observed_at):
    """Evaluate the exact allowlist and persist shadow observations only."""
    from telegram_kol_research.runtime_incident_observations import record_observations
    from telegram_kol_research.runtime_incident_rules import evaluate_rule

    counts = {"rules": 0, "observations": 0, "abnormal": 0, "insufficient": 0}
    observations = []
    for rule_id in sorted(config.rules):
        counts["rules"] += 1
        for facts in tuple(facts_by_rule.get(rule_id, ())):
            result = evaluate_rule(rule_id, facts)
            observations.append(result)
            counts["observations"] += 1
            if result.outcome == "abnormal":
                counts["abnormal"] += 1
            elif result.outcome == "evidence_insufficient":
                counts["insufficient"] += 1
    if observations:
        record_observations(
            session_factory,
            observations=tuple(observations),
            observed_at=observed_at,
        )
    return counts

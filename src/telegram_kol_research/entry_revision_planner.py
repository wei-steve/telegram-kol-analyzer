"""Plan immutable, exact-owned sizing revisions without exchange writes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    EntryAssemblyFragment,
    EntryStrategyAssembly,
    EntryStrategyFragment,
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    RawMessage,
    StrategyLifecycle,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
    StrategyThread,
)


PENDING_STATES = frozenset({"pending", "submitted", "open"})
FILLED_STATES = frozenset({"filled", "active", "partial_closed"})


@dataclass(frozen=True, slots=True)
class EntryRevisionPlanResult:
    status: str
    batch_id: int | None = None
    reason_code: str | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _blocked(reason: str) -> EntryRevisionPlanResult:
    return EntryRevisionPlanResult(status="blocked", reason_code=reason)


def plan_entry_revision(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    strategy_thread_id: int | None,
    entry_strategy_assembly_id: int,
    mode: str,
    planned_at: datetime | None = None,
) -> EntryRevisionPlanResult:
    """Freeze an exact submitted strategy and desired immutable replacement legs."""

    if mode not in {"disabled", "shadow", "live"}:
        raise ValueError("entry revision mode must be disabled, shadow, or live")
    if mode == "disabled":
        return EntryRevisionPlanResult(status="disabled")
    if strategy_thread_id is None:
        return _blocked("revision_target_not_unique")
    now = planned_at or datetime.now(UTC)
    with session_factory() as session:
        raw = session.get(RawMessage, int(raw_message_id))
        thread = session.get(StrategyThread, int(strategy_thread_id))
        assembly = session.get(EntryStrategyAssembly, int(entry_strategy_assembly_id))
        if raw is None or thread is None or assembly is None:
            return _blocked("revision_target_not_found")
        if int(raw.chat_id) != int(thread.chat_id):
            return _blocked("revision_target_source_mismatch")
        lifecycle = (
            session.get(StrategyLifecycle, int(thread.current_lifecycle_id))
            if thread.current_lifecycle_id is not None
            else None
        )
        if lifecycle is None or lifecycle.execution_binding_id is None:
            return _blocked("revision_target_not_unique")
        binding = session.get(ExecutionBinding, int(lifecycle.execution_binding_id))
        if binding is None:
            return _blocked("revision_binding_missing")
        if str(binding.strategy_instance_id or "") != str(assembly.strategy_instance_id):
            return _blocked("revision_assembly_binding_mismatch")
        strategy_raw = session.get(RawMessage, int(assembly.strategy_raw_message_id))
        if (
            strategy_raw is None
            or int(strategy_raw.chat_id) != int(binding.chat_id)
            or int(strategy_raw.message_id) != int(binding.message_id)
            or int(lifecycle.chat_id) != int(binding.chat_id)
            or int(lifecycle.message_id) != int(binding.message_id)
        ):
            return _blocked("revision_strategy_generation_mismatch")
        associations = (
            session.query(EntryAssemblyFragment, EntryStrategyFragment)
            .join(
                EntryStrategyFragment,
                EntryStrategyFragment.id
                == EntryAssemblyFragment.entry_strategy_fragment_id,
            )
            .filter(
                EntryAssemblyFragment.entry_strategy_assembly_id == int(assembly.id)
            )
            .all()
        )
        for _, fragment in associations:
            if (
                fragment.target_strategy_raw_message_id is not None
                and int(fragment.target_strategy_raw_message_id)
                != int(assembly.strategy_raw_message_id)
            ):
                return _blocked("revision_fragment_target_mismatch")
            if (
                fragment.target_strategy_thread_id is not None
                and int(fragment.target_strategy_thread_id) != int(thread.id)
            ) or (
                fragment.target_lifecycle_id is not None
                and int(fragment.target_lifecycle_id) != int(lifecycle.id)
            ):
                return _blocked("revision_fragment_target_mismatch")
        try:
            assembly_evidence = json.loads(assembly.evidence_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return _blocked("revision_assembly_evidence_invalid")
        order_draft = assembly_evidence.get("order_draft_snapshot")
        desired_legs = order_draft.get("order_legs") if isinstance(order_draft, dict) else None
        if not isinstance(desired_legs, list) or not desired_legs:
            return _blocked("revision_replacement_legs_missing")
        entry_legs = (
            session.query(ExecutionOrderLeg)
            .filter(
                ExecutionOrderLeg.execution_binding_id == int(binding.id),
                ExecutionOrderLeg.purpose == "entry",
            )
            .order_by(ExecutionOrderLeg.leg_index.asc(), ExecutionOrderLeg.id.asc())
            .all()
        )
        if not entry_legs:
            return _blocked("revision_entry_legs_missing")
        classified: list[tuple[ExecutionOrderLeg, str]] = []
        for leg in entry_legs:
            state = str(leg.status or "").lower()
            exact_identity = bool(leg.order_id or leg.client_order_id)
            verified = leg.last_verified_at is not None and leg.attribution_status == "verified"
            if not verified or not exact_identity:
                return _blocked("revision_submission_state_unknown")
            if state in PENDING_STATES and not leg.pos_id:
                classified.append((leg, "cancel_pending"))
            elif state in FILLED_STATES and leg.pos_id:
                classified.append((leg, "retain_filled"))
            else:
                return _blocked("revision_order_position_state_conflict")
        pos_ids = [str(leg.pos_id) for leg, action in classified if action == "retain_filled"]
        protections = (
            session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.pos_id.in_(pos_ids))
            .order_by(PositionProtectionLedger.id.asc())
            .all()
            if pos_ids
            else []
        )
        entry_snapshots = [
            {
                "execution_order_leg_id": int(leg.id),
                "leg_index": int(leg.leg_index),
                "status": str(leg.status),
                "action": action,
                "order_kind": str(leg.order_kind),
                "order_id": leg.order_id,
                "client_order_id": leg.client_order_id,
                "pos_id": leg.pos_id,
                "request": json.loads(leg.request_json or "{}"),
                "last_verified_at": leg.last_verified_at.isoformat(),
            }
            for leg, action in classified
        ]
        protection_snapshots = [
            {
                "id": int(row.id),
                "pos_id": str(row.pos_id),
                "order_id": str(row.order_id),
                "purpose": str(row.purpose),
                "status": str(row.status),
                "last_verified_at": (
                    row.last_verified_at.isoformat()
                    if row.last_verified_at is not None
                    else None
                ),
            }
            for row in protections
        ]
        target_snapshot = {
            "chat_id": int(thread.chat_id),
            "strategy_thread_id": int(thread.id),
            "target_lifecycle_id": int(lifecycle.id),
            "execution_binding_id": int(binding.id),
            "strategy_instance_id": str(binding.strategy_instance_id),
            "assembly_id": int(assembly.id),
            "assembly_fingerprint": str(assembly.fingerprint),
            "configured_risk_budget_usdt": assembly_evidence.get(
                "configured_risk_budget_usdt"
            ),
            "effective_risk_budget_usdt": assembly_evidence.get(
                "effective_risk_budget_usdt"
            ),
            "entry_legs": entry_snapshots,
            "protection": protection_snapshots,
        }
        replacement = {
            "assembly_fingerprint": str(assembly.fingerprint),
            "instrument_id": order_draft.get("instrument_id"),
            "stop_loss": order_draft.get("stop_loss"),
            "risk_budget_usdt": order_draft.get("risk_budget_usdt"),
            "order_legs": desired_legs,
        }
        fingerprint_payload = {
            "revision_kind": "entry_sizing",
            "mode": mode,
            "raw_message_id": int(raw.id),
            "target_snapshot": target_snapshot,
            "replacement": replacement,
        }
        fingerprint = hashlib.sha256(
            _canonical_json(fingerprint_payload).encode("utf-8")
        ).hexdigest()
        existing = (
            session.query(StrategyRevisionBatch)
            .filter(StrategyRevisionBatch.idempotency_fingerprint == fingerprint)
            .one_or_none()
        )
        if existing is not None:
            return EntryRevisionPlanResult(
                status=str(existing.status), batch_id=int(existing.id), reason_code=existing.reason_code
            )
        batch = StrategyRevisionBatch(
            idempotency_fingerprint=fingerprint,
            raw_message_id=int(raw.id),
            strategy_thread_id=int(thread.id),
            target_lifecycle_id=int(lifecycle.id),
            execution_binding_id=int(binding.id),
            revision_kind="entry_sizing",
            target_assembly_id=int(assembly.id),
            target_assembly_fingerprint=str(assembly.fingerprint),
            target_snapshot_json=_canonical_json(target_snapshot),
            status="shadow_planned" if mode == "shadow" else "planned",
            replacement_json=_canonical_json(replacement),
            planned_at=now,
            updated_at=now,
        )
        try:
            with session.begin_nested():
                session.add(batch)
                session.flush()
        except IntegrityError:
            existing = (
                session.query(StrategyRevisionBatch)
                .filter(StrategyRevisionBatch.idempotency_fingerprint == fingerprint)
                .one()
            )
            return EntryRevisionPlanResult(
                status=str(existing.status), batch_id=int(existing.id), reason_code=existing.reason_code
            )
        for leg, action in classified:
            session.add(
                StrategyRevisionLeg(
                    revision_batch_id=int(batch.id),
                    execution_order_leg_id=int(leg.id),
                    action=action,
                    prior_status=str(leg.status),
                    status="retained" if action == "retain_filled" else "planned",
                    order_id=leg.order_id,
                    client_order_id=leg.client_order_id,
                    pos_id=leg.pos_id,
                    updated_at=now,
                )
            )
        session.commit()
        return EntryRevisionPlanResult(status=str(batch.status), batch_id=int(batch.id))

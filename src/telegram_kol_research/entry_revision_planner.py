"""Plan immutable, exact-owned sizing revisions without exchange writes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    EntryAssemblyFragment,
    EntryStrategyAssembly,
    EntryStrategyFragment,
    ExecutionBinding,
    ExecutionOrderLeg,
    MessageEvidenceVersion,
    PositionProtectionLedger,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    StrategyMessageLink,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
    StrategyThread,
)


PENDING_STATES = frozenset({"pending", "submitted", "open"})
FILLED_STATES = frozenset({"filled", "active", "partial_closed"})
TERMINAL_ENTRY_STATES = frozenset(
    {"cancelled", "canceled", "rejected", "expired", "failed"}
)
POST_SUBMIT_ADJACENCY_WINDOW = timedelta(minutes=30)
ACTIVE_REVISION_STATES = frozenset(
    {
        "shadow_planned",
        "planned",
        "cancelling_old_entries",
        "old_entries_terminal",
        "rebuilding",
        "reconciling",
        "recovery_required",
    }
)


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
                ExecutionOrderLeg.status.not_in(TERMINAL_ENTRY_STATES),
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
            if (
                leg.last_verified_at is None
                or not exact_identity
                or leg.attribution_status != "verified"
            ):
                return _blocked("revision_submission_state_unknown")
            if state in PENDING_STATES and not leg.pos_id:
                classified.append((leg, "cancel_pending"))
            elif (
                state in FILLED_STATES
                and leg.pos_id
                and leg.attribution_status == "verified"
            ):
                classified.append((leg, "retain_filled"))
            else:
                return _blocked("revision_order_position_state_conflict")
        protections = (
            session.query(PositionProtectionLedger)
            .filter(
                PositionProtectionLedger.execution_binding_id == int(binding.id)
            )
            .order_by(PositionProtectionLedger.id.asc())
            .all()
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
            "contract_spec": order_draft.get("contract_spec"),
            "order_legs": desired_legs,
        }
        assembly_existing = (
            session.query(StrategyRevisionBatch)
            .filter(
                StrategyRevisionBatch.revision_kind == "entry_sizing",
                StrategyRevisionBatch.target_assembly_fingerprint
                == str(assembly.fingerprint),
            )
            .order_by(StrategyRevisionBatch.id.asc())
            .first()
        )
        if assembly_existing is not None:
            if mode == "live" and assembly_existing.status == "shadow_planned":
                assembly_existing.status = "planned"
                assembly_existing.updated_at = now
                session.commit()
            return EntryRevisionPlanResult(
                status=str(assembly_existing.status),
                batch_id=int(assembly_existing.id),
                reason_code=assembly_existing.reason_code,
            )
        fingerprint_payload = {
            "revision_kind": "entry_sizing",
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
            if mode == "live" and existing.status == "shadow_planned":
                existing.status = "planned"
                existing.updated_at = now
                session.commit()
            return EntryRevisionPlanResult(
                status=str(existing.status), batch_id=int(existing.id), reason_code=existing.reason_code
            )
        active = (
            session.query(StrategyRevisionBatch)
            .filter(
                StrategyRevisionBatch.revision_kind == "entry_sizing",
                StrategyRevisionBatch.execution_binding_id == int(binding.id),
                StrategyRevisionBatch.status.in_(ACTIVE_REVISION_STATES),
            )
            .first()
        )
        if active is not None:
            return _blocked("entry_revision_binding_already_active")
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
                .filter(
                    (StrategyRevisionBatch.idempotency_fingerprint == fingerprint)
                    | (
                        (StrategyRevisionBatch.revision_kind == "entry_sizing")
                        & (
                            StrategyRevisionBatch.target_assembly_fingerprint
                            == str(assembly.fingerprint)
                        )
                    )
                )
                .one_or_none()
            )
            if existing is None:
                return _blocked("entry_revision_binding_already_active")
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


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _revised_order_draft(
    *,
    base_evidence: dict[str, Any],
    fragments: list[EntryStrategyFragment],
) -> dict[str, Any] | None:
    base = base_evidence.get("order_draft_snapshot")
    if not isinstance(base, dict) or not isinstance(base.get("order_legs"), list):
        return None
    base_legs = [dict(leg) for leg in base["order_legs"] if isinstance(leg, dict)]
    if not base_legs:
        return None
    configured = _decimal(base_evidence.get("configured_risk_budget_usdt"))
    effective = _decimal(base_evidence.get("effective_risk_budget_usdt"))
    stop = _decimal(base.get("stop_loss"))
    if configured is None or effective is None or stop is None:
        return None
    multipliers: list[Decimal] = []
    allocations: list[Decimal] | None = None
    supplemental: list[Decimal] = []
    for fragment in fragments:
        try:
            payload = json.loads(fragment.payload_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if fragment.fragment_kind == "risk_multiplier":
            value = _decimal(payload.get("risk_multiplier"))
            if value is None or value <= 0 or value > 1:
                return None
            multipliers.append(value)
        elif fragment.fragment_kind == "leg_allocation":
            raw = payload.get("allocations")
            if not isinstance(raw, list):
                return None
            parsed = [_decimal(value) for value in raw]
            if any(value is None or value <= 0 for value in parsed):
                return None
            allocations = [value for value in parsed if value is not None]
        elif fragment.fragment_kind == "supplemental_entry":
            value = _decimal(payload.get("entry_price"))
            if value is None or value <= 0:
                return None
            if value not in supplemental:
                supplemental.append(value)
    distinct_multipliers = tuple(dict.fromkeys(multipliers))
    if len(distinct_multipliers) > 1:
        return None
    revised_effective = (
        configured * distinct_multipliers[0]
        if distinct_multipliers
        else effective
    )
    prices = [Decimal(str(leg["price"])) for leg in base_legs]
    for price in supplemental:
        if price not in prices:
            prices.append(price)
    if len(prices) > 5:
        return None
    if allocations is None:
        equal = Decimal("1") / Decimal(len(prices))
        allocations = [equal for _ in prices]
        allocations[-1] = Decimal("1") - sum(allocations[:-1], Decimal("0"))
    if len(allocations) != len(prices) or sum(allocations) != Decimal("1"):
        return None
    contract_spec = base.get("contract_spec")
    if not isinstance(contract_spec, dict):
        return None
    contract_value = _decimal(contract_spec.get("contract_value"))
    quantity_step = _decimal(contract_spec.get("quantity_step"))
    min_quantity = _decimal(contract_spec.get("min_quantity"))
    if (
        contract_value is None
        or contract_value <= 0
        or quantity_step is None
        or quantity_step <= 0
        or min_quantity is None
        or min_quantity <= 0
    ):
        return None
    revised_legs = []
    for index, (price, allocation) in enumerate(zip(prices, allocations, strict=True)):
        risk_budget = revised_effective * allocation
        raw_quantity = risk_budget / (abs(price - stop) * contract_value)
        quantity = (raw_quantity / quantity_step).to_integral_value(
            rounding=ROUND_DOWN
        ) * quantity_step
        if quantity < min_quantity:
            return None
        source_leg = base_legs[index] if index < len(base_legs) else {}
        revised_legs.append(
            {
                **source_leg,
                "price": float(price) if index >= len(base_legs) else source_leg.get("price"),
                "order_type": str(source_leg.get("order_type") or "limit"),
                "allocation_pct": float(allocation * Decimal("100")),
                "risk_budget_usdt": float(risk_budget),
                "quantity": float(quantity),
                "client_order_id": source_leg.get("client_order_id"),
            }
        )
    return {
        **base,
        "risk_budget_usdt": float(revised_effective),
        "order_legs": revised_legs,
    }


def _post_submit_target_pair(session, *, fragment, source_raw, now):
    """Resolve an explicit target, or wait until source adjacency is stable."""

    thread_ids = {
        int(fragment.target_strategy_thread_id)
        if fragment.target_strategy_thread_id is not None
        else None
    }
    thread_ids.discard(None)
    thread_ids.update(
        int(value)
        for (value,) in session.query(StrategyMessageLink.strategy_thread_id)
        .filter(
            StrategyMessageLink.raw_message_id == int(fragment.raw_message_id),
            StrategyMessageLink.status == "active",
        )
        .all()
    )
    if len(thread_ids) > 1:
        return None, "revision_fragment_target_ambiguous"
    query = (
        session.query(EntryStrategyAssembly, RawMessage)
        .join(RawMessage, RawMessage.id == EntryStrategyAssembly.strategy_raw_message_id)
        .join(SignalCandidate, SignalCandidate.id == EntryStrategyAssembly.signal_candidate_id)
        .join(
            ExecutionBinding,
            ExecutionBinding.strategy_instance_id == EntryStrategyAssembly.strategy_instance_id,
        )
        .join(StrategyLifecycle, StrategyLifecycle.execution_binding_id == ExecutionBinding.id)
        .join(StrategyThread, StrategyThread.current_lifecycle_id == StrategyLifecycle.id)
        .filter(
            RawMessage.chat_id == int(fragment.chat_id),
            SignalCandidate.symbol == str(fragment.symbol).upper(),
            SignalCandidate.side == str(fragment.side).lower(),
            ExecutionBinding.status.in_(("open", "active")),
            StrategyLifecycle.lifecycle_status.in_(("pending_entry", "entered")),
            StrategyThread.status == "active",
        )
    )
    if thread_ids:
        pair = query.filter(StrategyThread.id == next(iter(thread_ids))).one_or_none()
        return (
            (pair, None)
            if pair is not None
            else (None, "revision_fragment_target_mismatch")
        )
    source_time = source_raw.posted_at or source_raw.created_at
    comparable_now = now.replace(tzinfo=None) if source_time.tzinfo is None else now
    if comparable_now - source_time < POST_SUBMIT_ADJACENCY_WINDOW:
        return None, None
    pair = (
        query.filter(
            RawMessage.message_id < int(fragment.message_id),
            RawMessage.message_id >= int(fragment.message_id) - 20,
        )
        .order_by(
            RawMessage.posted_at.desc(),
            RawMessage.message_id.desc(),
            EntryStrategyAssembly.id.desc(),
        )
        .first()
    )
    if pair is None:
        return None, None
    _, strategy_raw = pair
    strategy_time = strategy_raw.posted_at or strategy_raw.created_at
    source_compare = source_time.replace(tzinfo=None) if strategy_time.tzinfo is None else source_time
    if source_compare - strategy_time > POST_SUBMIT_ADJACENCY_WINDOW:
        return None, "revision_fragment_adjacency_expired"
    boundary = (
        session.query(SignalCandidate.id)
        .join(RawMessage, RawMessage.id == SignalCandidate.raw_message_id)
        .filter(
            RawMessage.chat_id == int(fragment.chat_id),
            RawMessage.message_id > int(strategy_raw.message_id),
            RawMessage.message_id <= int(fragment.message_id) + 20,
            SignalCandidate.event_type.in_(
                ("entry_signal", "strategy_revision", "close_signal")
            )
            | SignalCandidate.management_action.in_(
                ("cancel_entry", "cancel", "full_exit", "close")
            ),
        )
        .first()
    )
    if boundary is not None:
        return None, "revision_fragment_target_ambiguous"
    failed_future = (
        session.query(MessageEvidenceVersion.id)
        .join(RawMessage, RawMessage.id == MessageEvidenceVersion.raw_message_id)
        .filter(
            RawMessage.chat_id == int(fragment.chat_id),
            RawMessage.message_id > int(fragment.message_id),
            RawMessage.message_id <= int(fragment.message_id) + 20,
            MessageEvidenceVersion.superseded_at.is_(None),
            MessageEvidenceVersion.extraction_status.in_(("failed", "expired")),
        )
        .first()
    )
    if failed_future is not None:
        return None, "revision_fragment_future_recognition_failed"
    return pair, None


def plan_post_submit_entry_fragment_revisions(
    session_factory: sessionmaker,
    *,
    fragment_ids: tuple[int, ...],
    mode: str,
    planned_at: datetime | None = None,
) -> tuple[EntryRevisionPlanResult, ...]:
    """Turn explicit post-submit fragments into one immutable revision target."""

    if mode == "disabled" or not fragment_ids:
        return ()
    if mode not in {"shadow", "live"}:
        raise ValueError("entry revision mode must be disabled, shadow, or live")
    now = planned_at or datetime.now(UTC)
    with session_factory() as session:
        fragments = (
            session.query(EntryStrategyFragment)
            .filter(
                EntryStrategyFragment.id.in_(fragment_ids),
                EntryStrategyFragment.status == "pending",
            )
            .order_by(EntryStrategyFragment.message_id.asc(), EntryStrategyFragment.id.asc())
            .all()
        )
        if not fragments:
            return ()
        first = fragments[0]
        if any(
            fragment.chat_id != first.chat_id
            or fragment.symbol != first.symbol
            or fragment.side != first.side
            for fragment in fragments
        ):
            return (_blocked("revision_fragment_target_mismatch"),)
        source_raw = session.get(RawMessage, int(first.raw_message_id))
        if source_raw is None:
            return (_blocked("revision_fragment_source_missing"),)
        pair, target_reason = _post_submit_target_pair(
            session, fragment=first, source_raw=source_raw, now=now
        )
        if target_reason is not None:
            return (_blocked(target_reason),)
        if pair is None:
            return ()
        assembly, strategy_raw = pair
        explicit_target = any(
            value is not None
            for value in (
                first.target_strategy_raw_message_id,
                first.target_strategy_thread_id,
                first.target_lifecycle_id,
            )
        ) or (
            session.query(StrategyMessageLink.id)
            .filter(
                StrategyMessageLink.raw_message_id == int(first.raw_message_id),
                StrategyMessageLink.status == "active",
            )
            .first()
            is not None
        )
        if not explicit_target:
            from telegram_kol_research.entry_assembly_admission import (
                assess_entry_assembly_admission,
            )

            admission = assess_entry_assembly_admission(
                session_factory,
                strategy_raw_message_id=int(strategy_raw.id),
                signal_candidate_id=int(assembly.signal_candidate_id),
                mode="live",
                assessed_at=now,
            )
            selected_ids = set(admission.selection.fragment_ids)
            if admission.status != "ready":
                return ()
            if not {int(row.id) for row in fragments}.issubset(selected_ids):
                return (_blocked("revision_fragment_target_ambiguous"),)
        binding = (
            session.query(ExecutionBinding)
            .filter(
                ExecutionBinding.strategy_instance_id == assembly.strategy_instance_id
            )
            .one_or_none()
        )
        if binding is None:
            return ()
        lifecycle = (
            session.query(StrategyLifecycle)
            .filter(StrategyLifecycle.execution_binding_id == int(binding.id))
            .order_by(StrategyLifecycle.id.desc())
            .first()
        )
        thread = (
            session.query(StrategyThread)
            .filter(StrategyThread.current_lifecycle_id == int(lifecycle.id))
            .one_or_none()
            if lifecycle is not None
            else None
        )
        if lifecycle is None or thread is None:
            return ()
        if (
            first.target_strategy_raw_message_id is not None
            and int(first.target_strategy_raw_message_id) != int(strategy_raw.id)
        ) or (
            first.target_lifecycle_id is not None
            and int(first.target_lifecycle_id) != int(lifecycle.id)
        ):
            return (_blocked("revision_fragment_target_mismatch"),)
        if str(binding.symbol).upper() != str(first.symbol).upper() or str(binding.side).lower() != str(first.side).lower():
            return (_blocked("revision_fragment_target_mismatch"),)
        entry_legs = (
            session.query(ExecutionOrderLeg)
            .filter(
                ExecutionOrderLeg.execution_binding_id == int(binding.id),
                ExecutionOrderLeg.purpose == "entry",
                ExecutionOrderLeg.status.not_in(TERMINAL_ENTRY_STATES),
            )
            .order_by(ExecutionOrderLeg.leg_index.asc())
            .all()
        )
        if not entry_legs:
            return ()
        try:
            base_evidence = json.loads(assembly.evidence_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return (_blocked("revision_assembly_evidence_invalid"),)
        previous_generation = (
            session.query(StrategyRevisionBatch)
            .filter(
                StrategyRevisionBatch.revision_kind == "entry_sizing",
                StrategyRevisionBatch.execution_binding_id == int(binding.id),
                StrategyRevisionBatch.status == "succeeded",
            )
            .order_by(StrategyRevisionBatch.id.desc())
            .first()
        )
        base_generation_fingerprint = str(assembly.fingerprint)
        if previous_generation is not None:
            try:
                previous_replacement = json.loads(
                    previous_generation.replacement_json or "{}"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                return (_blocked("revision_previous_generation_invalid"),)
            prior_draft = base_evidence.get("order_draft_snapshot")
            if not isinstance(prior_draft, dict):
                return (_blocked("revision_previous_generation_invalid"),)
            base_evidence = {
                **base_evidence,
                "effective_risk_budget_usdt": previous_replacement.get(
                    "risk_budget_usdt"
                ),
                "order_draft_snapshot": {
                    **prior_draft,
                    **previous_replacement,
                },
            }
            base_generation_fingerprint = str(
                previous_generation.target_assembly_fingerprint
            )
        revised_draft = _revised_order_draft(
            base_evidence=base_evidence, fragments=fragments
        )
        if revised_draft is None:
            return (_blocked("revision_fragment_economics_invalid"),)
        classified: list[tuple[ExecutionOrderLeg, str]] = []
        for leg in entry_legs:
            state = str(leg.status or "").lower()
            if (
                leg.last_verified_at is None
                or not (leg.order_id or leg.client_order_id)
                or leg.attribution_status != "verified"
            ):
                return (_blocked("revision_submission_state_unknown"),)
            if state in PENDING_STATES and not leg.pos_id:
                classified.append((leg, "cancel_pending"))
            elif (
                state in FILLED_STATES
                and leg.pos_id
                and leg.attribution_status == "verified"
            ):
                classified.append((leg, "retain_filled"))
            else:
                return (_blocked("revision_order_position_state_conflict"),)
        target_fingerprint = hashlib.sha256(
            _canonical_json(
                {
                    "base_assembly_fingerprint": assembly.fingerprint,
                    "base_revision_generation": base_generation_fingerprint,
                    "fragment_fingerprints": [row.fingerprint for row in fragments],
                    "order_draft_snapshot": revised_draft,
                }
            ).encode("utf-8")
        ).hexdigest()
        existing = (
            session.query(StrategyRevisionBatch)
            .filter(
                StrategyRevisionBatch.revision_kind == "entry_sizing",
                StrategyRevisionBatch.target_assembly_fingerprint == target_fingerprint,
            )
            .one_or_none()
        )
        if existing is not None:
            if mode == "live" and existing.status == "shadow_planned":
                existing.status = "planned"
                existing.updated_at = now
                for fragment in fragments:
                    fragment.status = "consumed"
                    fragment.source_relationship = "after_strategy"
                    fragment.target_strategy_raw_message_id = int(strategy_raw.id)
                    fragment.target_strategy_thread_id = int(thread.id)
                    fragment.target_lifecycle_id = int(lifecycle.id)
                    fragment.consumed_at = now
                    fragment.updated_at = now
                session.commit()
            return (
                EntryRevisionPlanResult(
                    str(existing.status), int(existing.id), existing.reason_code
                ),
            )
        active = (
            session.query(StrategyRevisionBatch)
            .filter(
                StrategyRevisionBatch.revision_kind == "entry_sizing",
                StrategyRevisionBatch.execution_binding_id == int(binding.id),
                StrategyRevisionBatch.status.in_(ACTIVE_REVISION_STATES),
            )
            .first()
        )
        if active is not None:
            return (_blocked("entry_revision_binding_already_active"),)
        target_snapshot = {
            "chat_id": int(thread.chat_id),
            "strategy_thread_id": int(thread.id),
            "target_lifecycle_id": int(lifecycle.id),
            "execution_binding_id": int(binding.id),
            "strategy_instance_id": str(binding.strategy_instance_id),
            "base_assembly_id": int(assembly.id),
            "base_assembly_fingerprint": str(assembly.fingerprint),
            "base_revision_generation": base_generation_fingerprint,
            "fragment_ids": [int(row.id) for row in fragments],
            "entry_legs": [
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
            ],
            "protection": [
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
                for row in (
                    session.query(PositionProtectionLedger)
                    .filter(
                        PositionProtectionLedger.execution_binding_id
                        == int(binding.id)
                    )
                    .order_by(PositionProtectionLedger.id.asc())
                    .all()
                )
            ],
        }
        replacement = {
            "assembly_fingerprint": target_fingerprint,
            "instrument_id": revised_draft.get("instrument_id"),
            "stop_loss": revised_draft.get("stop_loss"),
            "risk_budget_usdt": revised_draft.get("risk_budget_usdt"),
            "contract_spec": revised_draft.get("contract_spec"),
            "order_legs": revised_draft.get("order_legs"),
        }
        fingerprint = hashlib.sha256(
            _canonical_json(
                {
                    "revision_kind": "entry_sizing",
                    "target_snapshot": target_snapshot,
                    "replacement": replacement,
                }
            ).encode("utf-8")
        ).hexdigest()
        batch = StrategyRevisionBatch(
            idempotency_fingerprint=fingerprint,
            raw_message_id=int(first.raw_message_id),
            strategy_thread_id=int(thread.id),
            target_lifecycle_id=int(lifecycle.id),
            execution_binding_id=int(binding.id),
            revision_kind="entry_sizing",
            target_assembly_id=int(assembly.id),
            target_assembly_fingerprint=target_fingerprint,
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
                .filter(
                    StrategyRevisionBatch.revision_kind == "entry_sizing",
                    StrategyRevisionBatch.target_assembly_fingerprint
                    == target_fingerprint,
                )
                .one_or_none()
            )
            if existing is None:
                return (_blocked("entry_revision_binding_already_active"),)
            return (
                EntryRevisionPlanResult(
                    str(existing.status), int(existing.id), existing.reason_code
                ),
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
        if mode == "live":
            for fragment in fragments:
                fragment.status = "consumed"
                fragment.source_relationship = "after_strategy"
                fragment.target_strategy_raw_message_id = int(strategy_raw.id)
                fragment.target_strategy_thread_id = int(thread.id)
                fragment.target_lifecycle_id = int(lifecycle.id)
                fragment.consumed_at = now
                fragment.updated_at = now
        session.commit()
        return (EntryRevisionPlanResult(str(batch.status), int(batch.id)),)

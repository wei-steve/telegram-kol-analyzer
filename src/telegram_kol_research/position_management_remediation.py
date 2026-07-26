"""Fingerprint-guarded remediation for missed position-management instructions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.execution_bindings import _load_reconcile_snapshot
from telegram_kol_research.management_directives import resolve_management_directive
from telegram_kol_research.management_scope import (
    ManagementScopeError,
    resolve_management_scope_in_session,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    StrategyManagementBatch,
)
from telegram_kol_research.remediation_snapshot import (
    remediation_snapshot_payload,
    stable_position_payload,
)
from telegram_kol_research.strategy_management_executor import (
    execute_management_batch,
)
from telegram_kol_research.strategy_management_batches import load_management_batch
from telegram_kol_research.strategy_management_planner import (
    management_target_fingerprint,
    plan_strategy_management_batch,
)
from telegram_kol_research.trading_settings import load_trading_settings


@dataclass(frozen=True, slots=True)
class PositionRemediationAction:
    action_id: str
    action_kind: str
    raw_message_id: int | None
    lifecycle_id: int
    strategy_instance_id: str
    pos_ids: tuple[str, ...]
    expected_effect: dict[str, Any]
    evidence: dict[str, Any]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PositionRemediationStep:
    strategy_instance_id: str
    lifecycle_id: int
    execution_binding_id: int
    raw_message_id: int
    instruction_item_id: int | None
    candidate_id: int
    sequence: int
    posted_at: datetime
    action_kind: str
    state: str
    reason: str | None
    management_batch_id: int | None
    batch_status: str | None
    action: PositionRemediationAction | None


@dataclass(frozen=True, slots=True)
class PositionRemediationChain:
    strategy_instance_id: str
    lifecycle_id: int
    execution_binding_id: int
    steps: tuple[PositionRemediationStep, ...]
    conflicts: tuple[dict[str, Any], ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PositionRemediationPlan:
    snapshot_fingerprint: str
    actions: tuple[PositionRemediationAction, ...]
    conflicts: tuple[dict[str, Any], ...]
    chains: tuple[PositionRemediationChain, ...]


@dataclass(frozen=True, slots=True)
class RemediationApplyResult:
    status: str
    action_id: str
    batch_id: int | None
    result: dict[str, Any]


def build_position_management_remediation_plan(
    session_factory: sessionmaker,
    *,
    deepcoin_client,
    now: datetime | None = None,
) -> PositionRemediationPlan:
    """Build a read-only plan from one coherent exchange snapshot."""

    with session_factory() as session:
        instruments = {
            f"{str(symbol or '').upper()}-USDT-SWAP"
            for (symbol,) in session.query(ExecutionBinding.symbol)
            .filter(ExecutionBinding.venue == "deepcoin")
            .all()
            if str(symbol or "").strip()
        }
    snapshot = _load_reconcile_snapshot(
        deepcoin_client,
        instruments=instruments,
    )
    snapshot_payload = _snapshot_payload(snapshot)
    exchange_snapshot_fingerprint = _fingerprint(snapshot_payload)
    incomplete_observations = [
        dict(observation)
        for observation in snapshot.pending_tpsl_observations
        if not bool(observation.get("complete"))
    ]
    if snapshot.errors or incomplete_observations:
        conflicts = (
            {
                "reason": "exchange_snapshot_incomplete",
                "errors": dict(snapshot.errors),
                "incomplete_pending_tpsl": incomplete_observations,
            },
        )
        return PositionRemediationPlan(
            snapshot_fingerprint=_fingerprint(
                {"exchange": snapshot_payload, "actions": [], "conflicts": conflicts}
            ),
            actions=(),
            conflicts=conflicts,
            chains=(),
        )
    live_position_rows = [
        stable_position_payload(dict(row))
        for row in snapshot.positions
        if _first_text(row, "posId", "pos_id", "id")
    ]
    live_positions = {
        str(pos_id): dict(row)
        for row in live_position_rows
        if (pos_id := _first_text(row, "posId", "pos_id", "id"))
    }

    actions: list[PositionRemediationAction] = []
    static_steps: list[PositionRemediationStep] = []
    conflicts: list[dict[str, Any]] = []
    with session_factory() as session:
        candidates = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.parse_source == "mimo_authoritative")
            .filter(SignalCandidate.review_status != "approved_remediation")
            .filter(
                SignalCandidate.event_type.in_(("close_signal", "position_update"))
            )
            .order_by(SignalCandidate.raw_message_id, SignalCandidate.id)
            .all()
        )
        for candidate in candidates:
            item = (
                session.query(MessageInstructionItem)
                .filter(
                    MessageInstructionItem.signal_candidate_id == candidate.id,
                    MessageInstructionItem.retired_at.is_(None),
                )
                .one_or_none()
            )
            if item is not None and not _item_requires_remediation(item):
                continue
            raw_message = session.get(RawMessage, candidate.raw_message_id)
            if raw_message is None:
                continue
            identity_conflict = _candidate_item_strategy_conflict(
                session=session,
                candidate=candidate,
                item=item,
            )
            if identity_conflict is not None:
                lifecycle, binding, target_strategy_instance_id = identity_conflict
                conflict = {
                    "raw_message_id": int(raw_message.id),
                    "candidate_id": int(candidate.id),
                    "lifecycle_id": int(lifecycle.id),
                    "strategy_instance_id": str(binding.strategy_instance_id),
                    "target_strategy_instance_id": target_strategy_instance_id,
                    "reason": "candidate_item_strategy_mismatch",
                }
                conflicts.append(conflict)
                static_steps.append(
                    _batch_backed_static_step(
                        binding=binding,
                        lifecycle=lifecycle,
                        raw_message=raw_message,
                        candidate=candidate,
                        item=item,
                        action_kind=_candidate_action_kind(candidate),
                        state="blocked",
                        reason="candidate_item_strategy_mismatch",
                    )
                )
                continue
            decision = _candidate_decision(candidate)
            try:
                directive = resolve_management_directive(
                    text=raw_message.text or "",
                    lifecycle_event=decision,
                )
                targets = resolve_management_scope_in_session(
                    session,
                    raw_message=raw_message,
                    directive=directive,
                    explicit_target_lifecycle_id=candidate.target_lifecycle_id,
                    reply_target_lifecycle_id=None,
                )
            except (ManagementScopeError, ValueError) as exc:
                conflict = {
                    "raw_message_id": int(raw_message.id),
                    "candidate_id": int(candidate.id),
                    "reason": str(exc),
                }
                explicit_context = _load_candidate_conflict_context(
                    session=session,
                    candidate=candidate,
                    item=item,
                )
                if explicit_context is not None:
                    lifecycle, binding = explicit_context
                    conflict.update(
                        {
                            "lifecycle_id": int(lifecycle.id),
                            "strategy_instance_id": str(
                                binding.strategy_instance_id
                            ),
                        }
                    )
                    static_steps.append(
                        _batch_backed_static_step(
                            binding=binding,
                            lifecycle=lifecycle,
                            raw_message=raw_message,
                            candidate=candidate,
                            item=item,
                            action_kind=_candidate_action_kind(candidate),
                            state="blocked",
                            reason=str(exc),
                        )
                    )
                conflicts.append(conflict)
                continue
            for target in targets:
                lifecycle = session.get(StrategyLifecycle, target.lifecycle_id)
                binding = (
                    session.get(ExecutionBinding, lifecycle.execution_binding_id)
                    if lifecycle is not None
                    and lifecycle.execution_binding_id is not None
                    else None
                )
                if (
                    lifecycle is None
                    or binding is None
                    or str(binding.strategy_instance_id or "")
                    != target.strategy_instance_id
                ):
                    conflict = {
                        "raw_message_id": int(raw_message.id),
                        "candidate_id": int(candidate.id),
                        "lifecycle_id": target.lifecycle_id,
                        "reason": "target_strategy_binding_not_verified",
                    }
                    if (
                        lifecycle is not None
                        and binding is not None
                        and str(binding.strategy_instance_id or "").strip()
                    ):
                        conflict["strategy_instance_id"] = str(
                            binding.strategy_instance_id
                        )
                        static_steps.append(
                            _batch_backed_static_step(
                                binding=binding,
                                lifecycle=lifecycle,
                                raw_message=raw_message,
                                candidate=candidate,
                                item=item,
                                action_kind=directive.intent,
                                state="blocked",
                                reason="target_strategy_binding_not_verified",
                            )
                        )
                    conflicts.append(conflict)
                    continue
                existing_batches = (
                    session.query(StrategyManagementBatch)
                    .filter(
                        StrategyManagementBatch.raw_message_id == raw_message.id,
                        StrategyManagementBatch.target_lifecycle_id
                        == target.lifecycle_id,
                    )
                    .order_by(StrategyManagementBatch.id.desc())
                    .all()
                )
                existing_batches = [
                    batch
                    for batch in existing_batches
                    if _batch_matches_candidate_action(
                        batch=batch,
                        candidate=candidate,
                        directive_intent=directive.intent,
                        lifecycle_id=int(lifecycle.id),
                    )
                ]
                if existing_batches and not _batch_candidate_association_is_unique(
                    session=session,
                    candidate=candidate,
                ):
                    conflict = {
                        "raw_message_id": int(raw_message.id),
                        "candidate_id": int(candidate.id),
                        "lifecycle_id": int(lifecycle.id),
                        "strategy_instance_id": str(binding.strategy_instance_id),
                        "reason": "management_batch_candidate_ambiguous",
                    }
                    conflicts.append(conflict)
                    static_steps.append(
                        _batch_backed_static_step(
                            binding=binding,
                            lifecycle=lifecycle,
                            raw_message=raw_message,
                            candidate=candidate,
                            item=item,
                            action_kind=directive.intent,
                            state="blocked",
                            reason="management_batch_candidate_ambiguous",
                            batch=existing_batches[0],
                        )
                    )
                    continue
                terminal_batch = (
                    session.query(StrategyManagementBatch)
                    .filter(
                        StrategyManagementBatch.target_lifecycle_id
                        == target.lifecycle_id,
                        StrategyManagementBatch.effective_action == "full_exit",
                        StrategyManagementBatch.status == "succeeded",
                    )
                    .order_by(StrategyManagementBatch.id)
                    .first()
                )
                if terminal_batch is not None:
                    is_terminal_source = (
                        int(terminal_batch.raw_message_id) == int(raw_message.id)
                        and _terminal_batch_resolves_candidate(
                            batch=terminal_batch,
                            candidate=candidate,
                            directive_intent=directive.intent,
                            lifecycle_id=int(lifecycle.id),
                        )
                        and _batch_candidate_association_is_unique(
                            session=session,
                            candidate=candidate,
                        )
                    )
                    static_steps.append(
                        PositionRemediationStep(
                            strategy_instance_id=str(binding.strategy_instance_id),
                            lifecycle_id=int(lifecycle.id),
                            execution_binding_id=int(binding.id),
                            raw_message_id=int(raw_message.id),
                            instruction_item_id=(
                                int(item.id) if item is not None else None
                            ),
                            candidate_id=int(candidate.id),
                            sequence=int(item.sequence) if item is not None else 0,
                            posted_at=raw_message.posted_at,
                            action_kind=(
                                "full_exit"
                                if is_terminal_source
                                else directive.intent
                            ),
                            state=(
                                "resolved"
                                if is_terminal_source
                                else "terminally_skipped"
                            ),
                            reason=(
                                "confirmed_full_exit"
                                if is_terminal_source
                                else "old_lifecycle_already_fully_exited"
                            ),
                            management_batch_id=int(terminal_batch.id),
                            batch_status=str(terminal_batch.status),
                            action=None,
                        )
                    )
                    continue
                active_batch = next(
                    (
                        batch
                        for batch in existing_batches
                        if batch.status
                        in {
                            "ready",
                            "executing",
                            "reserved",
                            "submitted",
                            "reconciling",
                            "protection_ready",
                        }
                    ),
                    None,
                )
                if active_batch is not None:
                    static_steps.append(
                        _batch_backed_static_step(
                            binding=binding,
                            lifecycle=lifecycle,
                            raw_message=raw_message,
                            candidate=candidate,
                            item=item,
                            action_kind=directive.intent,
                            state="waiting_for_reconciliation",
                            reason=f"management_batch_{active_batch.status}",
                            batch=active_batch,
                        )
                    )
                    continue
                succeeded_batch = next(
                    (
                        batch
                        for batch in existing_batches
                        if batch.status == "succeeded"
                    ),
                    None,
                )
                if succeeded_batch is not None:
                    static_steps.append(
                        _batch_backed_static_step(
                            binding=binding,
                            lifecycle=lifecycle,
                            raw_message=raw_message,
                            candidate=candidate,
                            item=item,
                            action_kind=directive.intent,
                            state="resolved",
                            reason="management_batch_succeeded",
                            batch=succeeded_batch,
                        )
                    )
                    continue
                unresolved_batch = next(
                    (
                        batch
                        for batch in existing_batches
                        if batch.status
                        in {
                            "partial_failed",
                            "submit_unknown",
                            "recovery_required",
                        }
                    ),
                    None,
                )
                if unresolved_batch is not None:
                    static_steps.append(
                        _batch_backed_static_step(
                            binding=binding,
                            lifecycle=lifecycle,
                            raw_message=raw_message,
                            candidate=candidate,
                            item=item,
                            action_kind=directive.intent,
                            state="blocked",
                            reason="existing_management_batch_unresolved",
                            batch=unresolved_batch,
                        )
                    )
                    conflicts.append(
                        {
                            "raw_message_id": int(raw_message.id),
                            "candidate_id": int(candidate.id),
                            "lifecycle_id": target.lifecycle_id,
                            "management_batch_id": int(unresolved_batch.id),
                            "strategy_instance_id": str(
                                binding.strategy_instance_id
                            ),
                            "reason": "existing_management_batch_unresolved",
                        }
                    )
                    continue
                entry_legs = (
                    session.query(ExecutionOrderLeg)
                    .filter(
                        ExecutionOrderLeg.execution_binding_id == binding.id,
                        ExecutionOrderLeg.purpose == "entry",
                        ExecutionOrderLeg.attribution_status == "verified",
                        ExecutionOrderLeg.pos_id.is_not(None),
                        ExecutionOrderLeg.status.in_(
                            ("active", "open", "filled", "partial_closed")
                        ),
                    )
                    .order_by(ExecutionOrderLeg.leg_index, ExecutionOrderLeg.id)
                    .all()
                )
                pos_ids = tuple(
                    str(leg.pos_id)
                    for leg in entry_legs
                    if str(leg.pos_id) in live_positions
                )
                exact_live_identity = _entry_positions_match_exact_live_identity(
                    binding=binding,
                    entry_legs=entry_legs,
                    live_rows=live_position_rows,
                )
                if (
                    not pos_ids
                    or len(pos_ids) != len(entry_legs)
                    or not exact_live_identity
                ):
                    conflicts.append(
                        {
                            "raw_message_id": int(raw_message.id),
                            "candidate_id": int(candidate.id),
                            "lifecycle_id": target.lifecycle_id,
                            "strategy_instance_id": str(
                                binding.strategy_instance_id
                            ),
                            "reason": (
                                "late_fill_identity_not_exact"
                                if directive.intent == "cancel_entry"
                                else "target_live_position_not_exact"
                            ),
                        }
                    )
                    static_steps.append(
                        _batch_backed_static_step(
                            binding=binding,
                            lifecycle=lifecycle,
                            raw_message=raw_message,
                            candidate=candidate,
                            item=item,
                            action_kind=directive.intent,
                            state="blocked",
                            reason=(
                                "late_fill_identity_not_exact"
                                if directive.intent == "cancel_entry"
                                else "target_live_position_not_exact"
                            ),
                        )
                    )
                    continue
                effective_intent = (
                    "full_exit"
                    if directive.intent == "cancel_entry"
                    else directive.intent
                )
                late_fill_conversion = directive.intent == "cancel_entry"
                expected_effect = {
                    "fraction": directive.fraction,
                    "stop_loss": directive.stop_loss,
                    "cancel_deferred_entries": directive.cancel_deferred_entries,
                    "preserve_quantity": effective_intent
                    in {"move_stop_to_break_even", "adjust_stop_loss"},
                }
                evidence = {
                    "candidate_id": int(candidate.id),
                    "instruction_item_id": int(item.id) if item is not None else None,
                    "instruction_status": item.status if item is not None else "missing",
                    "source_chat_id": int(raw_message.chat_id),
                    "source_message_id": int(raw_message.message_id),
                    "source_posted_at": raw_message.posted_at,
                    "instruction_sequence": int(item.sequence) if item is not None else 0,
                    "scope_source": target.scope_source,
                    "reason_code": directive.reason_code,
                    "original_action_kind": directive.intent,
                    "late_fill_conversion": late_fill_conversion,
                    "exchange_snapshot_fingerprint": exchange_snapshot_fingerprint,
                    "instrument_scope": sorted(instruments),
                    "execution_binding_id": int(binding.id),
                    "execution_order_leg_ids": [int(leg.id) for leg in entry_legs],
                    "positions": [live_positions[pos_id] for pos_id in pos_ids],
                    "predecessor_signature": _predecessor_signature(
                        session=session,
                        strategy_instance_id=str(binding.strategy_instance_id),
                        current_raw_message=raw_message,
                        current_candidate=candidate,
                        current_item=item,
                        current_effective_intent=effective_intent,
                    ),
                }
                action_core = {
                    "action_kind": effective_intent,
                    "raw_message_id": int(raw_message.id),
                    "lifecycle_id": int(lifecycle.id),
                    "strategy_instance_id": str(binding.strategy_instance_id),
                    "pos_ids": list(pos_ids),
                    "expected_effect": expected_effect,
                    "evidence": evidence,
                }
                fingerprint = _fingerprint(action_core)
                actions.append(
                    PositionRemediationAction(
                        action_id=_fingerprint(
                            {
                                "raw_message_id": raw_message.id,
                                "candidate_id": candidate.id,
                                "lifecycle_id": lifecycle.id,
                                "action_kind": effective_intent,
                            }
                        )[:20],
                        action_kind=effective_intent,
                        raw_message_id=int(raw_message.id),
                        lifecycle_id=int(lifecycle.id),
                        strategy_instance_id=str(binding.strategy_instance_id),
                        pos_ids=pos_ids,
                        expected_effect=expected_effect,
                        evidence=evidence,
                        fingerprint=fingerprint,
                    )
                )

    chains = _build_remediation_chains(
        actions,
        static_steps=static_steps,
        conflicts=conflicts,
    )
    ordered_actions = tuple(
        step.action
        for chain in chains
        for step in chain.steps
        if step.state == "ready_for_approval" and step.action is not None
    )
    ordered_conflicts = tuple(
        sorted(
            conflicts,
            key=lambda row: (
                int(row.get("raw_message_id") or 0),
                int(row.get("candidate_id") or 0),
                str(row.get("reason") or ""),
            ),
        )
    )
    return PositionRemediationPlan(
        snapshot_fingerprint=_fingerprint(
            {
                "exchange": snapshot_payload,
                "actions": [asdict(action) for action in ordered_actions],
                "conflicts": list(ordered_conflicts),
                "chains": [asdict(chain) for chain in chains],
            }
        ),
        actions=ordered_actions,
        conflicts=ordered_conflicts,
        chains=chains,
    )


def _build_remediation_chains(
    actions: list[PositionRemediationAction],
    *,
    static_steps: list[PositionRemediationStep] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
) -> tuple[PositionRemediationChain, ...]:
    grouped: dict[str, list[PositionRemediationStep]] = {}
    for action in actions:
        grouped.setdefault(action.strategy_instance_id, []).append(
            PositionRemediationStep(
                strategy_instance_id=action.strategy_instance_id,
                lifecycle_id=action.lifecycle_id,
                execution_binding_id=int(action.evidence["execution_binding_id"]),
                raw_message_id=int(action.raw_message_id),
                instruction_item_id=action.evidence.get("instruction_item_id"),
                candidate_id=int(action.evidence["candidate_id"]),
                sequence=int(action.evidence.get("instruction_sequence") or 0),
                posted_at=action.evidence["source_posted_at"],
                action_kind=action.action_kind,
                state="unclassified",
                reason=None,
                management_batch_id=None,
                batch_status=None,
                action=action,
            )
        )
    for step in static_steps or []:
        grouped.setdefault(step.strategy_instance_id, []).append(step)
    chains: list[PositionRemediationChain] = []
    for strategy_instance_id, grouped_steps in grouped.items():
        ordered = sorted(
            grouped_steps,
            key=lambda step: (
                step.posted_at,
                step.raw_message_id,
                step.sequence,
                step.candidate_id,
            ),
        )
        steps_list: list[PositionRemediationStep] = []
        ready_assigned = False
        predecessor_blocking = False
        for step in ordered:
            if step.action is None:
                steps_list.append(step)
                if step.state not in {"resolved", "terminally_skipped"}:
                    predecessor_blocking = True
                continue
            state = (
                "ready_for_approval"
                if not ready_assigned and not predecessor_blocking
                else "waiting_for_predecessor"
            )
            predecessor_state = [
                {
                    "raw_message_id": prior.raw_message_id,
                    "instruction_item_id": prior.instruction_item_id,
                    "candidate_id": prior.candidate_id,
                    "state": prior.state,
                    "reason": prior.reason,
                    "management_batch_id": prior.management_batch_id,
                    "batch_status": prior.batch_status,
                }
                for prior in steps_list
            ]
            classified_action = (
                replace(
                    step.action,
                    fingerprint=_fingerprint(
                        {
                            "action_fingerprint": step.action.fingerprint,
                            "predecessors": predecessor_state,
                        }
                    ),
                )
                if state == "ready_for_approval"
                else replace(step.action, fingerprint="not-executable")
            )
            steps_list.append(
                PositionRemediationStep(
                    strategy_instance_id=step.strategy_instance_id,
                    lifecycle_id=step.lifecycle_id,
                    execution_binding_id=step.execution_binding_id,
                    raw_message_id=step.raw_message_id,
                    instruction_item_id=step.instruction_item_id,
                    candidate_id=step.candidate_id,
                    sequence=step.sequence,
                    posted_at=step.posted_at,
                    action_kind=step.action_kind,
                    state=state,
                    reason=(
                        None
                        if state == "ready_for_approval"
                        else "predecessor_not_resolved"
                    ),
                    management_batch_id=None,
                    batch_status=None,
                    action=classified_action,
                )
            )
            ready_assigned = True
            predecessor_blocking = True
        steps = tuple(steps_list)
        first = ordered[0]
        chain_conflicts = tuple(
            conflict
            for conflict in conflicts or []
            if conflict.get("strategy_instance_id") == strategy_instance_id
        )
        chains.append(
            PositionRemediationChain(
                strategy_instance_id=strategy_instance_id,
                lifecycle_id=first.lifecycle_id,
                execution_binding_id=first.execution_binding_id,
                steps=steps,
                conflicts=chain_conflicts,
                fingerprint=_fingerprint(
                    {
                        "strategy_instance_id": strategy_instance_id,
                        "steps": [asdict(step) for step in steps],
                        "conflicts": list(chain_conflicts),
                    }
                ),
            )
        )
    return tuple(
        sorted(chains, key=lambda chain: (chain.strategy_instance_id, chain.lifecycle_id))
    )


def _batch_backed_static_step(
    *,
    binding: ExecutionBinding,
    lifecycle: StrategyLifecycle,
    raw_message: RawMessage,
    candidate: SignalCandidate,
    item: MessageInstructionItem | None,
    action_kind: str,
    state: str,
    reason: str,
    batch: StrategyManagementBatch | None = None,
) -> PositionRemediationStep:
    return PositionRemediationStep(
        strategy_instance_id=str(binding.strategy_instance_id),
        lifecycle_id=int(lifecycle.id),
        execution_binding_id=int(binding.id),
        raw_message_id=int(raw_message.id),
        instruction_item_id=int(item.id) if item is not None else None,
        candidate_id=int(candidate.id),
        sequence=int(item.sequence) if item is not None else 0,
        posted_at=raw_message.posted_at,
        action_kind=action_kind,
        state=state,
        reason=reason,
        management_batch_id=int(batch.id) if batch is not None else None,
        batch_status=str(batch.status) if batch is not None else None,
        action=None,
    )


def apply_position_management_remediation_action(
    session_factory: sessionmaker,
    *,
    deepcoin_client,
    action_id: str,
    expected_fingerprint: str,
    now: datetime,
    contract_spec_provider=None,
) -> RemediationApplyResult:
    """Rebuild and apply exactly one reviewed action through the normal path."""

    if not action_id or not expected_fingerprint:
        raise ValueError("action_id and expected_fingerprint are required")
    settings = load_trading_settings(session_factory)
    if not settings.live_management_execution_enabled:
        raise ValueError("live management execution is disabled")
    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=deepcoin_client,
        now=now,
    )
    action = _select_executable_action(plan, action_id=action_id)
    if action.fingerprint != expected_fingerprint:
        raise ValueError("remediation action fingerprint mismatch")
    approved_chain = _chain_for_action(plan, action_id=action.action_id)
    candidate_id = _project_canonical_remediation_candidate(
        session_factory,
        action=action,
    )
    result = plan_strategy_management_batch(
        session_factory,
        raw_message_id=int(action.raw_message_id),
        candidate_id=candidate_id,
        deepcoin_client=deepcoin_client,
        contract_spec_provider=contract_spec_provider,
        planned_at=now,
        execution_mode="disabled",
    )
    if result.batch is None or (
        result.batch.status != "blocked"
        or result.batch.reason_code != "management_disabled_plan_only"
    ):
        raise ValueError(
            f"remediation planning did not become ready:{result.reason_code}"
        )
    _require_batch_matches_confirmed_action(action=action, batch=result.batch)
    refreshed_plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=deepcoin_client,
        now=now,
    )
    refreshed_action = _select_executable_action(
        refreshed_plan,
        action_id=action.action_id,
    )
    refreshed_chain = _chain_for_action(
        refreshed_plan,
        action_id=action.action_id,
    )
    if (
        refreshed_action.fingerprint != action.fingerprint
        or refreshed_chain.fingerprint != approved_chain.fingerprint
    ):
        raise ValueError("remediation chain changed before promotion")
    if not load_trading_settings(
        session_factory
    ).live_management_execution_enabled:
        raise ValueError("live management execution was disabled before write")
    _require_exchange_snapshot_fingerprint(
        deepcoin_client=deepcoin_client,
        action=action,
    )
    with session_factory() as session:
        stored_batch = session.get(StrategyManagementBatch, result.batch.id)
        if (
            stored_batch is None
            or stored_batch.status != "blocked"
            or stored_batch.execution_mode != "disabled"
            or stored_batch.reason_code != "management_disabled_plan_only"
        ):
            raise ValueError("remediation batch changed before promotion")
        source_candidate = session.get(
            SignalCandidate,
            int(action.evidence["candidate_id"]),
        )
        source_raw = session.get(RawMessage, int(action.raw_message_id))
        source_item = (
            session.get(
                MessageInstructionItem,
                int(action.evidence["instruction_item_id"]),
            )
            if action.evidence.get("instruction_item_id") is not None
            else None
        )
        if source_candidate is None or source_raw is None:
            raise ValueError("remediation source changed before promotion")
        current_predecessor_signature = _predecessor_signature(
            session=session,
            strategy_instance_id=action.strategy_instance_id,
            current_raw_message=source_raw,
            current_candidate=source_candidate,
            current_item=source_item,
            current_effective_intent=action.action_kind,
            excluded_batch_id=int(result.batch.id),
        )
        if current_predecessor_signature != action.evidence.get(
            "predecessor_signature"
        ):
            raise ValueError("remediation predecessors changed before promotion")
        target_snapshot = json.loads(stored_batch.target_snapshot_json)
        target_snapshot["remediation_confirmation"] = {
            "action_id": action.action_id,
            "action_fingerprint": action.fingerprint,
            "exchange_snapshot_fingerprint": action.evidence[
                "exchange_snapshot_fingerprint"
            ],
            "instrument_scope": list(action.evidence["instrument_scope"]),
        }
        stored_batch.target_snapshot_json = json.dumps(
            target_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        stored_batch.target_fingerprint = management_target_fingerprint(
            target_snapshot
        )
        stored_batch.execution_mode = "live"
        stored_batch.status = "ready"
        stored_batch.reason_code = None
        stored_batch.updated_at = now
        session.commit()
    promoted_batch = load_management_batch(session_factory, result.batch.id)
    execution_result = execute_management_batch(
        session_factory,
        batch_id=promoted_batch.id,
        deepcoin_client=deepcoin_client,
        executed_at=now,
    )
    return RemediationApplyResult(
        status=str(execution_result.get("status") or "unknown"),
        action_id=action.action_id,
        batch_id=promoted_batch.id,
        result=execution_result,
    )


def _select_executable_action(
    plan: PositionRemediationPlan,
    *,
    action_id: str,
) -> PositionRemediationAction:
    matches = [action for action in plan.actions if action.action_id == action_id]
    if len(matches) == 1:
        return matches[0]
    waiting_match = any(
        step.action is not None
        and step.action.action_id == action_id
        and step.state != "ready_for_approval"
        for chain in plan.chains
        for step in chain.steps
    )
    if waiting_match:
        raise ValueError("remediation action is not executable chain head")
    raise ValueError("remediation action not found or not unique")


def _chain_for_action(
    plan: PositionRemediationPlan,
    *,
    action_id: str,
) -> PositionRemediationChain:
    matches = [
        chain
        for chain in plan.chains
        if any(
            step.action is not None and step.action.action_id == action_id
            for step in chain.steps
        )
    ]
    if len(matches) != 1:
        raise ValueError("remediation action chain not found or not unique")
    return matches[0]


def _project_canonical_remediation_candidate(
    session_factory: sessionmaker,
    *,
    action: PositionRemediationAction,
) -> int:
    with session_factory() as session:
        source = session.get(SignalCandidate, int(action.evidence["candidate_id"]))
        if source is None or source.raw_message_id != int(action.raw_message_id):
            raise ValueError("remediation source candidate changed")
        expected_event_type = (
            "close_signal" if action.action_kind == "full_exit" else "position_update"
        )
        expected_fraction = action.expected_effect.get("fraction")
        expected_stop = action.expected_effect.get("stop_loss")
        if (
            source.event_type == expected_event_type
            and source.target_lifecycle_id == action.lifecycle_id
            and str(source.management_action or "") == action.action_kind
            and source.management_fraction == expected_fraction
            and (source.stop_loss_text or None) == (expected_stop or None)
        ):
            return int(source.id)
        projected = SignalCandidate(
            raw_message_id=int(action.raw_message_id),
            symbol=source.symbol,
            side=source.side,
            event_type=expected_event_type,
            target_lifecycle_id=action.lifecycle_id,
            management_action=action.action_kind,
            management_fraction=expected_fraction,
            recognition_generation=f"remediation:{action.fingerprint[:32]}",
            entry_text=source.entry_text,
            stop_loss_text=expected_stop,
            take_profit_text=source.take_profit_text,
            leverage_text=source.leverage_text,
            parse_source="mimo_authoritative",
            confidence=source.confidence,
            review_status="approved_remediation",
            review_note=f"fingerprinted remediation action {action.action_id}",
        )
        session.add(projected)
        session.commit()
        return int(projected.id)


def _require_batch_matches_confirmed_action(*, action, batch) -> None:
    if (
        batch.target_lifecycle_id != action.lifecycle_id
        or batch.strategy_instance_id != action.strategy_instance_id
        or batch.execution_binding_id
        != int(action.evidence["execution_binding_id"])
        or batch.intent != action.action_kind
        or (
            action.action_kind == "move_stop_to_break_even"
            and batch.effective_action != "break_even_by_market"
        )
        or batch.requested_fraction != action.expected_effect.get("fraction")
        or tuple(sorted(str(leg.pos_id) for leg in batch.legs))
        != tuple(sorted(action.pos_ids))
        or tuple(
            sorted(int(leg.execution_order_leg_id) for leg in batch.legs)
        )
        != tuple(
            sorted(
                int(value)
                for value in action.evidence["execution_order_leg_ids"]
            )
        )
    ):
        raise ValueError("planned batch does not match confirmed remediation action")
    snapshot = batch.target_snapshot
    planned_positions = {
        str(row.get("pos_id")): row
        for row in snapshot.get("positions", [])
        if isinstance(row, dict) and row.get("pos_id")
    }
    confirmed_positions = {
        str(_first_text(row, "posId", "pos_id", "id")): row
        for row in action.evidence.get("positions", [])
        if isinstance(row, dict) and _first_text(row, "posId", "pos_id", "id")
    }
    if set(planned_positions) != set(confirmed_positions):
        raise ValueError("planned position set changed after confirmation")
    for pos_id, planned in planned_positions.items():
        confirmed = confirmed_positions[pos_id]
        if (
            str(planned.get("size"))
            != str(_first_text(confirmed, "pos", "size", "sz"))
            or str(planned.get("avg_entry_price"))
            != str(_first_text(confirmed, "avgPx", "avgPrice", "avg_entry_price"))
        ):
            raise ValueError("planned position economics changed after confirmation")


def _require_exchange_snapshot_fingerprint(*, deepcoin_client, action) -> None:
    instruments = {
        str(value).upper()
        for value in action.evidence.get("instrument_scope", [])
        if str(value or "").strip()
    }
    snapshot = _load_reconcile_snapshot(
        deepcoin_client,
        instruments=instruments,
    )
    if snapshot.errors or any(
        not bool(observation.get("complete"))
        for observation in snapshot.pending_tpsl_observations
    ):
        raise ValueError("final remediation exchange snapshot is incomplete")
    current_fingerprint = _fingerprint(_snapshot_payload(snapshot))
    if current_fingerprint != action.evidence.get("exchange_snapshot_fingerprint"):
        raise ValueError("exchange snapshot changed before remediation promotion")


def _candidate_decision(candidate: SignalCandidate) -> dict[str, Any]:
    return {
        "event_type": (
            "exit_position"
            if candidate.event_type == "close_signal"
            else "position_update"
        ),
        "target_lifecycle_id": candidate.target_lifecycle_id,
        "symbol": candidate.symbol,
        "side": candidate.side,
        "management_action": candidate.management_action,
        "management_fraction": candidate.management_fraction,
        "stop_loss": candidate.stop_loss_text,
        "take_profit": candidate.take_profit_text,
        "confidence": candidate.confidence,
    }


def _load_candidate_conflict_context(
    *,
    session,
    candidate: SignalCandidate,
    item: MessageInstructionItem | None,
) -> tuple[StrategyLifecycle, ExecutionBinding] | None:
    if candidate.target_lifecycle_id is not None:
        lifecycle = session.get(
            StrategyLifecycle,
            int(candidate.target_lifecycle_id),
        )
        if lifecycle is not None and lifecycle.execution_binding_id is not None:
            binding = session.get(
                ExecutionBinding,
                int(lifecycle.execution_binding_id),
            )
            if binding is not None and str(
                binding.strategy_instance_id or ""
            ).strip():
                return lifecycle, binding
    return _load_item_strategy_context(session=session, item=item)


def _load_item_strategy_context(
    *,
    session,
    item: MessageInstructionItem | None,
) -> tuple[StrategyLifecycle, ExecutionBinding] | None:
    strategy_instance_id = str(
        item.strategy_instance_id if item is not None else ""
    ).strip()
    if not strategy_instance_id:
        return None
    bindings = (
        session.query(ExecutionBinding)
        .filter(
            ExecutionBinding.strategy_instance_id == strategy_instance_id,
            ExecutionBinding.venue == "deepcoin",
        )
        .all()
    )
    if len(bindings) != 1:
        return None
    binding = bindings[0]
    lifecycles = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.execution_binding_id == binding.id)
        .all()
    )
    if len(lifecycles) != 1:
        return None
    return lifecycles[0], binding


def _candidate_item_strategy_conflict(
    *,
    session,
    candidate: SignalCandidate,
    item: MessageInstructionItem | None,
) -> tuple[StrategyLifecycle, ExecutionBinding, str] | None:
    item_context = _load_item_strategy_context(session=session, item=item)
    if item_context is None or candidate.target_lifecycle_id is None:
        return None
    target_lifecycle = session.get(
        StrategyLifecycle,
        int(candidate.target_lifecycle_id),
    )
    if (
        target_lifecycle is None
        or target_lifecycle.execution_binding_id is None
    ):
        return None
    target_binding = session.get(
        ExecutionBinding,
        int(target_lifecycle.execution_binding_id),
    )
    if target_binding is None:
        return None
    item_lifecycle, item_binding = item_context
    target_strategy_instance_id = str(
        target_binding.strategy_instance_id or ""
    ).strip()
    if (
        not target_strategy_instance_id
        or target_strategy_instance_id
        == str(item_binding.strategy_instance_id)
    ):
        return None
    return item_lifecycle, item_binding, target_strategy_instance_id


def _candidate_action_kind(candidate: SignalCandidate) -> str:
    if str(candidate.management_action or "").strip():
        return str(candidate.management_action)
    return "full_exit" if candidate.event_type == "close_signal" else "position_update"


def _predecessor_signature(
    *,
    session,
    strategy_instance_id: str,
    current_raw_message: RawMessage,
    current_candidate: SignalCandidate,
    current_item: MessageInstructionItem | None,
    current_effective_intent: str,
    excluded_batch_id: int | None = None,
) -> str:
    current_key = (
        current_raw_message.posted_at,
        int(current_raw_message.id),
        int(current_item.sequence) if current_item is not None else 0,
        int(current_candidate.id),
    )
    rows = (
        session.query(SignalCandidate, RawMessage)
        .join(RawMessage, RawMessage.id == SignalCandidate.raw_message_id)
        .filter(
            SignalCandidate.parse_source == "mimo_authoritative",
            SignalCandidate.review_status != "approved_remediation",
            SignalCandidate.event_type.in_(("close_signal", "position_update")),
        )
        .all()
    )
    predecessors: list[dict[str, Any]] = []
    for candidate, raw_message in rows:
        item = (
            session.query(MessageInstructionItem)
            .filter(
                MessageInstructionItem.signal_candidate_id == candidate.id,
                MessageInstructionItem.retired_at.is_(None),
            )
            .one_or_none()
        )
        candidate_strategy_id = _candidate_strategy_instance_id(
            session=session,
            candidate=candidate,
            item=item,
        )
        if candidate_strategy_id != strategy_instance_id:
            continue
        key = (
            raw_message.posted_at,
            int(raw_message.id),
            int(item.sequence) if item is not None else 0,
            int(candidate.id),
        )
        if key >= current_key:
            continue
        predecessors.append(
            {
                "key": key,
                "instruction_item_id": int(item.id) if item is not None else None,
                "status": item.status if item is not None else "missing",
                "result_json": item.result_json if item is not None else None,
                "error_json": item.error_json if item is not None else None,
                "candidate_state": {
                    "event_type": candidate.event_type,
                    "target_lifecycle_id": candidate.target_lifecycle_id,
                    "management_action": candidate.management_action,
                    "management_fraction": candidate.management_fraction,
                    "recognition_generation": candidate.recognition_generation,
                },
                "raw_text": raw_message.text,
            }
        )
    predecessors.sort(key=lambda row: tuple(row["key"]))
    batches = (
        session.query(StrategyManagementBatch, RawMessage)
        .join(RawMessage, RawMessage.id == StrategyManagementBatch.raw_message_id)
        .filter(
            StrategyManagementBatch.strategy_instance_id == strategy_instance_id
        )
        .all()
    )
    batch_state = []
    for batch, raw_message in batches:
        is_current_plan_only_batch = (
            int(batch.raw_message_id) == int(current_raw_message.id)
            and str(batch.intent) == current_effective_intent
            and str(batch.execution_mode) == "disabled"
            and str(batch.status) == "blocked"
            and str(batch.reason_code) == "management_disabled_plan_only"
        )
        if (
            excluded_batch_id is not None
            and int(batch.id) == excluded_batch_id
        ) or is_current_plan_only_batch:
            continue
        batch_state.append(
            {
                "id": int(batch.id),
                "raw_message_id": int(batch.raw_message_id),
                "raw_posted_at": raw_message.posted_at,
                "recognition_generation": batch.recognition_generation,
                "target_lifecycle_id": int(batch.target_lifecycle_id),
                "intent": batch.intent,
                "effective_action": batch.effective_action,
                "status": batch.status,
                "reason_code": batch.reason_code,
                "updated_at": batch.updated_at,
            }
        )
    batch_state.sort(key=lambda row: int(row["id"]))
    current_state = {
        "raw_message_id": int(current_raw_message.id),
        "raw_posted_at": current_raw_message.posted_at,
        "raw_text": current_raw_message.text,
        "candidate_id": int(current_candidate.id),
        "candidate_event_type": current_candidate.event_type,
        "candidate_target_lifecycle_id": current_candidate.target_lifecycle_id,
        "candidate_management_action": current_candidate.management_action,
        "candidate_management_fraction": current_candidate.management_fraction,
        "candidate_recognition_generation": current_candidate.recognition_generation,
        "instruction_item_id": (
            int(current_item.id) if current_item is not None else None
        ),
        "instruction_status": (
            current_item.status if current_item is not None else "missing"
        ),
        "instruction_result_json": (
            current_item.result_json if current_item is not None else None
        ),
        "instruction_error_json": (
            current_item.error_json if current_item is not None else None
        ),
        "effective_intent": current_effective_intent,
    }
    return _fingerprint(
        {
            "current": current_state,
            "predecessors": predecessors,
            "batches": batch_state,
        }
    )


def _candidate_strategy_instance_id(
    *,
    session,
    candidate: SignalCandidate,
    item: MessageInstructionItem | None,
) -> str | None:
    identity_conflict = _candidate_item_strategy_conflict(
        session=session,
        candidate=candidate,
        item=item,
    )
    if identity_conflict is not None:
        return str(identity_conflict[1].strategy_instance_id)
    explicit_context = _load_candidate_conflict_context(
        session=session,
        candidate=candidate,
        item=None,
    )
    if explicit_context is not None:
        return str(explicit_context[1].strategy_instance_id)
    item_strategy_id = str(
        item.strategy_instance_id if item is not None else ""
    ).strip()
    if item_strategy_id:
        return item_strategy_id
    return None


def _item_requires_remediation(item: MessageInstructionItem) -> bool:
    if item.status in {"failed", "unknown"}:
        return True
    if item.status != "succeeded" or not item.result_json:
        return False
    try:
        result = json.loads(item.result_json)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(result, dict) and str(result.get("status") or "").lower() in {
        "skipped",
        "shadow_planned",
    }


def _batch_matches_candidate_action(
    *,
    batch: StrategyManagementBatch,
    candidate: SignalCandidate,
    directive_intent: str,
    lifecycle_id: int,
) -> bool:
    generation = str(candidate.recognition_generation or "").strip()
    if (
        bool(generation)
        and str(batch.recognition_generation) == generation
        and str(batch.intent) == directive_intent
    ):
        return True
    effective_intent = (
        "full_exit" if directive_intent == "cancel_entry" else directive_intent
    )
    if str(batch.intent) != effective_intent:
        return False
    return _batch_remediation_action_id(batch) == _candidate_action_id(
        candidate=candidate,
        lifecycle_id=lifecycle_id,
        action_kind=effective_intent,
    )


def _terminal_batch_resolves_candidate(
    *,
    batch: StrategyManagementBatch,
    candidate: SignalCandidate,
    directive_intent: str,
    lifecycle_id: int,
) -> bool:
    return _batch_matches_candidate_action(
        batch=batch,
        candidate=candidate,
        directive_intent=directive_intent,
        lifecycle_id=lifecycle_id,
    )


def _batch_remediation_action_id(
    batch: StrategyManagementBatch,
) -> str | None:
    try:
        confirmation = json.loads(batch.target_snapshot_json).get(
            "remediation_confirmation",
            {},
        )
    except (TypeError, json.JSONDecodeError):
        return None
    action_id = str(confirmation.get("action_id") or "").strip()
    return action_id or None


def _candidate_action_id(
    *,
    candidate: SignalCandidate,
    lifecycle_id: int,
    action_kind: str,
) -> str:
    return _fingerprint(
        {
            "raw_message_id": candidate.raw_message_id,
            "candidate_id": candidate.id,
            "lifecycle_id": lifecycle_id,
            "action_kind": action_kind,
        }
    )[:20]


def _batch_candidate_association_is_unique(
    *,
    session,
    candidate: SignalCandidate,
) -> bool:
    matching_ids = [
        int(candidate_id)
        for (candidate_id,) in (
            session.query(SignalCandidate.id)
            .filter(
                SignalCandidate.raw_message_id == candidate.raw_message_id,
                SignalCandidate.recognition_generation
                == candidate.recognition_generation,
                SignalCandidate.target_lifecycle_id
                == candidate.target_lifecycle_id,
                SignalCandidate.parse_source == "mimo_authoritative",
                SignalCandidate.review_status != "approved_remediation",
            )
            .all()
        )
    ]
    return matching_ids == [int(candidate.id)]


def _entry_positions_match_exact_live_identity(
    *,
    binding: ExecutionBinding,
    entry_legs: list[ExecutionOrderLeg],
    live_rows: list[dict[str, Any]],
) -> bool:
    leg_pos_ids = [str(leg.pos_id or "").strip() for leg in entry_legs]
    if not leg_pos_ids or any(not value for value in leg_pos_ids):
        return False
    binding_pos_ids = {
        value.strip()
        for value in str(binding.pos_id or "").replace(";", ",").split(",")
        if value.strip()
    }
    if binding_pos_ids != set(leg_pos_ids):
        return False
    expected_instrument = f"{str(binding.symbol).upper()}-USDT-SWAP"
    expected_side = _normalize_position_side(binding.side)
    if expected_side is None:
        return False
    for pos_id in leg_pos_ids:
        matches = [
            row
            for row in live_rows
            if _first_text(row, "posId", "pos_id", "id") == pos_id
        ]
        if len(matches) != 1:
            return False
        row = matches[0]
        if (
            str(_first_text(row, "instId", "inst_id", "instrument_id") or "").upper()
            != expected_instrument
            or _normalize_position_side(
                _first_text(row, "posSide", "pos_side", "side")
            )
            != expected_side
        ):
            return False
    return True


def _normalize_position_side(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"long", "buy"}:
        return "long"
    if normalized in {"short", "sell"}:
        return "short"
    return None


def _snapshot_payload(snapshot) -> dict[str, Any]:
    return remediation_snapshot_payload(snapshot)


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _first_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None

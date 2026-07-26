"""Fingerprint-guarded remediation for missed position-management instructions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
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
    raw_message_id: int
    instruction_item_id: int | None
    candidate_id: int
    sequence: int
    posted_at: datetime
    action_kind: str
    state: str
    reason: str | None
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
    live_positions = {
        str(pos_id): dict(row)
        for row in snapshot.positions
        if (pos_id := _first_text(row, "posId", "pos_id", "id"))
    }

    actions: list[PositionRemediationAction] = []
    conflicts: list[dict[str, Any]] = []
    with session_factory() as session:
        candidates = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.parse_source == "mimo_authoritative")
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
                conflicts.append(
                    {
                        "raw_message_id": int(raw_message.id),
                        "candidate_id": int(candidate.id),
                        "reason": str(exc),
                    }
                )
                continue
            for target in targets:
                lifecycle = session.get(StrategyLifecycle, target.lifecycle_id)
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
                if any(
                    batch.status
                    in {
                        "ready",
                        "executing",
                        "reserved",
                        "submitted",
                        "reconciling",
                        "protection_ready",
                        "succeeded",
                    }
                    for batch in existing_batches
                ):
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
                    conflicts.append(
                        {
                            "raw_message_id": int(raw_message.id),
                            "candidate_id": int(candidate.id),
                            "lifecycle_id": target.lifecycle_id,
                            "management_batch_id": int(unresolved_batch.id),
                            "reason": "existing_management_batch_unresolved",
                        }
                    )
                    continue
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
                    conflicts.append(
                        {
                            "raw_message_id": int(raw_message.id),
                            "candidate_id": int(candidate.id),
                            "lifecycle_id": target.lifecycle_id,
                            "reason": "target_strategy_binding_not_verified",
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
                if not pos_ids or len(pos_ids) != len(entry_legs):
                    conflicts.append(
                        {
                            "raw_message_id": int(raw_message.id),
                            "candidate_id": int(candidate.id),
                            "lifecycle_id": target.lifecycle_id,
                            "reason": (
                                "late_fill_identity_not_exact"
                                if directive.intent == "cancel_entry"
                                else "target_live_position_not_exact"
                            ),
                        }
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

    chains = _build_remediation_chains(actions)
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
) -> tuple[PositionRemediationChain, ...]:
    grouped: dict[str, list[PositionRemediationAction]] = {}
    for action in actions:
        grouped.setdefault(action.strategy_instance_id, []).append(action)
    chains: list[PositionRemediationChain] = []
    for strategy_instance_id, grouped_actions in grouped.items():
        ordered = sorted(
            grouped_actions,
            key=lambda action: (
                str(action.evidence.get("source_posted_at") or ""),
                int(action.raw_message_id or 0),
                int(action.evidence.get("instruction_sequence") or 0),
                int(action.evidence.get("candidate_id") or 0),
            ),
        )
        steps = tuple(
            PositionRemediationStep(
                raw_message_id=int(action.raw_message_id),
                instruction_item_id=action.evidence.get("instruction_item_id"),
                candidate_id=int(action.evidence["candidate_id"]),
                sequence=int(action.evidence.get("instruction_sequence") or 0),
                posted_at=action.evidence["source_posted_at"],
                action_kind=action.action_kind,
                state=(
                    "ready_for_approval"
                    if index == 0
                    else "waiting_for_predecessor"
                ),
                reason=None if index == 0 else "predecessor_not_resolved",
                action=action,
            )
            for index, action in enumerate(ordered)
        )
        first = ordered[0]
        chains.append(
            PositionRemediationChain(
                strategy_instance_id=strategy_instance_id,
                lifecycle_id=first.lifecycle_id,
                execution_binding_id=int(first.evidence["execution_binding_id"]),
                steps=steps,
                conflicts=(),
                fingerprint=_fingerprint(
                    {
                        "strategy_instance_id": strategy_instance_id,
                        "steps": [asdict(step) for step in steps],
                    }
                ),
            )
        )
    return tuple(
        sorted(chains, key=lambda chain: (chain.strategy_instance_id, chain.lifecycle_id))
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
    if plan.conflicts:
        raise ValueError("remediation plan has unresolved conflicts")
    matches = [action for action in plan.actions if action.action_id == action_id]
    if len(matches) != 1:
        raise ValueError("remediation action not found or not unique")
    action = matches[0]
    if action.fingerprint != expected_fingerprint:
        raise ValueError("remediation action fingerprint mismatch")
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


def _snapshot_payload(snapshot) -> dict[str, Any]:
    return {
        "positions": list(snapshot.positions),
        "pending_trigger_orders": list(snapshot.pending_trigger_orders),
        "open_orders": list(snapshot.open_orders),
        "order_history": list(snapshot.order_history),
        "trade_fills": list(snapshot.trade_fills),
        "trigger_history": list(snapshot.trigger_history),
        "pending_tpsl_observations": list(snapshot.pending_tpsl_observations),
        "errors": dict(snapshot.errors),
    }


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

"""Bounded, append-only repair proof for entry assembly fingerprints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from telegram_kol_research.models import (
    EntryStrategyAssembly,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    RawMessage,
    SignalCandidate,
    TradeSignal,
)


RECONCILIATION_ACTION = "entry_assembly_fingerprint_reconciled"
RECONCILIATION_POLICY = "entry-assembly-fingerprint-reconciliation-v1"
_MAX_JSON_BYTES = 1_000_000
_DRAFT_KEYS = (
    "strategy_instance_id",
    "instrument_id",
    "stop_loss",
    "take_profit_legs",
    "risk_budget_usdt",
    "contract_spec",
    "order_legs",
)
_LEG_KEYS = (
    "price",
    "order_type",
    "allocation_pct",
    "risk_budget_usdt",
    "quantity",
    "quantity_unit",
    "estimated_stop_loss_usdt",
    "client_order_id",
)


@dataclass(frozen=True, slots=True)
class EntryAssemblyFingerprintRepairAction:
    assembly_id: int
    execution_binding_id: int
    trade_signal_id: int | None
    strategy_instance_id: str
    old_fingerprint: str
    final_fingerprint: str
    repair_fingerprint: str


@dataclass(frozen=True, slots=True)
class EntryAssemblyFingerprintRepairPlan:
    action: EntryAssemblyFingerprintRepairAction | None
    conflicts: tuple[str, ...]
    fingerprint: str


def canonical_fingerprint(payload: Mapping[str, Any]) -> str:
    """Hash a JSON-compatible mapping with the assembly's canonical encoding."""

    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def derive_pre_finalization_fingerprint(
    final_evidence: Mapping[str, Any],
) -> str:
    """Derive the fingerprint before the two finalization fields were attached."""

    evidence = dict(final_evidence)
    evidence.pop("order_draft_snapshot", None)
    evidence.pop("final_entry_leg_count", None)
    return canonical_fingerprint(evidence)


def build_reconciliation_fingerprint(
    *,
    assembly_id: int,
    execution_binding_id: int,
    trade_signal_id: int | None,
    strategy_instance_id: str,
    old_fingerprint: str,
    final_fingerprint: str,
) -> str:
    return canonical_fingerprint(
        {
            "policy_version": RECONCILIATION_POLICY,
            "assembly_id": int(assembly_id),
            "execution_binding_id": int(execution_binding_id),
            "trade_signal_id": trade_signal_id,
            "strategy_instance_id": strategy_instance_id,
            "old_fingerprint": old_fingerprint,
            "final_fingerprint": final_fingerprint,
        }
    )


def build_entry_assembly_fingerprint_repair_plan(
    session_factory: sessionmaker,
    *,
    assembly_id: int,
    execution_binding_id: int,
    session: Session | None = None,
) -> EntryAssemblyFingerprintRepairPlan:
    """Read exact durable rows and return one mechanically proven repair action."""

    if session is not None:
        return _build_entry_assembly_fingerprint_repair_plan(
            session,
            assembly_id=assembly_id,
            execution_binding_id=execution_binding_id,
        )
    with session_factory() as owned_session:
        return _build_entry_assembly_fingerprint_repair_plan(
            owned_session,
            assembly_id=assembly_id,
            execution_binding_id=execution_binding_id,
        )


def _build_entry_assembly_fingerprint_repair_plan(
    session: Session,
    *,
    assembly_id: int,
    execution_binding_id: int,
) -> EntryAssemblyFingerprintRepairPlan:
    conflicts: list[str] = []
    assembly = session.get(EntryStrategyAssembly, int(assembly_id))
    binding = session.get(ExecutionBinding, int(execution_binding_id))
    if assembly is None:
        conflicts.append("assembly_not_found")
    if binding is None:
        conflicts.append("execution_binding_not_found")
    if conflicts:
        return _plan(None, conflicts)

    final_evidence = _json_object(assembly.evidence_json)
    binding_payload = _json_object(binding.payload_json)
    if final_evidence is None:
        conflicts.append("assembly_evidence_invalid")
    if binding_payload is None:
        conflicts.append("binding_payload_invalid")
    if conflicts:
        return _plan(None, conflicts)

    snapshot = final_evidence.get("order_draft_snapshot")
    final_leg_count = final_evidence.get("final_entry_leg_count")
    if (
        not isinstance(snapshot, Mapping)
        or not isinstance(snapshot.get("order_legs"), list)
        or not all(isinstance(leg, Mapping) for leg in snapshot["order_legs"])
        or isinstance(final_leg_count, bool)
        or not isinstance(final_leg_count, int)
        or final_leg_count != len(snapshot["order_legs"])
        or not 1 <= final_leg_count <= 5
    ):
        conflicts.append("assembly_finalization_fields_missing")
    if canonical_fingerprint(final_evidence) != str(assembly.fingerprint):
        conflicts.append("assembly_final_fingerprint_invalid")

    strategy_id = str(assembly.strategy_instance_id or "")
    if not strategy_id or strategy_id != strategy_id.strip():
        conflicts.append("assembly_strategy_identity_invalid")
    if assembly.entry_preamble_id is not None:
        conflicts.append("assembly_not_v2")
    if not _positive_int_equals(
        final_evidence.get("strategy_raw_message_id"),
        assembly.strategy_raw_message_id,
    ):
        conflicts.append("assembly_strategy_raw_message_identity_mismatch")
    if not _positive_int_equals(
        final_evidence.get("signal_candidate_id"), assembly.signal_candidate_id
    ):
        conflicts.append("assembly_signal_candidate_identity_mismatch")
    strategy_raw_message = session.get(
        RawMessage, int(assembly.strategy_raw_message_id)
    )
    signal_candidate = session.get(
        SignalCandidate, int(assembly.signal_candidate_id)
    )
    if strategy_raw_message is None:
        conflicts.append("strategy_raw_message_not_found")
    if signal_candidate is None:
        conflicts.append("signal_candidate_not_found")
    if signal_candidate is not None and (
        int(signal_candidate.raw_message_id) != int(assembly.strategy_raw_message_id)
    ):
        conflicts.append("signal_candidate_raw_message_mismatch")
    if strategy_raw_message is not None and not _raw_source_identity_matches(
        strategy_raw_message,
        evidence=final_evidence,
        binding=binding,
    ):
        conflicts.append("strategy_raw_message_source_mismatch")
    if signal_candidate is not None and not _candidate_is_entry_strategy(
        signal_candidate,
        evidence=final_evidence,
        binding=binding,
    ):
        conflicts.append("signal_candidate_not_entry_strategy")
    snapshot_strategy = (
        str(snapshot.get("strategy_instance_id") or "")
        if isinstance(snapshot, Mapping)
        else ""
    )
    if not snapshot_strategy or snapshot_strategy != strategy_id:
        conflicts.append("assembly_snapshot_strategy_mismatch")
    if str(binding.strategy_instance_id or "") != strategy_id:
        conflicts.append("binding_strategy_mismatch")
    if str(binding.venue or "").lower() != "deepcoin":
        conflicts.append("binding_venue_mismatch")
    if not _source_identity_matches(final_evidence, binding):
        conflicts.append("binding_source_identity_mismatch")

    binding_draft = binding_payload.get("draft")
    stale_evidence = (
        binding_draft.get("entry_preamble_assembly")
        if isinstance(binding_draft, Mapping)
        else None
    )
    old_fingerprint = derive_pre_finalization_fingerprint(final_evidence)
    if not _stale_evidence_matches(
        stale_evidence,
        assembly_id=int(assembly.id),
        strategy_instance_id=strategy_id,
        old_fingerprint=old_fingerprint,
    ):
        conflicts.append("binding_old_fingerprint_not_derivable")
    if not isinstance(binding_draft, Mapping) or not isinstance(snapshot, Mapping) or (
        _bounded_draft(binding_draft) != _bounded_draft(snapshot)
    ):
        conflicts.append("binding_draft_identity_mismatch")

    trade_signal = _load_exact_trade_signal(session, assembly, binding)
    if trade_signal is None:
        conflicts.append("trade_signal_identity_missing")
    elif not _signal_has_stale_evidence(
        trade_signal,
        assembly_id=int(assembly.id),
        strategy_instance_id=strategy_id,
        old_fingerprint=old_fingerprint,
        snapshot=snapshot,
    ):
        conflicts.append("trade_signal_evidence_mismatch")

    legs = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.execution_binding_id == int(binding.id))
        .filter(ExecutionOrderLeg.purpose == "entry")
        .order_by(ExecutionOrderLeg.leg_index.asc(), ExecutionOrderLeg.id.asc())
        .all()
    )
    if not isinstance(snapshot, Mapping) or not _execution_legs_match(
        legs,
        snapshot=snapshot,
        strategy_instance_id=strategy_id,
        symbol=str(binding.symbol),
        side=str(binding.side),
    ):
        conflicts.append("execution_leg_identity_mismatch")

    if conflicts:
        return _plan(None, conflicts)
    trade_signal_id = int(trade_signal.id) if trade_signal is not None else None
    repair_fingerprint = build_reconciliation_fingerprint(
        assembly_id=int(assembly.id),
        execution_binding_id=int(binding.id),
        trade_signal_id=trade_signal_id,
        strategy_instance_id=strategy_id,
        old_fingerprint=old_fingerprint,
        final_fingerprint=str(assembly.fingerprint),
    )
    action = EntryAssemblyFingerprintRepairAction(
        assembly_id=int(assembly.id),
        execution_binding_id=int(binding.id),
        trade_signal_id=trade_signal_id,
        strategy_instance_id=strategy_id,
        old_fingerprint=old_fingerprint,
        final_fingerprint=str(assembly.fingerprint),
        repair_fingerprint=repair_fingerprint,
    )
    events = (
        session.query(ExecutionEvent)
        .filter(ExecutionEvent.execution_binding_id == int(binding.id))
        .filter(ExecutionEvent.action == RECONCILIATION_ACTION)
        .order_by(ExecutionEvent.id.asc())
        .all()
    )
    if any(not _event_matches(row, action) for row in events) or len(events) > 1:
        conflicts.append("reconciliation_event_conflict")
        action = None
    return _plan(action, conflicts)


def apply_entry_assembly_fingerprint_repair_plan(
    session_factory: sessionmaker,
    *,
    assembly_id: int,
    execution_binding_id: int,
    expected_plan_fingerprint: str,
    applied_at: datetime,
) -> int:
    """Append exactly one audited repair event after rebuilding the proof."""

    with session_factory() as session:
        _acquire_repair_write_lock(session)
        plan = build_entry_assembly_fingerprint_repair_plan(
            session_factory,
            assembly_id=assembly_id,
            execution_binding_id=execution_binding_id,
            session=session,
        )
        if plan.action is None or plan.conflicts:
            raise RuntimeError("repair_plan_not_actionable")
        if str(expected_plan_fingerprint) != plan.fingerprint:
            raise ValueError("repair_plan_fingerprint_mismatch")
        action = plan.action
        expected = _event_values(action, applied_at=applied_at)
        existing = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.notification_fingerprint == action.repair_fingerprint)
            .one_or_none()
        )
        if existing is not None:
            if _event_matches(existing, action):
                return int(existing.id)
            raise RuntimeError("repair_event_fingerprint_collision")
        event = ExecutionEvent(**expected)
        session.add(event)
        try:
            session.commit()
            return int(event.id)
        except IntegrityError:
            session.rollback()
            existing = (
                session.query(ExecutionEvent)
                .filter(
                    ExecutionEvent.notification_fingerprint
                    == action.repair_fingerprint
                )
                .one_or_none()
            )
            if existing is not None and _event_matches(existing, action):
                return int(existing.id)
            raise RuntimeError("repair_event_fingerprint_collision")


def _plan(
    action: EntryAssemblyFingerprintRepairAction | None, conflicts: list[str]
) -> EntryAssemblyFingerprintRepairPlan:
    unique_conflicts = tuple(dict.fromkeys(conflicts))
    payload = {
        "policy_version": RECONCILIATION_POLICY,
        "action": asdict(action) if action is not None else None,
        "conflicts": list(unique_conflicts),
    }
    return EntryAssemblyFingerprintRepairPlan(
        action=action,
        conflicts=unique_conflicts,
        fingerprint=canonical_fingerprint(payload),
    )


def _json_object(raw: str | None) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    try:
        if len(raw.encode("utf-8")) > _MAX_JSON_BYTES:
            return None
        value = json.loads(raw)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _bounded_draft(draft: Mapping[str, Any]) -> dict[str, Any] | None:
    bounded = {key: draft.get(key) for key in _DRAFT_KEYS if key != "order_legs"}
    raw_legs = draft.get("order_legs")
    if not isinstance(raw_legs, list) or not all(
        isinstance(leg, Mapping) for leg in raw_legs
    ):
        return None
    bounded["order_legs"] = [
        {key: leg.get(key) for key in _LEG_KEYS} for leg in raw_legs
    ]
    return bounded


def _positive_int_equals(value: Any, expected: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
        and value == int(expected)
    )


def _acquire_repair_write_lock(session: Session) -> None:
    """Prevent SQLite writers from changing proof rows until event commit."""

    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def _source_identity_matches(evidence: Mapping[str, Any], binding: ExecutionBinding) -> bool:
    return (
        evidence.get("chat_id") == int(binding.chat_id)
        and evidence.get("strategy_message_id") == int(binding.message_id)
        and str(evidence.get("symbol") or "").upper() == str(binding.symbol).upper()
        and str(evidence.get("side") or "").lower() == str(binding.side).lower()
    )


def _raw_source_identity_matches(
    raw_message: RawMessage,
    *,
    evidence: Mapping[str, Any],
    binding: ExecutionBinding,
) -> bool:
    return (
        int(raw_message.chat_id) == evidence.get("chat_id") == int(binding.chat_id)
        and int(raw_message.message_id)
        == evidence.get("strategy_message_id")
        == int(binding.message_id)
    )


def _candidate_is_entry_strategy(
    candidate: SignalCandidate,
    *,
    evidence: Mapping[str, Any],
    binding: ExecutionBinding,
) -> bool:
    return (
        candidate.event_type == "entry_signal"
        and candidate.review_status in {"pending", "confirmed"}
        and candidate.target_lifecycle_id is None
        and candidate.management_action is None
        and str(candidate.symbol or "")
        == str(evidence.get("symbol") or "")
        == str(binding.symbol)
        and str(candidate.side or "")
        == str(evidence.get("side") or "")
        == str(binding.side)
    )


def _load_exact_trade_signal(session, assembly, binding) -> TradeSignal | None:
    rows = (
        session.query(TradeSignal)
        .filter(TradeSignal.strategy_instance_id == str(assembly.strategy_instance_id))
        .filter(TradeSignal.chat_id == int(binding.chat_id))
        .filter(TradeSignal.message_id == int(binding.message_id))
        .filter(TradeSignal.symbol == str(binding.symbol))
        .filter(TradeSignal.side == str(binding.side))
        .filter(TradeSignal.kol_id == str(binding.kol_id))
        .filter(TradeSignal.venue == "deepcoin")
        .filter(TradeSignal.source_type == "recovery")
        .filter(TradeSignal.action == "open_position")
        .filter(TradeSignal.status == "submitted")
        .filter(TradeSignal.processed_at.isnot(None))
        .order_by(TradeSignal.id.asc())
        .limit(2)
        .all()
    )
    return rows[0] if len(rows) == 1 else None


def _signal_has_stale_evidence(
    signal: TradeSignal,
    *,
    assembly_id: int,
    strategy_instance_id: str,
    old_fingerprint: str,
    snapshot: Mapping[str, Any],
) -> bool:
    payload = _json_object(signal.payload_json)
    if payload is None:
        return False
    top = payload.get("entry_preamble_assembly")
    draft = payload.get("deepcoin_order_draft")
    nested = draft.get("entry_preamble_assembly") if isinstance(draft, Mapping) else None
    return (
        _stale_evidence_matches(
            top,
            assembly_id=assembly_id,
            strategy_instance_id=strategy_instance_id,
            old_fingerprint=old_fingerprint,
        )
        and _stale_evidence_matches(
            nested,
            assembly_id=assembly_id,
            strategy_instance_id=strategy_instance_id,
            old_fingerprint=old_fingerprint,
        )
        and isinstance(draft, Mapping)
        and _bounded_draft(draft) == _bounded_draft(snapshot)
    )


def _stale_evidence_matches(
    evidence: Any,
    *,
    assembly_id: int,
    strategy_instance_id: str,
    old_fingerprint: str,
) -> bool:
    return (
        isinstance(evidence, Mapping)
        and evidence.get("assembly_id") == assembly_id
        and str(evidence.get("strategy_instance_id") or "") == strategy_instance_id
        and str(evidence.get("assembly_fingerprint") or "") == old_fingerprint
    )


def _execution_legs_match(
    legs: list[ExecutionOrderLeg],
    *,
    snapshot: Mapping[str, Any],
    strategy_instance_id: str,
    symbol: str,
    side: str,
) -> bool:
    expected_legs = snapshot.get("order_legs")
    instrument = str(snapshot.get("instrument_id") or "").upper()
    if not isinstance(expected_legs, list) or len(legs) != len(expected_legs):
        return False
    if instrument != f"{symbol.upper()}-USDT-SWAP":
        return False
    for index, (row, expected) in enumerate(zip(legs, expected_legs), 1):
        request = _json_object(row.request_json)
        if not isinstance(expected, Mapping) or request is None:
            return False
        request_price = request.get("price", request.get("triggerPrice"))
        draft_order_type = str(expected.get("order_type") or "").lower()
        expected_order_kind = {
            "limit": "trigger_limit",
            "market": "market",
        }.get(draft_order_type)
        expected_request_order_type = {
            "limit": "limit",
            "market": "market",
        }.get(draft_order_type)
        if (
            int(row.leg_index) != index
            or str(row.strategy_instance_id or "") != strategy_instance_id
            or str(row.venue or "").lower() != "deepcoin"
            or str(row.status or "").lower() not in {"submitted", "open", "active"}
            or not str(row.order_id or "").strip()
            or expected_order_kind is None
            or str(row.order_kind or "").lower() != expected_order_kind
            or str(row.client_order_id or "") != str(expected.get("client_order_id") or "")
            or not str(row.client_order_id or "").strip()
            or str(request.get("instId") or "").upper() != instrument
            or str(request.get("posSide") or request.get("side") or "").lower()
            != side.lower()
            or str(request.get("orderType") or "").lower()
            != expected_request_order_type
            or not _same_number(request_price, expected.get("price"))
            or not _same_number(request.get("sz"), expected.get("quantity"))
            or str(request.get("clOrdId") or "")
            != str(expected.get("client_order_id") or "")
        ):
            return False
    return True


def _same_number(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    try:
        left_number = Decimal(str(left))
        right_number = Decimal(str(right))
    except (InvalidOperation, ValueError):
        return False
    return left_number.is_finite() and right_number.is_finite() and left_number == right_number


def _event_documents(action: EntryAssemblyFingerprintRepairAction):
    common = {
        "policy_version": RECONCILIATION_POLICY,
        "assembly_id": action.assembly_id,
        "execution_binding_id": action.execution_binding_id,
        "trade_signal_id": action.trade_signal_id,
        "strategy_instance_id": action.strategy_instance_id,
    }
    before = {**common, "assembly_fingerprint": action.old_fingerprint}
    after = {
        **common,
        "assembly_fingerprint": action.final_fingerprint,
        "repair_fingerprint": action.repair_fingerprint,
    }
    return before, after


def _event_values(
    action: EntryAssemblyFingerprintRepairAction, *, applied_at: datetime
) -> dict[str, Any]:
    before, after = _event_documents(action)
    return {
        "execution_binding_id": action.execution_binding_id,
        "trade_signal_id": action.trade_signal_id,
        "strategy_instance_id": action.strategy_instance_id,
        "venue": "deepcoin",
        "action": RECONCILIATION_ACTION,
        "status": "resolved",
        "reason": "pre_finalization_payload_preserved",
        "before_json": json.dumps(before, ensure_ascii=False, sort_keys=True),
        "after_json": json.dumps(after, ensure_ascii=False, sort_keys=True),
        "notification_status": None,
        "notification_fingerprint": action.repair_fingerprint,
        "created_at": applied_at,
    }


def _event_matches(
    event: ExecutionEvent, action: EntryAssemblyFingerprintRepairAction
) -> bool:
    before, after = _event_documents(action)
    return (
        event.execution_binding_id == action.execution_binding_id
        and event.trade_signal_id == action.trade_signal_id
        and event.strategy_instance_id == action.strategy_instance_id
        and event.venue == "deepcoin"
        and event.action == RECONCILIATION_ACTION
        and event.status == "resolved"
        and event.reason == "pre_finalization_payload_preserved"
        and _json_object(event.before_json) == before
        and _json_object(event.after_json) == after
        and event.kol_id is None
        and event.chat_id is None
        and event.message_id is None
        and event.source_message_id is None
        and event.symbol is None
        and event.side is None
        and event.order_id is None
        and event.client_order_id is None
        and event.pos_id is None
        and event.related_order_id is None
        and event.request_json is None
        and event.response_json is None
        and event.exchange_event_time is None
        and event.notification_status is None
        and event.notification_fingerprint == action.repair_fingerprint
        and event.notification_message_id is None
        and event.notification_error is None
        and event.notification_attempts == 0
        and event.notification_next_attempt_at is None
        and event.notification_claim_token is None
        and event.notification_claimed_at is None
        and event.notified_at is None
    )

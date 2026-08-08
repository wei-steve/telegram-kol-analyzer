"""Bounded, append-only repair proof for entry assembly fingerprints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from telegram_kol_research.entry_strategy_assembly import (
    build_bounded_entry_order_draft_snapshot,
)
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
LEGACY_FINALIZED_RECONCILIATION_POLICY = (
    "entry-assembly-fingerprint-reconciliation-legacy-finalized-v1"
)
_MAX_JSON_BYTES = 1_000_000
_LEGACY_SNAPSHOT_KEYS = frozenset(
    {
        "strategy_instance_id",
        "instrument_id",
        "stop_loss",
        "take_profit_legs",
        "risk_budget_usdt",
        "contract_spec",
        "order_legs",
    }
)
_LEGACY_LEG_KEYS = frozenset(
    {
        "price",
        "order_type",
        "allocation_pct",
        "risk_budget_usdt",
        "quantity",
        "quantity_unit",
        "estimated_stop_loss_usdt",
        "client_order_id",
    }
)
_LEGACY_FULL_DRAFT_KEYS = frozenset(
    {
        "blocking_reason_codes",
        "contract_spec",
        "dry_run_only",
        "entry_preamble_assembly",
        "executable",
        "instrument_id",
        "margin_mode",
        "notes",
        "order_legs",
        "position_mode",
        "risk_budget_usdt",
        "source",
        "stop_loss",
        "strategy_instance_id",
        "symbol",
        "take_profit_legs",
        "venue",
    }
)
_LEGACY_FULL_CONTRACT_KEYS = frozenset(
    {
        "contract_value",
        "instrument_id",
        "min_quantity",
        "price_tick",
        "quantity_step",
    }
)
_LEGACY_FULL_SOURCE_KEYS = frozenset(
    {"chat_id", "kol_code", "kol_id", "message_id"}
)
_LEGACY_FULL_LEG_KEYS = frozenset(
    {
        "allocation_pct",
        "base_asset_estimate",
        "client_order_id",
        "estimated_stop_loss_usdt",
        "order_type",
        "position_side",
        "price",
        "quantity",
        "quantity_unit",
        "risk_budget_usdt",
        "side",
    }
)
_LEGACY_TAKE_PROFIT_KEYS = frozenset(
    {"allocation_pct", "index", "order_type", "price"}
)
_STALE_EVIDENCE_KEYS = frozenset(
    {"assembly_id", "strategy_instance_id", "assembly_fingerprint"}
)
_LEGACY_BINDING_PAYLOAD_KEYS = frozenset({"draft", "submitted_orders"})
_LEGACY_SIGNAL_PAYLOAD_KEYS = frozenset({"deepcoin_order_draft", "source"})
_LEGACY_SIGNAL_SOURCE_KEYS = frozenset(
    {"chat_id", "message_id", "side", "symbol"}
)
_LEGACY_SUBMITTED_ORDER_KEYS = frozenset(
    {
        "client_order_id",
        "execution_type",
        "leg_index",
        "order_id",
        "pos_id",
        "protection_request",
        "protection_response",
        "request",
        "response",
    }
)
_LEGACY_SUBMISSION_RESPONSE_KEYS = frozenset({"code", "data", "msg"})
_LEGACY_SUBMISSION_DATA_KEYS = frozenset(
    {"clOrdId", "ordId", "sCode", "sMsg", "tag"}
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
    policy_version: str


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


def entry_order_snapshot_matches_durable_identity(
    snapshot: Mapping[str, Any],
    *,
    strategy_instance_id: str,
    symbol: str,
    side: str,
    kol_id: str,
    chat_id: int,
    message_id: int,
    margin_mode: str,
    position_mode: str,
) -> bool:
    """Bind a canonical entry snapshot to identities stored outside its JSON."""

    source = snapshot.get("source")
    legs = snapshot.get("order_legs")
    expected_order_side = {"long": "buy", "short": "sell"}.get(side)
    return (
        bool(strategy_instance_id)
        and snapshot.get("strategy_instance_id") == strategy_instance_id
        and snapshot.get("symbol") == symbol
        and snapshot.get("instrument_id") == f"{symbol}-USDT-SWAP"
        and snapshot.get("margin_mode") == margin_mode
        and snapshot.get("position_mode") == position_mode
        and isinstance(source, Mapping)
        and source.get("kol_id") == kol_id
        and source.get("chat_id") == chat_id
        and source.get("message_id") == message_id
        and expected_order_side is not None
        and isinstance(legs, list)
        and all(
            isinstance(leg, Mapping)
            and leg.get("position_side") == side
            and leg.get("side") == expected_order_side
            for leg in legs
        )
    )


def build_reconciliation_fingerprint(
    *,
    assembly_id: int,
    execution_binding_id: int,
    trade_signal_id: int | None,
    strategy_instance_id: str,
    old_fingerprint: str,
    final_fingerprint: str,
    policy_version: str = RECONCILIATION_POLICY,
) -> str:
    return canonical_fingerprint(
        {
            "policy_version": policy_version,
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
    canonical_snapshot = _bounded_snapshot(snapshot)
    legacy_snapshot = _legacy_snapshot(snapshot)
    final_leg_count = final_evidence.get("final_entry_leg_count")
    snapshot_legs = (
        snapshot.get("order_legs") if isinstance(snapshot, Mapping) else None
    )
    if (
        not isinstance(snapshot, Mapping)
        or (canonical_snapshot != snapshot and legacy_snapshot != snapshot)
        or not isinstance(snapshot_legs, list)
        or not all(isinstance(leg, Mapping) for leg in snapshot_legs)
        or isinstance(final_leg_count, bool)
        or not isinstance(final_leg_count, int)
        or final_leg_count != len(snapshot_legs)
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
    identity_snapshot = (
        canonical_snapshot
        if canonical_snapshot == snapshot
        else binding_payload.get("draft")
    )
    if not isinstance(identity_snapshot, Mapping) or not (
        entry_order_snapshot_matches_durable_identity(
            identity_snapshot,
            strategy_instance_id=strategy_id,
            symbol=str(binding.symbol),
            side=str(binding.side),
            kol_id=str(binding.kol_id),
            chat_id=int(binding.chat_id),
            message_id=int(binding.message_id),
            margin_mode=str(binding.margin_mode),
            position_mode=str(binding.position_mode),
        )
    ):
        conflicts.append("assembly_snapshot_durable_identity_mismatch")

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
    canonical_proof = canonical_snapshot == snapshot
    legacy_proof = legacy_snapshot == snapshot
    if canonical_proof:
        if _bounded_snapshot(binding_draft) != canonical_snapshot:
            conflicts.append("binding_draft_identity_mismatch")
    elif legacy_proof:
        if not legacy_finalized_binding_payload_is_exact(binding_payload) or (
            legacy_finalized_snapshot_from_full_draft(binding_draft) != legacy_snapshot
        ):
            conflicts.append("assembly_legacy_snapshot_binding_mismatch")
    else:
        conflicts.append("binding_draft_identity_mismatch")

    trade_signal = _load_exact_trade_signal(session, assembly, binding)
    if trade_signal is None:
        conflicts.append("trade_signal_identity_missing")
    elif canonical_proof:
        if not _signal_has_stale_evidence(
            trade_signal,
            assembly_id=int(assembly.id),
            strategy_instance_id=strategy_id,
            old_fingerprint=old_fingerprint,
            snapshot=snapshot,
        ):
            conflicts.append("trade_signal_evidence_mismatch")
    elif not _legacy_signal_has_stale_evidence(
        trade_signal,
        assembly_id=int(assembly.id),
        strategy_instance_id=strategy_id,
        old_fingerprint=old_fingerprint,
        binding_draft=binding_draft,
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
    legs_match = False
    if isinstance(snapshot, Mapping) and canonical_proof:
        legs_match = _execution_legs_match(
            legs,
            snapshot=snapshot,
            strategy_instance_id=strategy_id,
            symbol=str(binding.symbol),
            side=str(binding.side),
        )
    elif isinstance(binding_draft, Mapping) and legacy_proof:
        legs_match = legacy_finalized_execution_legs_match(
            legs,
            full_draft=binding_draft,
            final_leg_count=final_leg_count,
            strategy_instance_id=strategy_id,
            symbol=str(binding.symbol),
            side=str(binding.side),
            submitted_orders=binding_payload.get("submitted_orders"),
        )
    if not legs_match:
        conflicts.append("execution_leg_identity_mismatch")

    if conflicts:
        return _plan(None, conflicts)
    trade_signal_id = int(trade_signal.id) if trade_signal is not None else None
    policy_version = (
        RECONCILIATION_POLICY
        if canonical_proof
        else LEGACY_FINALIZED_RECONCILIATION_POLICY
    )
    repair_fingerprint = build_reconciliation_fingerprint(
        assembly_id=int(assembly.id),
        execution_binding_id=int(binding.id),
        trade_signal_id=trade_signal_id,
        strategy_instance_id=strategy_id,
        old_fingerprint=old_fingerprint,
        final_fingerprint=str(assembly.fingerprint),
        policy_version=policy_version,
    )
    action = EntryAssemblyFingerprintRepairAction(
        assembly_id=int(assembly.id),
        execution_binding_id=int(binding.id),
        trade_signal_id=trade_signal_id,
        strategy_instance_id=strategy_id,
        old_fingerprint=old_fingerprint,
        final_fingerprint=str(assembly.fingerprint),
        repair_fingerprint=repair_fingerprint,
        policy_version=policy_version,
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
        value = json.loads(
            raw,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _bounded_snapshot(draft: Any) -> dict[str, object] | None:
    if not isinstance(draft, Mapping):
        return None
    try:
        return build_bounded_entry_order_draft_snapshot(draft)
    except (TypeError, ValueError):
        return None


def _legacy_snapshot(snapshot: Any) -> dict[str, object] | None:
    if not isinstance(snapshot, Mapping) or set(snapshot) != _LEGACY_SNAPSHOT_KEYS:
        return None
    legs = snapshot.get("order_legs")
    contract_spec = snapshot.get("contract_spec")
    if (
        not isinstance(legs, list)
        or len(legs) != 2
        or any(
            not isinstance(leg, Mapping) or set(leg) != _LEGACY_LEG_KEYS
            for leg in legs
        )
        or not isinstance(contract_spec, Mapping)
        or set(contract_spec) != {"contract_value", "quantity_step", "min_quantity"}
        or not _legacy_snapshot_values_are_valid(snapshot)
    ):
        return None
    return json.loads(json.dumps(dict(snapshot), ensure_ascii=False))


def legacy_finalized_snapshot_from_full_draft(
    draft: Any,
) -> dict[str, object] | None:
    if not _legacy_full_draft_is_exact(draft):
        return None
    legs = draft["order_legs"]
    contract_spec = draft["contract_spec"]
    if (
        not isinstance(legs, list)
        or len(legs) != 2
        or any(not isinstance(leg, Mapping) for leg in legs)
        or not isinstance(contract_spec, Mapping)
    ):
        return None
    projected = {
        "strategy_instance_id": draft["strategy_instance_id"],
        "instrument_id": draft["instrument_id"],
        "stop_loss": draft["stop_loss"],
        "take_profit_legs": draft["take_profit_legs"],
        "risk_budget_usdt": draft["risk_budget_usdt"],
        "contract_spec": {
            key: contract_spec[key]
            for key in ("contract_value", "quantity_step", "min_quantity")
        },
        "order_legs": [
            {key: leg[key] for key in _LEGACY_LEG_KEYS} for leg in legs
        ],
    }
    return _legacy_snapshot(projected)


def _legacy_snapshot_values_are_valid(snapshot: Mapping[str, Any]) -> bool:
    contract = snapshot["contract_spec"]
    legs = snapshot["order_legs"]
    take_profit_legs = snapshot["take_profit_legs"]
    return (
        _nonempty_string(snapshot["strategy_instance_id"])
        and _nonempty_string(snapshot["instrument_id"])
        and _positive_number(snapshot["stop_loss"])
        and _positive_number(snapshot["risk_budget_usdt"])
        and all(_positive_number(contract[key]) for key in contract)
        and isinstance(take_profit_legs, list)
        and 1 <= len(take_profit_legs) <= 5
        and all(_legacy_take_profit_is_exact(row) for row in take_profit_legs)
        and all(_legacy_snapshot_leg_values_are_valid(row) for row in legs)
    )


def _legacy_full_draft_is_exact(draft: Any) -> bool:
    if not isinstance(draft, Mapping) or set(draft) != _LEGACY_FULL_DRAFT_KEYS:
        return False
    contract = draft["contract_spec"]
    source = draft["source"]
    legs = draft["order_legs"]
    take_profit_legs = draft["take_profit_legs"]
    stale = draft["entry_preamble_assembly"]
    return (
        isinstance(contract, Mapping)
        and set(contract) == _LEGACY_FULL_CONTRACT_KEYS
        and isinstance(source, Mapping)
        and set(source) == _LEGACY_FULL_SOURCE_KEYS
        and isinstance(stale, Mapping)
        and set(stale) == _STALE_EVIDENCE_KEYS
        and isinstance(legs, list)
        and len(legs) == 2
        and all(
            isinstance(leg, Mapping)
            and set(leg) == _LEGACY_FULL_LEG_KEYS
            and _legacy_full_leg_values_are_valid(leg)
            for leg in legs
        )
        and isinstance(take_profit_legs, list)
        and 1 <= len(take_profit_legs) <= 5
        and all(_legacy_take_profit_is_exact(row) for row in take_profit_legs)
        and isinstance(draft["blocking_reason_codes"], list)
        and all(isinstance(value, str) for value in draft["blocking_reason_codes"])
        and type(draft["dry_run_only"]) is bool
        and type(draft["executable"]) is bool
        and draft["dry_run_only"] is False
        and draft["executable"] is True
        and isinstance(draft["notes"], list)
        and all(isinstance(value, str) for value in draft["notes"])
        and _nonempty_string(draft["instrument_id"])
        and contract["instrument_id"] == draft["instrument_id"]
        and all(
            _positive_number(contract[key])
            for key in (
                "contract_value",
                "min_quantity",
                "price_tick",
                "quantity_step",
            )
        )
        and draft["margin_mode"] in {"cross", "isolated"}
        and draft["position_mode"] in {"split", "merge"}
        and _positive_number(draft["risk_budget_usdt"])
        and _positive_number(draft["stop_loss"])
        and _nonempty_string(draft["strategy_instance_id"])
        and _nonempty_string(draft["symbol"])
        and draft["venue"] == "deepcoin"
        and _nonempty_string(source["kol_id"])
        and _nonempty_string(source["kol_code"])
        and isinstance(source["chat_id"], int)
        and not isinstance(source["chat_id"], bool)
        and isinstance(source["message_id"], int)
        and not isinstance(source["message_id"], bool)
        and source["message_id"] > 0
    )


def legacy_finalized_binding_payload_is_exact(payload: Any) -> bool:
    if (
        not isinstance(payload, Mapping)
        or set(payload) != _LEGACY_BINDING_PAYLOAD_KEYS
    ):
        return False
    draft = payload["draft"]
    submitted = payload["submitted_orders"]
    if (
        not _legacy_full_draft_is_exact(draft)
        or not isinstance(submitted, list)
        or len(submitted) != 2
        or len(submitted) != len(draft["order_legs"])
    ):
        return False
    return all(
        _legacy_submitted_order_is_exact(
            row,
            draft=draft,
            leg=draft["order_legs"][index - 1],
            index=index,
        )
        for index, row in enumerate(submitted, 1)
    )


def legacy_finalized_signal_payload_is_exact(payload: Any) -> bool:
    if (
        not isinstance(payload, Mapping)
        or set(payload) != _LEGACY_SIGNAL_PAYLOAD_KEYS
    ):
        return False
    draft = payload["deepcoin_order_draft"]
    source = payload["source"]
    return (
        _legacy_full_draft_is_exact(draft)
        and isinstance(source, Mapping)
        and set(source) == _LEGACY_SIGNAL_SOURCE_KEYS
        and isinstance(source["chat_id"], int)
        and not isinstance(source["chat_id"], bool)
        and isinstance(source["message_id"], int)
        and not isinstance(source["message_id"], bool)
        and source["message_id"] > 0
        and source["chat_id"] == draft["source"]["chat_id"]
        and source["message_id"] == draft["source"]["message_id"]
        and source["side"] in {"long", "short"}
        and source["side"] == draft["order_legs"][0]["position_side"]
        and _nonempty_string(source["symbol"])
        and source["symbol"] == draft["symbol"]
    )


def _legacy_submitted_order_is_exact(
    value: Any,
    *,
    draft: Mapping[str, Any],
    leg: Mapping[str, Any],
    index: int,
) -> bool:
    if not isinstance(value, Mapping) or set(value) != _LEGACY_SUBMITTED_ORDER_KEYS:
        return False
    response = value["response"]
    response_data = response.get("data") if isinstance(response, Mapping) else None
    protection_request = value["protection_request"]
    protection_response = value["protection_response"]
    protection_data = (
        protection_response.get("data")
        if isinstance(protection_response, Mapping)
        else None
    )
    request = value["request"]
    if not isinstance(request, Mapping) or not _exact_request_matches_leg(
        request, expected=leg, full_draft=draft
    ):
        return False
    expected_protection = {
        "slOrdPx": request["slOrdPx"],
        "slTriggerPx": request["slTriggerPx"],
    }
    return (
        isinstance(value["leg_index"], int)
        and not isinstance(value["leg_index"], bool)
        and value["leg_index"] == index
        and value["execution_type"] == "trigger_limit"
        and value["client_order_id"] == leg["client_order_id"]
        and _nonempty_string(value["order_id"])
        and value["pos_id"] is None
        and protection_request == expected_protection
        and isinstance(response, Mapping)
        and set(response) == _LEGACY_SUBMISSION_RESPONSE_KEYS
        and response["code"] == "0"
        and isinstance(response["msg"], str)
        and isinstance(response_data, Mapping)
        and set(response_data) == _LEGACY_SUBMISSION_DATA_KEYS
        and response_data["clOrdId"] == value["client_order_id"]
        and response_data["ordId"] == value["order_id"]
        and response_data["sCode"] == "0"
        and isinstance(response_data["sMsg"], str)
        and isinstance(response_data["tag"], str)
        and isinstance(protection_response, Mapping)
        and set(protection_response) == {"code", "data"}
        and protection_response["code"] == "0"
        and protection_data == {"attached_on_trigger_order": True}
    )


def _legacy_full_leg_values_are_valid(leg: Mapping[str, Any]) -> bool:
    return (
        _positive_number(leg["allocation_pct"])
        and _positive_number(leg["base_asset_estimate"])
        and _nonempty_string(leg["client_order_id"])
        and _positive_number(leg["estimated_stop_loss_usdt"])
        and leg["order_type"] == "limit"
        and leg["position_side"] in {"long", "short"}
        and _positive_number(leg["price"])
        and _positive_number(leg["quantity"])
        and _nonempty_string(leg["quantity_unit"])
        and _positive_number(leg["risk_budget_usdt"])
        and leg["side"] in {"buy", "sell"}
    )


def _legacy_snapshot_leg_values_are_valid(leg: Mapping[str, Any]) -> bool:
    return (
        _positive_number(leg["price"])
        and leg["order_type"] == "limit"
        and _positive_number(leg["allocation_pct"])
        and _positive_number(leg["risk_budget_usdt"])
        and _positive_number(leg["quantity"])
        and _nonempty_string(leg["quantity_unit"])
        and _positive_number(leg["estimated_stop_loss_usdt"])
        and _nonempty_string(leg["client_order_id"])
    )


def _legacy_take_profit_is_exact(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == _LEGACY_TAKE_PROFIT_KEYS
        and _positive_number(value["allocation_pct"])
        and isinstance(value["index"], int)
        and not isinstance(value["index"], bool)
        and value["index"] > 0
        and value["order_type"] == "limit"
        and _positive_number(value["price"])
    )


def _positive_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
        and value not in {float("inf"), float("-inf")}
    )


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


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
        and _bounded_snapshot(draft) == _bounded_snapshot(snapshot)
    )


def _legacy_signal_has_stale_evidence(
    signal: TradeSignal,
    *,
    assembly_id: int,
    strategy_instance_id: str,
    old_fingerprint: str,
    binding_draft: Any,
    snapshot: Mapping[str, Any],
) -> bool:
    payload = _json_object(signal.payload_json)
    if not legacy_finalized_signal_payload_is_exact(payload):
        return False
    signal_draft = payload.get("deepcoin_order_draft")
    nested = (
        signal_draft.get("entry_preamble_assembly")
        if isinstance(signal_draft, Mapping)
        else None
    )
    return (
        isinstance(binding_draft, Mapping)
        and signal_draft == binding_draft
        and _stale_evidence_matches(
            nested,
            assembly_id=assembly_id,
            strategy_instance_id=strategy_instance_id,
            old_fingerprint=old_fingerprint,
        )
        and legacy_finalized_snapshot_from_full_draft(signal_draft) == snapshot
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
    all_expected_legs = snapshot.get("order_legs")
    selected_indices = snapshot.get("selected_entry_leg_indices")
    instrument = str(snapshot.get("instrument_id") or "").upper()
    if not isinstance(all_expected_legs, list) or not isinstance(selected_indices, list):
        return False
    if any(
        isinstance(index, bool)
        or not isinstance(index, int)
        or index < 1
        or index > len(all_expected_legs)
        for index in selected_indices
    ):
        return False
    expected_legs = [all_expected_legs[index - 1] for index in selected_indices]
    if len(legs) != len(expected_legs):
        return False
    if instrument != f"{symbol.upper()}-USDT-SWAP":
        return False
    for index, (row, expected) in enumerate(zip(legs, expected_legs), 1):
        request = _json_object(row.request_json)
        if not isinstance(expected, Mapping) or request is None:
            return False
        draft_order_type = str(expected.get("order_type") or "").lower()
        expected_order_kind = {
            "limit": "trigger_limit",
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
            or not _request_matches_selected_leg(
                request,
                expected=expected,
                snapshot=snapshot,
                instrument=instrument,
            )
        ):
            return False
    return True


def legacy_finalized_execution_legs_match(
    legs: list[Any],
    *,
    full_draft: Mapping[str, Any],
    final_leg_count: Any,
    strategy_instance_id: str,
    symbol: str,
    side: str,
    submitted_orders: Any,
) -> bool:
    expected_legs = full_draft.get("order_legs")
    instrument = str(full_draft.get("instrument_id") or "").upper()
    if (
        isinstance(final_leg_count, bool)
        or not isinstance(final_leg_count, int)
        or not isinstance(expected_legs, list)
        or len(expected_legs) != final_leg_count
        or len(legs) != final_leg_count
        or instrument != f"{symbol.upper()}-USDT-SWAP"
        or not isinstance(submitted_orders, list)
        or len(submitted_orders) != final_leg_count
        or any(
            not isinstance(row, Mapping)
            or set(row) != _LEGACY_SUBMITTED_ORDER_KEYS
            for row in submitted_orders
        )
    ):
        return False
    for index, (row, expected, submitted) in enumerate(
        zip(legs, expected_legs, submitted_orders), 1
    ):
        request_raw = _leg_value(row, "request_json")
        request = (
            dict(request_raw)
            if isinstance(request_raw, Mapping)
            else _json_object(request_raw)
        )
        if not isinstance(expected, Mapping) or request is None:
            return False
        order_type = str(expected.get("order_type") or "").lower()
        expected_kind = {"limit": "trigger_limit", "market": "market"}.get(
            order_type
        )
        if (
            _leg_value(row, "leg_index") != index
            or str(_leg_value(row, "strategy_instance_id") or "")
            != strategy_instance_id
            or str(_leg_value(row, "venue") or "").lower() != "deepcoin"
            or str(_leg_value(row, "status") or "").lower()
            not in {"pending", "submitted", "open", "active"}
            or not str(_leg_value(row, "order_id") or "").strip()
            or _leg_value(row, "order_id") != submitted["order_id"]
            or expected_kind is None
            or str(_leg_value(row, "order_kind") or "").lower() != expected_kind
            or not str(_leg_value(row, "client_order_id") or "").strip()
            or str(_leg_value(row, "client_order_id"))
            != str(expected.get("client_order_id") or "")
            or _leg_value(row, "client_order_id")
            != submitted["client_order_id"]
            or request != submitted["request"]
            or not _exact_request_matches_leg(
                request, expected=expected, full_draft=full_draft
            )
        ):
            return False
    return str(full_draft.get("position_mode") or "") in {"split", "merge"} and (
        str(full_draft.get("symbol") or "").upper() == symbol.upper()
        and all(
            str(leg.get("position_side") or "").lower() == side.lower()
            for leg in expected_legs
        )
    )


def _leg_value(row: Any, field: str) -> Any:
    return row.get(field) if isinstance(row, Mapping) else getattr(row, field, None)


def _exact_request_matches_leg(
    request: Mapping[str, Any],
    *,
    expected: Mapping[str, Any],
    full_draft: Mapping[str, Any],
) -> bool:
    try:
        from telegram_kol_research.recovery_live_submit import (
            RecoveryLiveSubmitError,
            build_deepcoin_market_order_payload,
            build_deepcoin_trigger_order_payload,
        )

        order_type = str(expected.get("order_type") or "").lower()
        if order_type == "market":
            expected_request = build_deepcoin_market_order_payload(
                dict(full_draft), dict(expected)
            )
        elif order_type == "limit":
            expected_request = build_deepcoin_trigger_order_payload(
                dict(full_draft), dict(expected)
            )
        else:
            return False
    except (KeyError, TypeError, ValueError, RecoveryLiveSubmitError):
        return False
    return dict(request) == expected_request


def _request_matches_selected_leg(
    request: Mapping[str, Any],
    *,
    expected: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    instrument: str,
) -> bool:
    order_type = str(expected.get("order_type") or "").lower()
    if str(snapshot.get("instrument_id") or "").upper() != instrument:
        return False
    try:
        from telegram_kol_research.recovery_live_submit import (
            RecoveryLiveSubmitError,
            build_deepcoin_market_order_payload,
            build_deepcoin_trigger_order_payload,
        )

        draft = dict(snapshot)
        leg = dict(expected)
        if order_type == "market":
            expected_request = build_deepcoin_market_order_payload(draft, leg)
        elif order_type == "limit":
            expected_request = build_deepcoin_trigger_order_payload(draft, leg)
        else:
            return False
    except (KeyError, TypeError, ValueError, RecoveryLiveSubmitError):
        return False

    persistence_only_keys = {"merged_from_leg_indices"}
    if set(request) - set(expected_request) - persistence_only_keys:
        return False
    if set(expected_request) - set(request):
        return False
    if any(request[key] != value for key, value in expected_request.items()):
        return False
    return "merged_from_leg_indices" not in request or isinstance(
        request["merged_from_leg_indices"], list
    )


def _event_documents(action: EntryAssemblyFingerprintRepairAction):
    common = {
        "policy_version": action.policy_version,
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

"""Bounded read-only reconciliation of instruction execution evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.instruction_execution_contracts import (
    InstructionExecutionConflictError,
    transition_instruction_execution_contract,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    InstructionExecutionContract,
    StrategyLifecycle,
)


SUBMITTING_STALE_AFTER = timedelta(minutes=2)
READBACK_STATES = frozenset({"submitting", "submit_unknown"})


@dataclass(frozen=True, slots=True)
class ExecutionContradictionFact:
    code: str
    contract_id: int | None = None
    message_instruction_item_id: int | None = None
    raw_message_id: int | None = None


@dataclass(slots=True)
class InstructionExecutionReconciliationResult:
    checked: int = 0
    transitioned: int = 0
    facts: list[ExecutionContradictionFact] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _LocalEvidence:
    binding: ExecutionBinding | None
    legs: tuple[ExecutionOrderLeg, ...]
    instrument_id: str | None


@dataclass(frozen=True, slots=True)
class _ExchangeEvidence:
    complete: bool
    rows: tuple[dict[str, Any], ...]


def reconcile_instruction_execution_contracts(
    session_factory: sessionmaker,
    *,
    client,
    reconciled_at: datetime,
    mode: str,
    limit: int = 20,
) -> InstructionExecutionReconciliationResult:
    """Reconcile bounded contracts without calling any exchange mutation API."""

    if mode not in {"disabled", "shadow", "live"}:
        raise ValueError("execution contract mode must be disabled, shadow, or live")
    result = InstructionExecutionReconciliationResult()
    if mode == "disabled" or limit <= 0:
        return result
    bounded_limit = max(0, min(int(limit), 100))
    now = _aware_utc(reconciled_at)
    with session_factory() as session:
        contracts = (
            session.query(InstructionExecutionContract)
            .order_by(
                InstructionExecutionContract.deadline_at.asc(),
                InstructionExecutionContract.id.asc(),
            )
            .limit(bounded_limit)
            .all()
        )
        contract_ids = [int(row.id) for row in contracts]
    for contract_id in contract_ids:
        with session_factory() as session:
            contract = session.get(InstructionExecutionContract, contract_id)
            if contract is None:
                continue
            session.expunge(contract)
        result.checked += 1
        local = _load_local_evidence(session_factory, contract)
        _append_local_facts(result, contract=contract, local=local, now=now)
        if contract.state == "deferred" and _deadline_elapsed(contract, now=now):
            if _transition(
                session_factory,
                contract=contract,
                new_state="expired",
                reason_code="execution_contract_deadline_elapsed",
                reconciled_at=now,
                evidence_refs=[{"kind": "execution_reconciliation", "result": "overdue"}],
            ):
                result.transitioned += 1
            continue
        if contract.state not in READBACK_STATES:
            continue
        if not local.instrument_id or local.binding is None or not local.legs:
            _append_fact(result, "exchange_snapshot_incomplete", contract)
            continue
        exchange = _read_exact_instrument_snapshot(client, local.instrument_id)
        if not exchange.complete:
            _append_fact(result, "exchange_snapshot_incomplete", contract)
            continue
        evidence_state = _classify_exchange_evidence(local, exchange.rows)
        if evidence_state == "duplicate":
            _append_fact(result, "exchange_evidence_duplicate", contract)
            if contract.state == "submitting" and _submitting_stale(contract, now=now):
                if _transition_unknown(session_factory, contract=contract, reconciled_at=now):
                    result.transitioned += 1
            continue
        if evidence_state == "verified":
            if _transition(
                session_factory,
                contract=contract,
                new_state="verified",
                reason_code="execution_readback_verified",
                reconciled_at=now,
                evidence_refs=[
                    {
                        "kind": "execution_readback",
                        "binding_id": int(local.binding.id),
                        "leg_indices": [int(leg.leg_index) for leg in local.legs],
                    }
                ],
                execution_binding_id=int(local.binding.id),
                terminal_kind="verified_entry",
                completion_scope="full",
            ):
                result.transitioned += 1
            continue
        if evidence_state == "absent":
            if contract.state == "submit_unknown" or not contract.attempted_exchange_write:
                if _transition(
                    session_factory,
                    contract=contract,
                    new_state="failed",
                    reason_code="execution_readback_confirmed_absent",
                    reconciled_at=now,
                    evidence_refs=[
                        {"kind": "execution_readback", "result": "confirmed_absent"}
                    ],
                ):
                    result.transitioned += 1
            elif _submitting_stale(contract, now=now):
                if _transition_unknown(session_factory, contract=contract, reconciled_at=now):
                    result.transitioned += 1
    _append_lifecycle_facts(session_factory, result=result, limit=bounded_limit)
    return result


def _load_local_evidence(
    session_factory: sessionmaker,
    contract: InstructionExecutionContract,
) -> _LocalEvidence:
    with session_factory() as session:
        binding = None
        if contract.execution_binding_id is not None:
            binding = session.get(ExecutionBinding, int(contract.execution_binding_id))
        if binding is None and contract.strategy_instance_id:
            matches = (
                session.query(ExecutionBinding)
                .filter(
                    ExecutionBinding.strategy_instance_id
                    == str(contract.strategy_instance_id),
                    ExecutionBinding.venue == "deepcoin",
                )
                .all()
            )
            binding = matches[0] if len(matches) == 1 else None
        legs: list[ExecutionOrderLeg] = []
        if binding is not None:
            legs = (
                session.query(ExecutionOrderLeg)
                .filter(
                    ExecutionOrderLeg.execution_binding_id == int(binding.id),
                    ExecutionOrderLeg.purpose == "entry",
                )
                .order_by(ExecutionOrderLeg.leg_index.asc())
                .all()
            )
        instrument_id = _binding_instrument_id(binding)
        if binding is not None:
            session.expunge(binding)
        for leg in legs:
            session.expunge(leg)
        return _LocalEvidence(binding, tuple(legs), instrument_id)


def _binding_instrument_id(binding: ExecutionBinding | None) -> str | None:
    if binding is None:
        return None
    try:
        payload = json.loads(binding.payload_json or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}
    draft = payload.get("draft") if isinstance(payload, dict) else None
    instrument_id = draft.get("instrument_id") if isinstance(draft, dict) else None
    if instrument_id:
        return str(instrument_id).upper()
    symbol = str(binding.symbol or "").upper()
    return f"{symbol}-USDT-SWAP" if symbol else None


def _read_exact_instrument_snapshot(client, instrument_id: str) -> _ExchangeEvidence:
    rows: list[dict[str, Any]] = []
    try:
        positions = client.list_positions()
        open_orders = client.list_open_orders()
        order_history = client.list_order_history(inst_id=instrument_id)
        trade_fills = client.list_trade_fills(inst_id=instrument_id)
        trigger_history = client.list_trigger_order_history(inst_id=instrument_id)
        pending_response = client.read_trigger_orders_pending(inst_id=instrument_id)
        pending_rows = (
            pending_response.get("data")
            if isinstance(pending_response, dict)
            else None
        )
    except Exception:
        return _ExchangeEvidence(False, ())
    sources = (
        positions,
        open_orders,
        order_history,
        trade_fills,
        trigger_history,
        pending_rows,
    )
    if any(
        not isinstance(source, list)
        or any(not isinstance(row, dict) for row in source)
        for source in sources
    ):
        return _ExchangeEvidence(False, ())
    for source in sources:
        rows.extend(source)
    return _ExchangeEvidence(True, tuple(rows))


def _classify_exchange_evidence(
    local: _LocalEvidence,
    exchange_rows: tuple[dict[str, Any], ...],
) -> str:
    matched_any = False
    for leg in local.legs:
        pos_id = str(leg.pos_id or "")
        if pos_id:
            pos_matches = {
                _row_identity(row)
                for row in exchange_rows
                if _first_string(row, "posId", "pos_id", "positionId") == pos_id
            }
            pos_matches.discard("")
            if len(pos_matches) > 1:
                return "duplicate"
            if len(pos_matches) == 1 and str(leg.attribution_status or "") == "verified":
                matched_any = True
                continue
        client_order_id = str(leg.client_order_id or "")
        order_id = str(leg.order_id or "")
        matches = {
            _row_identity(row)
            for row in exchange_rows
            if (
                client_order_id
                and _first_string(
                    row, "clOrdId", "clientOrderId", "client_order_id"
                )
                == client_order_id
            )
            or (
                order_id
                and _first_string(row, "ordId", "orderId", "order_id", "algoId")
                == order_id
            )
        }
        matches.discard("")
        if len(matches) > 1:
            return "duplicate"
        if len(matches) == 1:
            matched_any = True
            continue
        return "absent"
    return "verified" if matched_any else "absent"


def _row_identity(row: dict[str, Any]) -> str:
    order_id = _first_string(row, "ordId", "orderId", "order_id", "algoId")
    client_id = _first_string(row, "clOrdId", "clientOrderId", "client_order_id")
    pos_id = _first_string(row, "posId", "pos_id", "positionId")
    return "|".join(value for value in (order_id, client_id, pos_id) if value)


def _first_string(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in {None, ""}:
            return str(value)
    return ""


def _append_local_facts(
    result: InstructionExecutionReconciliationResult,
    *,
    contract: InstructionExecutionContract,
    local: _LocalEvidence,
    now: datetime,
) -> None:
    if contract.state == "deferred" and _deadline_elapsed(contract, now=now):
        _append_fact(result, "deferred_overdue", contract)
    if contract.state == "submitting" and _submitting_stale(contract, now=now):
        _append_fact(result, "submitting_stale", contract)
    if contract.state == "verified" and local.binding is None:
        _append_fact(result, "verified_without_binding", contract)
    if local.binding is not None and contract.state != "verified":
        _append_fact(result, "binding_without_verified_contract", contract)
    if contract.state == "verified" and contract.completion_scope == "partial":
        _append_fact(result, "multi_leg_partial", contract)


def _append_lifecycle_facts(
    session_factory: sessionmaker,
    *,
    result: InstructionExecutionReconciliationResult,
    limit: int,
) -> None:
    remaining = max(0, limit - len(result.facts))
    if remaining == 0:
        return
    with session_factory() as session:
        rows = (
            session.query(StrategyLifecycle)
            .filter(
                StrategyLifecycle.lifecycle_status == "entered",
                StrategyLifecycle.execution_binding_id.is_(None),
            )
            .order_by(StrategyLifecycle.id.asc())
            .limit(remaining)
            .all()
        )
        for row in rows:
            result.facts.append(
                ExecutionContradictionFact(
                    "lifecycle_entered_without_binding",
                    raw_message_id=None,
                )
            )


def _append_fact(
    result: InstructionExecutionReconciliationResult,
    code: str,
    contract: InstructionExecutionContract,
) -> None:
    if len(result.facts) >= 100:
        return
    result.facts.append(
        ExecutionContradictionFact(
            code,
            contract_id=int(contract.id),
            message_instruction_item_id=int(contract.message_instruction_item_id),
            raw_message_id=int(contract.raw_message_id),
        )
    )


def _transition_unknown(
    session_factory: sessionmaker,
    *,
    contract: InstructionExecutionContract,
    reconciled_at: datetime,
) -> bool:
    return _transition(
        session_factory,
        contract=contract,
        new_state="submit_unknown",
        reason_code="stale_submission_requires_readback",
        reconciled_at=reconciled_at,
        evidence_refs=[{"kind": "execution_readback", "result": "not_visible"}],
    )


def _transition(
    session_factory: sessionmaker,
    *,
    contract: InstructionExecutionContract,
    new_state: str,
    reason_code: str,
    reconciled_at: datetime,
    evidence_refs: list[dict[str, object]],
    execution_binding_id: int | None = None,
    terminal_kind: str | None = None,
    completion_scope: str | None = None,
) -> bool:
    try:
        transition_instruction_execution_contract(
            session_factory,
            contract_id=int(contract.id),
            expected_state=str(contract.state),
            expected_version=int(contract.state_version),
            new_state=new_state,
            reason_code=reason_code,
            evidence_refs=evidence_refs,
            transitioned_at=reconciled_at,
            execution_binding_id=execution_binding_id,
            terminal_kind=terminal_kind,
            completion_scope=completion_scope,
        )
    except InstructionExecutionConflictError:
        return False
    return True


def _deadline_elapsed(contract: InstructionExecutionContract, *, now: datetime) -> bool:
    return contract.deadline_at is not None and now >= _aware_utc(contract.deadline_at)


def _submitting_stale(contract: InstructionExecutionContract, *, now: datetime) -> bool:
    progress = contract.last_progress_at or contract.updated_at or contract.created_at
    return now - _aware_utc(progress) >= SUBMITTING_STALE_AFTER


def _aware_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)

"""Bounded read-only reconciliation of instruction execution evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, update
from sqlalchemy.exc import IntegrityError
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
    TradeSignal,
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
    resolved_transient_fact_keys: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _LocalEvidence:
    binding: ExecutionBinding | None
    legs: tuple["_ExpectedLeg", ...]
    instrument_id: str | None
    signal: "_SignalIdentity | None" = None
    draft: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _ExpectedLeg:
    leg_index: int
    order_kind: str
    client_order_id: str | None = None
    order_id: str | None = None
    pos_id: str | None = None
    attribution_status: str | None = None
    local_status: str | None = None


@dataclass(frozen=True, slots=True)
class _SignalIdentity:
    kol_id: str
    chat_id: int
    message_id: int
    symbol: str
    side: str
    venue: str
    strategy_instance_id: str | None


@dataclass(frozen=True, slots=True)
class _ExchangeEvidence:
    complete: bool
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _ClassifiedEvidence:
    state: str
    matched_rows: tuple[dict[str, Any] | None, ...]


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
    comparable_now = now.replace(tzinfo=None)
    with session_factory() as session:
        contracts = (
            session.query(InstructionExecutionContract)
            .filter(
                or_(
                    InstructionExecutionContract.state.in_(READBACK_STATES),
                    and_(
                        InstructionExecutionContract.state == "deferred",
                        InstructionExecutionContract.deadline_at.is_not(None),
                        InstructionExecutionContract.deadline_at <= comparable_now,
                    ),
                    and_(
                        InstructionExecutionContract.state == "verified",
                        InstructionExecutionContract.terminal_kind
                        == "verified_entry",
                        or_(
                            InstructionExecutionContract.execution_binding_id.is_(None),
                            InstructionExecutionContract.completion_scope == "partial",
                        ),
                    ),
                )
            )
            .order_by(
                InstructionExecutionContract.updated_at.asc(),
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
            _rotate_reconciliation_candidate(
                session_factory, contract=contract, checked_at=now
            )
            continue
        if not local.instrument_id or not local.legs:
            _append_fact(result, "exchange_snapshot_incomplete", contract)
            _rotate_reconciliation_candidate(session_factory, contract=contract, checked_at=now)
            continue
        exchange = _read_exact_instrument_snapshot(client, local.instrument_id)
        if not exchange.complete:
            _append_fact(result, "exchange_snapshot_incomplete", contract)
            _rotate_reconciliation_candidate(session_factory, contract=contract, checked_at=now)
            continue
        result.resolved_transient_fact_keys.append(
            f"{contract.id}-exchange_snapshot_incomplete"
        )
        classified = _classify_exchange_evidence(local, exchange.rows)
        evidence_state = classified.state
        if evidence_state != "duplicate":
            result.resolved_transient_fact_keys.append(
                f"{contract.id}-exchange_evidence_duplicate"
            )
        if evidence_state == "duplicate":
            _append_fact(result, "exchange_evidence_duplicate", contract)
            if contract.state == "submitting" and _submitting_stale(contract, now=now):
                if _transition_unknown(session_factory, contract=contract, reconciled_at=now):
                    result.transitioned += 1
            else:
                _rotate_reconciliation_candidate(session_factory, contract=contract, checked_at=now)
            continue
        if evidence_state == "verified":
            binding_id = _persist_binding_from_readback(
                session_factory,
                contract=contract,
                local=local,
                matched_rows=classified.matched_rows,
                reconciled_at=now,
            )
            if binding_id is None:
                _append_fact(result, "exchange_snapshot_incomplete", contract)
                _rotate_reconciliation_candidate(
                    session_factory, contract=contract, checked_at=now
                )
                continue
            if _transition(
                session_factory,
                contract=contract,
                new_state="verified",
                reason_code="execution_readback_verified",
                reconciled_at=now,
                evidence_refs=[
                    {
                        "kind": "execution_readback",
                        "binding_id": binding_id,
                        "leg_indices": [int(leg.leg_index) for leg in local.legs],
                    }
                ],
                execution_binding_id=binding_id,
                terminal_kind="verified_entry",
                completion_scope="full",
            ):
                result.transitioned += 1
            continue
        if evidence_state == "verified_partial":
            binding_id = _persist_binding_from_readback(
                session_factory,
                contract=contract,
                local=local,
                matched_rows=classified.matched_rows,
                reconciled_at=now,
            )
            if binding_id is None:
                _append_fact(result, "exchange_snapshot_incomplete", contract)
                _rotate_reconciliation_candidate(session_factory, contract=contract, checked_at=now)
                continue
            if _transition(
                session_factory,
                contract=contract,
                new_state="verified",
                reason_code="execution_readback_verified_partial",
                reconciled_at=now,
                evidence_refs=[
                    {
                        "kind": "execution_readback",
                        "binding_id": binding_id,
                        "result": "verified_partial",
                    }
                ],
                execution_binding_id=binding_id,
                terminal_kind="verified_entry",
                completion_scope="partial",
            ):
                result.transitioned += 1
            continue
        if evidence_state == "refusal":
            if local.binding is not None:
                binding_id = _persist_binding_from_readback(
                    session_factory,
                    contract=contract,
                    local=local,
                    matched_rows=classified.matched_rows,
                    reconciled_at=now,
                )
                if binding_id is None:
                    _append_fact(result, "exchange_snapshot_incomplete", contract)
                    _rotate_reconciliation_candidate(session_factory, contract=contract, checked_at=now)
                    continue
            if _transition(
                session_factory,
                contract=contract,
                new_state="verified",
                reason_code="execution_readback_verified_refusal",
                reconciled_at=now,
                evidence_refs=[
                    {"kind": "execution_readback", "result": "verified_refusal"}
                ],
                terminal_kind="verified_refusal",
                completion_scope="full",
            ):
                result.transitioned += 1
            continue
        if evidence_state == "partial":
            _append_fact(result, "multi_leg_partial", contract)
            if contract.state == "submitting" and _submitting_stale(contract, now=now):
                if _transition_unknown(session_factory, contract=contract, reconciled_at=now):
                    result.transitioned += 1
            else:
                _rotate_reconciliation_candidate(session_factory, contract=contract, checked_at=now)
            continue
        if evidence_state == "incomplete":
            _append_fact(result, "exchange_snapshot_incomplete", contract)
            if contract.state == "submitting" and _submitting_stale(contract, now=now):
                if _transition_unknown(session_factory, contract=contract, reconciled_at=now):
                    result.transitioned += 1
            else:
                _rotate_reconciliation_candidate(session_factory, contract=contract, checked_at=now)
            continue
        if evidence_state == "absent":
            durable_absence = _all_legs_durably_absent(local.legs)
            if not contract.attempted_exchange_write or durable_absence:
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
            elif contract.state == "submitting" and _submitting_stale(contract, now=now):
                if _transition_unknown(session_factory, contract=contract, reconciled_at=now):
                    result.transitioned += 1
            else:
                _rotate_reconciliation_candidate(session_factory, contract=contract, checked_at=now)
    _append_lifecycle_facts(session_factory, result=result, limit=bounded_limit)
    return result


def _load_local_evidence(
    session_factory: sessionmaker,
    contract: InstructionExecutionContract,
) -> _LocalEvidence:
    with session_factory() as session:
        signal = (
            session.get(TradeSignal, int(contract.trade_signal_id))
            if contract.trade_signal_id is not None
            else None
        )
        draft = _trade_signal_draft(signal)
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
        stored_legs: list[ExecutionOrderLeg] = []
        if binding is not None:
            stored_legs = (
                session.query(ExecutionOrderLeg)
                .filter(
                    ExecutionOrderLeg.execution_binding_id == int(binding.id),
                    ExecutionOrderLeg.purpose == "entry",
                )
                .order_by(ExecutionOrderLeg.leg_index.asc())
                .all()
            )
        legs = tuple(
            _ExpectedLeg(
                leg_index=int(leg.leg_index),
                order_kind=str(leg.order_kind or "unknown"),
                client_order_id=str(leg.client_order_id or "") or None,
                order_id=str(leg.order_id or "") or None,
                pos_id=str(leg.pos_id or "") or None,
                attribution_status=str(leg.attribution_status or "") or None,
                local_status=str(leg.status or "") or None,
            )
            for leg in stored_legs
        )
        if not legs:
            legs = _draft_expected_legs(draft)
        instrument_id = _binding_instrument_id(binding) or _draft_instrument_id(draft)
        signal_identity = (
            _SignalIdentity(
                kol_id=str(signal.kol_id),
                chat_id=int(signal.chat_id),
                message_id=int(signal.message_id),
                symbol=str(signal.symbol).upper(),
                side=str(signal.side).lower(),
                venue=str(signal.venue or "deepcoin").lower(),
                strategy_instance_id=(
                    str(signal.strategy_instance_id)
                    if signal.strategy_instance_id
                    else contract.strategy_instance_id
                ),
            )
            if signal is not None
            else None
        )
        if binding is not None:
            session.expunge(binding)
        return _LocalEvidence(binding, legs, instrument_id, signal_identity, draft)


def _trade_signal_draft(signal: TradeSignal | None) -> dict[str, Any] | None:
    if signal is None:
        return None
    try:
        payload = json.loads(signal.payload_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    draft = payload.get("deepcoin_order_draft") if isinstance(payload, dict) else None
    return draft if isinstance(draft, dict) else None


def _draft_expected_legs(draft: dict[str, Any] | None) -> tuple[_ExpectedLeg, ...]:
    if not isinstance(draft, dict):
        return ()
    order_legs = draft.get("order_legs")
    if not isinstance(order_legs, list):
        return ()
    selected = draft.get("selected_entry_leg_indices")
    if selected is None:
        selected = list(range(1, len(order_legs) + 1))
    if not isinstance(selected, list):
        return ()
    expected: list[_ExpectedLeg] = []
    for raw_index in selected:
        if type(raw_index) is not int or raw_index < 1 or raw_index > len(order_legs):
            return ()
        row = order_legs[raw_index - 1]
        if not isinstance(row, dict):
            return ()
        client_order_id = str(row.get("client_order_id") or "").strip()
        if not client_order_id:
            return ()
        expected.append(
            _ExpectedLeg(
                leg_index=raw_index,
                order_kind=str(row.get("order_type") or "unknown").lower(),
                client_order_id=client_order_id,
            )
        )
    return tuple(expected)


def _draft_instrument_id(draft: dict[str, Any] | None) -> str | None:
    if not isinstance(draft, dict):
        return None
    instrument_id = str(draft.get("instrument_id") or "").strip()
    return instrument_id.upper() or None


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
        ("positions", positions, False),
        ("open_orders", open_orders, False),
        ("order_history", order_history, True),
        ("trade_fills", trade_fills, True),
        ("trigger_history", trigger_history, True),
        ("pending_triggers", pending_rows, True),
    )
    if any(
        not isinstance(source, list)
        or any(not isinstance(row, dict) for row in source)
        for _, source, _ in sources
    ):
        return _ExchangeEvidence(False, ())
    for source_name, source, endpoint_scoped in sources:
        for row in source:
            row_instrument = _first_string(row, "instId", "instrument_id", "symbol")
            if not endpoint_scoped and row_instrument.upper() != instrument_id.upper():
                continue
            annotated = dict(row)
            annotated["__reconciliation_source"] = source_name
            rows.append(annotated)
    return _ExchangeEvidence(True, tuple(rows))


def _classify_exchange_evidence(
    local: _LocalEvidence,
    exchange_rows: tuple[dict[str, Any], ...],
) -> _ClassifiedEvidence:
    outcomes: list[str] = []
    matched_rows: list[dict[str, Any] | None] = []
    for leg in local.legs:
        pos_id = str(leg.pos_id or "").strip()
        client_order_id = str(leg.client_order_id or "").strip()
        order_id = str(leg.order_id or "").strip()
        matches = [
            row
            for row in exchange_rows
            if (
                pos_id
                and str(leg.attribution_status or "").lower() == "verified"
                and _first_string(row, "posId", "pos_id", "positionId") == pos_id
            )
            or (
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
        ]
        identities = {_row_identity(row) for row in matches}
        identities.discard("")
        if len(identities) > 1:
            return _ClassifiedEvidence("duplicate", tuple(matched_rows))
        if not matches:
            outcomes.append("absent")
            matched_rows.append(None)
            continue
        classifications = {_classify_exchange_row(row) for row in matches}
        if len(classifications) > 1:
            return _ClassifiedEvidence("duplicate", tuple(matched_rows))
        outcome = classifications.pop()
        outcomes.append(outcome)
        matched_rows.append(matches[0])

    unique = set(outcomes)
    if unique == {"positive"}:
        state = "verified"
    elif unique == {"negative"}:
        state = "refusal"
    elif unique == {"positive", "negative"}:
        state = "verified_partial"
    elif unique == {"absent"}:
        state = "absent"
    elif "unknown" in unique:
        state = "incomplete"
    else:
        state = "partial"
    return _ClassifiedEvidence(state, tuple(matched_rows))


_POSITIVE_EXCHANGE_STATES = frozenset(
    {
        "open", "live", "pending", "submitted", "active", "partially_filled",
        "partiallyfilled", "filled", "completed", "done", "succeeded", "success",
    }
)
_NEGATIVE_EXCHANGE_STATES = frozenset(
    {"canceled", "cancelled", "rejected", "expired", "failed", "error"}
)
_DURABLE_ABSENCE_STATES = frozenset(
    {"canceled", "cancelled", "rejected", "expired", "failed"}
)


def _classify_exchange_row(row: dict[str, Any]) -> str:
    source = str(row.get("__reconciliation_source") or "")
    if source in {"positions", "trade_fills"}:
        return "positive"
    if source in {"open_orders", "pending_triggers"}:
        state = _first_string(
            row, "state", "status", "ordStatus", "orderStatus", "algoStatus"
        ).strip().lower()
        return "negative" if state in _NEGATIVE_EXCHANGE_STATES else "positive"
    state = _first_string(
        row, "state", "status", "ordStatus", "orderStatus", "algoStatus"
    ).strip().lower()
    if state in _POSITIVE_EXCHANGE_STATES:
        return "positive"
    if state in _NEGATIVE_EXCHANGE_STATES:
        return "negative"
    return "unknown"


def _all_legs_durably_absent(legs: tuple[_ExpectedLeg, ...]) -> bool:
    return bool(legs) and all(
        not leg.pos_id
        and str(leg.local_status or "").strip().lower() in _DURABLE_ABSENCE_STATES
        for leg in legs
    )


def _persist_binding_from_readback(
    session_factory: sessionmaker,
    *,
    contract: InstructionExecutionContract,
    local: _LocalEvidence,
    matched_rows: tuple[dict[str, Any] | None, ...],
    reconciled_at: datetime,
) -> int | None:
    """Persist exact immutable-draft/readback evidence without exchange writes."""

    if len(matched_rows) != len(local.legs) or any(row is None for row in matched_rows):
        return None
    recovered: list[dict[str, str | int | None]] = []
    for leg, row in zip(local.legs, matched_rows, strict=True):
        assert row is not None
        outcome = _classify_exchange_row(row)
        if outcome not in {"positive", "negative"}:
            return None
        recovered.append(
            {
                "leg_index": int(leg.leg_index),
                "order_kind": leg.order_kind,
                "client_order_id": leg.client_order_id,
                "order_id": _first_string(
                    row, "ordId", "orderId", "order_id", "algoId"
                ) or None,
                "pos_id": _first_string(row, "posId", "pos_id", "positionId") or None,
                "status": _recovered_leg_status(row),
                "outcome": outcome,
            }
        )
    if local.binding is None and not any(
        row["outcome"] == "positive" for row in recovered
    ):
        return None
    signal = local.signal
    if local.binding is None and (signal is None or local.draft is None):
        return None
    try:
        with session_factory() as session:
            binding = (
                session.get(ExecutionBinding, int(local.binding.id))
                if local.binding is not None
                else session.query(ExecutionBinding)
                .filter(
                    ExecutionBinding.venue == signal.venue,
                    ExecutionBinding.chat_id == signal.chat_id,
                    ExecutionBinding.message_id == signal.message_id,
                    ExecutionBinding.symbol == signal.symbol,
                    ExecutionBinding.side == signal.side,
                )
                .one_or_none()
            )
            if binding is None:
                assert signal is not None
                binding = ExecutionBinding(
                    strategy_instance_id=signal.strategy_instance_id,
                    kol_id=signal.kol_id,
                    chat_id=signal.chat_id,
                    message_id=signal.message_id,
                    symbol=signal.symbol,
                    side=signal.side,
                    venue=signal.venue,
                )
                session.add(binding)
                session.flush()
            elif (
                signal is not None
                and signal.strategy_instance_id
                and binding.strategy_instance_id != signal.strategy_instance_id
            ):
                return None
            strategy_instance_id = (
                signal.strategy_instance_id
                if signal is not None
                else binding.strategy_instance_id
            )
            venue = signal.venue if signal is not None else str(binding.venue)
            binding.strategy_instance_id = strategy_instance_id
            binding.order_id = _join_exact_ids(recovered, "order_id")
            binding.client_order_id = _join_exact_ids(recovered, "client_order_id")
            binding.pos_id = _join_exact_ids(recovered, "pos_id")
            if local.draft is not None:
                binding.margin_mode = str(local.draft.get("margin_mode") or "cross")
                binding.position_mode = str(local.draft.get("position_mode") or "split")
                binding.payload_json = json.dumps(
                    {"draft": local.draft, "recovered_orders": recovered},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            binding.last_exchange_status = "readback_recovered"
            binding.recovered_at = reconciled_at
            if all(row["outcome"] == "negative" for row in recovered):
                binding.status = _terminal_binding_status(recovered)
            else:
                binding.status = "active" if binding.pos_id else "open"
            binding.updated_at = reconciled_at
            for row in recovered:
                leg = (
                    session.query(ExecutionOrderLeg)
                    .filter(
                        ExecutionOrderLeg.execution_binding_id == int(binding.id),
                        ExecutionOrderLeg.purpose == "entry",
                        ExecutionOrderLeg.leg_index == int(row["leg_index"]),
                    )
                    .one_or_none()
                )
                if leg is None:
                    leg = ExecutionOrderLeg(
                        execution_binding_id=int(binding.id),
                        purpose="entry",
                        leg_index=int(row["leg_index"]),
                        created_at=reconciled_at,
                    )
                    session.add(leg)
                elif (
                    leg.client_order_id
                    and leg.client_order_id != row["client_order_id"]
                ):
                    return None
                leg.strategy_instance_id = strategy_instance_id
                leg.order_kind = str(row["order_kind"] or "unknown")
                leg.order_id = str(row["order_id"] or "") or None
                leg.client_order_id = str(row["client_order_id"] or "") or None
                leg.pos_id = str(row["pos_id"] or "") or None
                leg.venue = venue
                leg.attribution_status = "verified" if row["pos_id"] else "unassigned"
                leg.attribution_evidence_json = json.dumps(
                    {"source": "exact_instruction_execution_readback"},
                    sort_keys=True,
                )
                leg.last_verified_at = reconciled_at
                leg.status = str(row["status"])
                leg.terminal_reason = (
                    str(row["status"]) if row["outcome"] == "negative" else None
                )
                leg.response_json = json.dumps(
                    {
                        "order_id": row["order_id"],
                        "client_order_id": row["client_order_id"],
                        "pos_id": row["pos_id"],
                    },
                    sort_keys=True,
                )
                leg.updated_at = reconciled_at
            lifecycle_matches = (
                session.query(StrategyLifecycle)
                .filter(
                    StrategyLifecycle.chat_id == int(binding.chat_id),
                    StrategyLifecycle.message_id == int(binding.message_id),
                    StrategyLifecycle.symbol == str(binding.symbol),
                    StrategyLifecycle.side == str(binding.side),
                )
                .all()
            )
            if len(lifecycle_matches) > 1:
                return None
            if lifecycle_matches:
                if lifecycle_matches[0].execution_binding_id not in {
                    None,
                    int(binding.id),
                }:
                    return None
                lifecycle_matches[0].execution_binding_id = int(binding.id)
                lifecycle_matches[0].updated_at = reconciled_at
            binding_id = int(binding.id)
            session.commit()
            return binding_id
    except IntegrityError:
        # A concurrent reconciler may have persisted the same evidence. Reuse it
        # only when every immutable client id still matches exactly.
        with session_factory() as session:
            query = session.query(ExecutionBinding)
            if signal is None:
                query = query.filter(
                    ExecutionBinding.id == (
                        int(local.binding.id) if local.binding is not None else -1
                    )
                )
            else:
                query = query.filter(
                    ExecutionBinding.venue == signal.venue,
                    ExecutionBinding.chat_id == signal.chat_id,
                    ExecutionBinding.message_id == signal.message_id,
                    ExecutionBinding.symbol == signal.symbol,
                    ExecutionBinding.side == signal.side,
                    ExecutionBinding.strategy_instance_id
                    == signal.strategy_instance_id,
                )
            binding = query.one_or_none()
            if binding is None:
                return None
            expected_binding_status = (
                _terminal_binding_status(recovered)
                if all(row["outcome"] == "negative" for row in recovered)
                else (
                    "active"
                    if _join_exact_ids(recovered, "pos_id")
                    else "open"
                )
            )
            if (
                binding.order_id != _join_exact_ids(recovered, "order_id")
                or binding.client_order_id
                != _join_exact_ids(recovered, "client_order_id")
                or binding.pos_id != _join_exact_ids(recovered, "pos_id")
                or str(binding.status or "").lower() != expected_binding_status
                or str(binding.last_exchange_status or "") != "readback_recovered"
            ):
                return None
            legs = (
                session.query(ExecutionOrderLeg)
                .filter(
                    ExecutionOrderLeg.execution_binding_id == int(binding.id),
                    ExecutionOrderLeg.purpose == "entry",
                )
                .order_by(ExecutionOrderLeg.leg_index.asc())
                .all()
            )
            expected = [
                (
                    int(row["leg_index"]),
                    row["client_order_id"],
                    row["order_id"],
                    row["pos_id"],
                    row["status"],
                )
                for row in recovered
            ]
            actual = [
                (
                    int(leg.leg_index),
                    leg.client_order_id,
                    leg.order_id,
                    leg.pos_id,
                    str(leg.status or "").lower(),
                )
                for leg in legs
            ]
            if actual != expected:
                return None
            lifecycle_matches = (
                session.query(StrategyLifecycle)
                .filter(
                    StrategyLifecycle.chat_id == int(binding.chat_id),
                    StrategyLifecycle.message_id == int(binding.message_id),
                    StrategyLifecycle.symbol == str(binding.symbol),
                    StrategyLifecycle.side == str(binding.side),
                )
                .all()
            )
            if len(lifecycle_matches) > 1 or (
                lifecycle_matches
                and lifecycle_matches[0].execution_binding_id != int(binding.id)
            ):
                return None
            return int(binding.id)


def _recovered_leg_status(row: dict[str, Any]) -> str:
    if str(row.get("__reconciliation_source") or "") in {"positions", "trade_fills"}:
        return "active" if _first_string(row, "posId", "pos_id", "positionId") else "filled"
    state = _first_string(
        row, "state", "status", "ordStatus", "orderStatus", "algoStatus"
    ).strip().lower()
    return (
        state
        if state in _POSITIVE_EXCHANGE_STATES | _NEGATIVE_EXCHANGE_STATES
        else "open"
    )


def _terminal_binding_status(
    rows: list[dict[str, str | int | None]],
) -> str:
    states = {str(row.get("status") or "").lower() for row in rows}
    for state in ("rejected", "cancelled", "canceled", "expired", "failed"):
        if state in states:
            return "cancelled" if state == "canceled" else state
    return "failed"


def _join_exact_ids(rows: list[dict[str, str | int | None]], key: str) -> str | None:
    values = [str(row.get(key) or "") for row in rows]
    return ",".join(value for value in values if value) or None


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
    if (
        contract.state == "verified"
        and contract.terminal_kind == "verified_entry"
        and local.binding is None
    ):
        _append_fact(result, "verified_without_binding", contract)
    if (
        local.binding is not None
        and str(local.binding.venue or "").lower() == "deepcoin"
        and str(local.binding.status or "").lower() in {"open", "active"}
        and contract.state in {
        "pending", "deferred", "submit_unknown", "failed", "expired"
        }
    ):
        _append_fact(
            result,
            "terminal_contract_with_live_exchange_evidence"
            if contract.state in {"failed", "expired"}
            else "binding_without_verified_contract",
            contract,
        )
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


def _rotate_reconciliation_candidate(
    session_factory: sessionmaker,
    *,
    contract: InstructionExecutionContract,
    checked_at: datetime,
) -> None:
    if contract.state not in {"submitting", "submit_unknown", "verified"}:
        return
    with session_factory() as session:
        session.execute(
            update(InstructionExecutionContract)
            .where(
                InstructionExecutionContract.id == int(contract.id),
                InstructionExecutionContract.state == str(contract.state),
                InstructionExecutionContract.state_version
                == int(contract.state_version),
            )
            .values(updated_at=checked_at)
        )
        session.commit()


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

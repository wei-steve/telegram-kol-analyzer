"""GET-only reconciliation for version-pinned protected entry operations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import aliased, sessionmaker

from telegram_kol_research.deepcoin_execution_operations import (
    contains_credential_marker,
    DeepcoinOperationConflict,
    ExecutionOperationRecord,
    load_operation_bundle,
    record_snapshot_evidence,
    transition_execution_operation,
)
from telegram_kol_research.deepcoin_snapshot_authority import (
    AccountSnapshotEvidence,
    build_exchange_collection_evidence,
)
from telegram_kol_research.models import DeepcoinExecutionOperation
from telegram_kol_research.models import DeepcoinSnapshotEvidence
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import ExecutionOrderLeg
from telegram_kol_research.models import PositionMutationIntent
from telegram_kol_research.models import PositionProtectionLedger
from telegram_kol_research.models import TradeSignal
from telegram_kol_research.position_mutation_intents import (
    PositionMutationIntentError,
    load_validated_set_position_request,
)
from telegram_kol_research.trade_signals import (
    TradeSignalTransitionError,
    finalize_trade_signal_from_execution_operation,
)


_COLLECTION_NAMES = (
    "positions",
    "position_history",
    "open_orders",
    "pending_trigger_orders",
    "order_history",
    "trade_fills",
    "trigger_history",
)
_SAFE_EXCHANGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_OPERATIONS_PER_CYCLE = 128
_MAX_ROW_DEPTH = 12
_MAX_ROW_NODES = 20_000
_MAX_ROW_TEXT = 16_384


@dataclass(slots=True)
class ProtectedEntryReconciliationResult:
    checked: int = 0
    confirmed: int = 0
    unchanged: int = 0
    conflicts: int = 0


@dataclass(frozen=True, slots=True)
class _SnapshotProof:
    complete: bool
    fingerprint: str | None
    row_count: int
    start_generation: int
    end_generation: int
    uid_scope_hash: str | None
    capture_started_at: datetime
    capture_ended_at: datetime
    error_code: str | None


@dataclass(frozen=True, slots=True)
class _EntryMatch:
    disposition: str
    order_id: str | None = None
    pos_id: str | None = None


@dataclass(frozen=True, slots=True)
class _EntryAuthority:
    client_order_id: str
    client_order_ref: str
    instrument_id: str
    leg_index: int
    position_side: str
    quantity: str
    side: str
    request_fingerprint: str
    economics_fingerprint: str
    uid_scope_hash: str
    pre_submit_position_refs: frozenset[str]


def reconcile_protected_entry_operations(
    session_factory: sessionmaker,
    *,
    snapshot: Any,
    reconciled_at: datetime | None = None,
) -> ProtectedEntryReconciliationResult:
    """Reconcile protected operations from one already captured account snapshot.

    No exchange client or writer callback is accepted by this API.  The caller
    must supply the shared, generation-fenced snapshot used by the surrounding
    execution-reconciliation cycle.
    """

    now = _normalized_datetime(reconciled_at or datetime.now(UTC))
    proof = _snapshot_proof(snapshot, captured_at=now)
    with session_factory() as session:
        snapshot_counts = (
            session.query(
                DeepcoinSnapshotEvidence.deepcoin_execution_operation_id.label(
                    "operation_id"
                ),
                func.count(DeepcoinSnapshotEvidence.id).label("snapshot_count"),
            )
            .group_by(DeepcoinSnapshotEvidence.deepcoin_execution_operation_id)
            .subquery()
        )
        operation_ids = [
            int(row_id)
            for (row_id,) in (
                session.query(DeepcoinExecutionOperation.id)
                .outerjoin(
                    snapshot_counts,
                    snapshot_counts.c.operation_id
                    == DeepcoinExecutionOperation.id,
                )
                .filter(
                    DeepcoinExecutionOperation.contract_version == "1",
                    or_(
                        and_(
                            DeepcoinExecutionOperation.state.in_(
                                {"entry_pending_readback", "entry_unknown"}
                            ),
                            DeepcoinExecutionOperation.parent_operation_id.is_(None),
                        ),
                        and_(
                            DeepcoinExecutionOperation.state.in_(
                                {
                                    "protection_pending_readback",
                                    "protection_unknown",
                                }
                            ),
                            DeepcoinExecutionOperation.parent_operation_id.is_not(
                                None
                            ),
                        ),
                    ),
                )
                .order_by(
                    func.coalesce(snapshot_counts.c.snapshot_count, 0),
                    DeepcoinExecutionOperation.updated_at,
                    DeepcoinExecutionOperation.id,
                )
                .limit(_MAX_OPERATIONS_PER_CYCLE)
                .all()
            )
        ]

    result = ProtectedEntryReconciliationResult()
    for operation_id in operation_ids:
        result.checked += 1
        try:
            operation = load_operation_bundle(
                session_factory,
                operation_id=operation_id,
            ).operation
            try:
                evidence = _strict_object(operation.evidence_json)
            except (TypeError, ValueError, RecursionError, UnicodeError):
                snapshot_record = _record_shared_snapshot(
                    session_factory,
                    operation=operation,
                    proof=replace(
                        proof,
                        complete=False,
                        error_code=(
                            "protected_entry_operation_evidence_invalid"
                        ),
                    ),
                )
                _freeze_conflict(
                    session_factory,
                    operation=operation,
                    evidence={
                        "reconciliation_error": "operation_evidence_invalid"
                    },
                    snapshot_evidence_id=snapshot_record.id,
                    reason_code=(
                        "protected_entry_reconciliation_evidence_invalid"
                    ),
                    reconciled_at=now,
                )
                _project_trade_signal_best_effort(
                    session_factory,
                    trade_signal_id=operation.trade_signal_id,
                    reconciled_at=now,
                )
                result.conflicts += 1
                continue
            expected_uid = evidence.get("uid_scope_hash")
            parent_record = None
            parent_evidence = None
            if operation.parent_operation_id is not None:
                try:
                    parent_record, parent_evidence = _parent_authority(
                        session_factory,
                        parent_operation_id=operation.parent_operation_id,
                    )
                except (TypeError, ValueError, RecursionError, UnicodeError):
                    snapshot_record = _record_shared_snapshot(
                        session_factory,
                        operation=operation,
                        proof=replace(
                            proof,
                            complete=False,
                            error_code=(
                                "protected_entry_parent_evidence_invalid"
                            ),
                        ),
                    )
                    _freeze_conflict(
                        session_factory,
                        operation=operation,
                        evidence=evidence,
                        snapshot_evidence_id=snapshot_record.id,
                        reason_code=(
                            "protected_entry_reconciliation_evidence_invalid"
                        ),
                        reconciled_at=now,
                    )
                    _freeze_parent_conflict(
                        session_factory,
                        parent_operation_id=operation.parent_operation_id,
                        snapshot_evidence_id=snapshot_record.id,
                        reconciled_at=now,
                    )
                    _project_trade_signal_best_effort(
                        session_factory,
                        trade_signal_id=operation.trade_signal_id,
                        reconciled_at=now,
                    )
                    result.conflicts += 1
                    continue
                expected_uid = parent_evidence.get("uid_scope_hash")
            scope_conflict = not _is_fingerprint(expected_uid) or (
                proof.uid_scope_hash is not None
                and proof.uid_scope_hash != expected_uid
            )
            snapshot_record = _record_shared_snapshot(
                session_factory,
                operation=operation,
                proof=(
                    replace(
                        proof,
                        complete=False,
                        error_code=(
                            "protected_entry_snapshot_scope_conflict"
                        ),
                    )
                    if scope_conflict
                    else proof
                ),
            )
            if scope_conflict:
                _freeze_conflict(
                    session_factory,
                    operation=operation,
                    evidence=evidence,
                    snapshot_evidence_id=snapshot_record.id,
                    reason_code="protected_entry_reconciliation_scope_conflict",
                    reconciled_at=now,
                )
                _freeze_parent_conflict(
                    session_factory,
                    parent_operation_id=operation.parent_operation_id,
                    snapshot_evidence_id=snapshot_record.id,
                    reconciled_at=now,
                )
                _project_trade_signal_best_effort(
                    session_factory,
                    trade_signal_id=operation.trade_signal_id,
                    reconciled_at=now,
                )
                result.conflicts += 1
                continue
            if not proof.complete:
                result.unchanged += 1
                continue
            if operation.state in {
                "entry_pending_readback",
                "entry_unknown",
            }:
                match = _match_entry_operation(
                    session_factory,
                    operation=operation,
                    evidence=evidence,
                    snapshot=snapshot,
                )
                if match.disposition == "pending":
                    result.unchanged += 1
                    continue
                if match.disposition == "conflict":
                    _freeze_conflict(
                        session_factory,
                        operation=operation,
                        evidence=evidence,
                        snapshot_evidence_id=snapshot_record.id,
                        reason_code=(
                            "protected_entry_reconciliation_identity_conflict"
                        ),
                        reconciled_at=now,
                    )
                    _project_trade_signal_best_effort(
                        session_factory,
                        trade_signal_id=operation.trade_signal_id,
                        reconciled_at=now,
                    )
                    result.conflicts += 1
                    continue
                _confirm_entry(
                    session_factory,
                    operation=operation,
                    evidence=evidence,
                    snapshot_evidence_id=snapshot_record.id,
                    order_id=str(match.order_id),
                    pos_id=str(match.pos_id),
                    proof=proof,
                    reconciled_at=now,
                )
            else:
                disposition = _protection_disposition(
                    session_factory,
                    operation=operation,
                    evidence=evidence,
                    snapshot=snapshot,
                    parent_record=parent_record,
                    parent_evidence=parent_evidence,
                )
                if disposition == "pending":
                    result.unchanged += 1
                    continue
                if disposition == "conflict":
                    _freeze_conflict(
                        session_factory,
                        operation=operation,
                        evidence=evidence,
                        snapshot_evidence_id=snapshot_record.id,
                        reason_code=(
                            "protected_entry_reconciliation_identity_conflict"
                        ),
                        reconciled_at=now,
                    )
                    _freeze_parent_conflict(
                        session_factory,
                        parent_operation_id=operation.parent_operation_id,
                        snapshot_evidence_id=snapshot_record.id,
                        reconciled_at=now,
                    )
                    _project_trade_signal_best_effort(
                        session_factory,
                        trade_signal_id=operation.trade_signal_id,
                        reconciled_at=now,
                    )
                    result.conflicts += 1
                    continue
                _confirm_protection(
                    session_factory,
                    operation=operation,
                    evidence=evidence,
                    snapshot_evidence_id=snapshot_record.id,
                    snapshot=snapshot,
                    proof=proof,
                    reconciled_at=now,
                )
            result.confirmed += 1
        except (DeepcoinOperationConflict, TradeSignalTransitionError):
            result.unchanged += 1
        except (TypeError, ValueError, RecursionError):
            result.unchanged += 1
    _replay_parent_conflicts(
        session_factory,
        reconciled_at=now,
    )
    _replay_protection_aggregates(
        session_factory,
        snapshot=snapshot,
        proof=proof,
        reconciled_at=now,
    )
    _replay_compatibility_projections(
        session_factory,
        reconciled_at=now,
    )
    return result


def _replay_protection_aggregates(
    session_factory: sessionmaker,
    *,
    snapshot: Any,
    proof: _SnapshotProof,
    reconciled_at: datetime,
) -> None:
    if not proof.complete:
        return
    with session_factory() as session:
        parent = aliased(DeepcoinExecutionOperation)
        parent_ids = [
            int(parent_id)
            for (parent_id,) in (
                session.query(DeepcoinExecutionOperation.parent_operation_id)
                .join(
                    parent,
                    parent.id
                    == DeepcoinExecutionOperation.parent_operation_id,
                )
                .filter(
                    DeepcoinExecutionOperation.contract_version == "1",
                    DeepcoinExecutionOperation.parent_operation_id.is_not(None),
                    DeepcoinExecutionOperation.phase == "protection_readback",
                    DeepcoinExecutionOperation.state == "protected",
                    parent.contract_version == "1",
                    parent.state.in_({"protection_prepared", "recovery_required"}),
                )
                .distinct()
                .order_by(
                    parent.updated_at,
                    DeepcoinExecutionOperation.parent_operation_id,
                )
                .limit(_MAX_OPERATIONS_PER_CYCLE)
                .all()
            )
        ]
    for parent_id in parent_ids:
        try:
            _confirm_parent_protection_aggregate(
                session_factory,
                parent_operation_id=parent_id,
                snapshot=snapshot,
                proof=proof,
                reconciled_at=reconciled_at,
            )
        except (
            DeepcoinOperationConflict,
            TypeError,
            ValueError,
            RecursionError,
        ):
            pass
        finally:
            _touch_replay_candidate(
                session_factory,
                operation_id=parent_id,
                touched_at=reconciled_at,
            )


def _replay_parent_conflicts(
    session_factory: sessionmaker,
    *,
    reconciled_at: datetime,
) -> None:
    with session_factory() as session:
        parent = aliased(DeepcoinExecutionOperation)
        rows = (
            session.query(
                DeepcoinExecutionOperation.id,
                DeepcoinExecutionOperation.parent_operation_id,
            )
            .join(
                parent,
                parent.id == DeepcoinExecutionOperation.parent_operation_id,
            )
            .filter(
                DeepcoinExecutionOperation.contract_version == "1",
                DeepcoinExecutionOperation.parent_operation_id.is_not(None),
                DeepcoinExecutionOperation.state == "recovery_required",
                parent.contract_version == "1",
                parent.state != "recovery_required",
            )
            .order_by(
                DeepcoinExecutionOperation.updated_at,
                DeepcoinExecutionOperation.id,
            )
            .limit(_MAX_OPERATIONS_PER_CYCLE)
            .all()
        )
        candidates = []
        for child_id, parent_id in rows:
            snapshot = (
                session.query(DeepcoinSnapshotEvidence)
                .filter_by(deepcoin_execution_operation_id=int(child_id))
                .order_by(
                    DeepcoinSnapshotEvidence.ordinal.desc(),
                    DeepcoinSnapshotEvidence.id.desc(),
                )
                .first()
            )
            if snapshot is not None:
                candidates.append((int(parent_id), int(snapshot.id)))
    for parent_id, snapshot_evidence_id in candidates:
        _freeze_parent_conflict(
            session_factory,
            parent_operation_id=parent_id,
            snapshot_evidence_id=snapshot_evidence_id,
            reconciled_at=reconciled_at,
        )


def _record_shared_snapshot(
    session_factory: sessionmaker,
    *,
    operation: ExecutionOperationRecord,
    proof: _SnapshotProof,
):
    durable_evidence = {
        "collection_kinds": list(_COLLECTION_NAMES),
        "source": "shared_account_reconciliation",
        "uid_scope_hash": proof.uid_scope_hash,
    }
    durable_evidence_json = json.dumps(
        durable_evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    prior_snapshots = load_operation_bundle(
        session_factory,
        operation_id=operation.id,
    ).snapshots
    if prior_snapshots:
        latest = prior_snapshots[-1]
        if (
            latest.snapshot_kind == "account_composite"
            and latest.available == (proof.uid_scope_hash is not None)
            and latest.schema_valid == (proof.fingerprint is not None)
            and latest.complete == proof.complete
            and latest.row_count == proof.row_count
            and latest.page_count == (1 if proof.fingerprint is not None else 0)
            and latest.collection_fingerprint == proof.fingerprint
            and latest.start_write_generation == proof.start_generation
            and latest.end_write_generation == proof.end_generation
            and _normalized_datetime(latest.capture_started_at)
            == proof.capture_started_at
            and _normalized_datetime(latest.capture_ended_at)
            == proof.capture_ended_at
            and latest.evidence_json == durable_evidence_json
            and latest.error_code == proof.error_code
        ):
            return latest
    return record_snapshot_evidence(
        session_factory,
        operation_id=operation.id,
        expected_operation_key=operation.operation_key,
        expected_request_fingerprint=operation.request_fingerprint,
        expected_economics_fingerprint=operation.economics_fingerprint,
        expected_uid_scope_hash=(
            proof.uid_scope_hash if proof.complete else None
        ),
        expected_account_write_generation=(
            proof.end_generation if proof.complete else None
        ),
        snapshot_kind="account_composite",
        available=proof.uid_scope_hash is not None,
        schema_valid=proof.fingerprint is not None,
        complete=proof.complete,
        row_count=proof.row_count,
        page_count=1 if proof.fingerprint is not None else 0,
        collection_fingerprint=proof.fingerprint,
        start_write_generation=proof.start_generation,
        end_write_generation=proof.end_generation,
        capture_started_at=proof.capture_started_at,
        capture_ended_at=proof.capture_ended_at,
        evidence=durable_evidence,
        error_category=None if proof.complete else "snapshot_incomplete",
        error_code=proof.error_code,
    )


def _snapshot_proof(snapshot: Any, *, captured_at: datetime) -> _SnapshotProof:
    authority = getattr(snapshot, "account_authority", None)
    started_at = _optional_snapshot_datetime(
        getattr(snapshot, "capture_started_at", None),
        fallback=captured_at,
    )
    ended_at = _optional_snapshot_datetime(
        getattr(snapshot, "capture_ended_at", None),
        fallback=captured_at,
    )
    errors = getattr(snapshot, "errors", None)
    structure_valid = isinstance(errors, Mapping)
    rows_by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for name in _COLLECTION_NAMES:
        rows = getattr(snapshot, name, None)
        if not isinstance(rows, list) or not all(
            isinstance(row, Mapping) for row in rows
        ):
            structure_valid = False
            rows_by_kind[name] = []
        else:
            rows_by_kind[name] = rows
    try:
        fingerprint = (
            _composite_fingerprint(rows_by_kind) if structure_valid else None
        )
    except (TypeError, ValueError, RecursionError):
        fingerprint = None
        structure_valid = False
    authority_collection_valid = False
    if fingerprint is not None and isinstance(authority, AccountSnapshotEvidence):
        expected_collection = build_exchange_collection_evidence(
            endpoint="account_composite",
            response={
                "data": [
                    {
                        "collection_fingerprint": fingerprint,
                        "collection_kinds": sorted(_COLLECTION_NAMES),
                    }
                ]
            },
        )
        collections = authority.collections
        authority_collection_valid = bool(
            isinstance(collections, tuple)
            and len(collections) == 1
            and collections[0].endpoint == "account_composite"
            and collections[0].available
            and collections[0].schema_valid
            and collections[0].complete
            and collections[0].reason_code is None
            and collections[0].row_count == 1
            and collections[0].page_count == 1
            and collections[0].fingerprint == expected_collection.fingerprint
        )
    authority_valid = (
        isinstance(authority, AccountSnapshotEvidence)
        and authority_collection_valid
        and _is_fingerprint(authority.uid_scope_hash)
        and type(authority.start_write_generation) is int
        and authority.start_write_generation >= 0
        and type(authority.end_write_generation) is int
        and authority.end_write_generation >= 0
        and type(authority.complete) is bool
    )
    start_generation = (
        int(authority.start_write_generation) if authority_valid else 0
    )
    end_generation = (
        int(authority.end_write_generation) if authority_valid else 0
    )
    uid_scope_hash = str(authority.uid_scope_hash) if authority_valid else None
    complete = bool(
        authority_valid
        and authority.complete
        and structure_valid
        and not errors
        and start_generation == end_generation
        and end_generation > 0
        and end_generation % 2 == 0
        and fingerprint is not None
        and ended_at >= started_at
        and ended_at <= datetime.now(UTC) + timedelta(minutes=5)
    )
    return _SnapshotProof(
        complete=complete,
        fingerprint=fingerprint,
        row_count=sum(len(rows) for rows in rows_by_kind.values()),
        start_generation=start_generation,
        end_generation=end_generation,
        uid_scope_hash=uid_scope_hash,
        capture_started_at=started_at,
        capture_ended_at=max(started_at, ended_at),
        error_code=(
            None if complete else "protected_entry_snapshot_incomplete"
        ),
    )


def _match_entry_operation(
    session_factory: sessionmaker,
    *,
    operation: ExecutionOperationRecord,
    evidence: Mapping[str, Any],
    snapshot: Any,
) -> _EntryMatch:
    if not _readback_operation_identity_valid(operation):
        return _EntryMatch("conflict")
    try:
        authority = _entry_authority(
            session_factory,
            operation=operation,
            evidence=evidence,
        )
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return _EntryMatch("conflict")
    bundle = load_operation_bundle(session_factory, operation_id=operation.id)
    if not _writer_attempt_authoritative(
        operation,
        attempts=bundle.attempts,
        normalized_path="/deepcoin/trade/order",
        phase="entry_submit",
        uid_scope_hash=authority.uid_scope_hash,
    ):
        return _EntryMatch("conflict")
    if not _snapshot_after_sent_writer(snapshot, attempts=bundle.attempts):
        return _EntryMatch("conflict")

    matched_rows: list[tuple[str, Mapping[str, Any]]] = []
    for collection_name in ("open_orders", "order_history", "trade_fills"):
        for row in getattr(snapshot, collection_name, []):
            client_order_id = _first_text(
                row, "clOrdId", "clientOrderId", "client_order_id"
            )
            if client_order_id and _sha256(client_order_id) == authority.client_order_ref:
                matched_rows.append((collection_name, row))
    if not matched_rows:
        return _EntryMatch("pending")

    identities: set[tuple[str, str]] = set()
    observed_order_ids: set[str] = set()
    terminal_identities: set[tuple[str, str]] = set()
    fill_totals: dict[tuple[str, str], Decimal] = {}
    incomplete = False
    for collection_name, row in matched_rows:
        disposition, identity, observed_order_id = _entry_row_disposition(
            collection_name,
            row,
            authority=authority,
        )
        if observed_order_id:
            observed_order_ids.add(observed_order_id)
        if disposition == "conflict":
            return _EntryMatch("conflict")
        if disposition == "pending":
            incomplete = True
        elif identity is not None:
            identities.add(identity)
            if collection_name == "trade_fills":
                quantity = _first_text(
                    row, "fillSz", "accFillSz", "sz", "size", "quantity"
                )
                try:
                    fill_totals[identity] = fill_totals.get(
                        identity, Decimal("0")
                    ) + Decimal(quantity)
                except (InvalidOperation, TypeError, ValueError):
                    return _EntryMatch("conflict")
            else:
                terminal_identities.add(identity)
    if len(observed_order_ids) > 1 or len(identities) > 1:
        return _EntryMatch("conflict")
    if incomplete or not identities:
        return _EntryMatch("pending")
    order_id, pos_id = next(iter(identities))
    identity = (order_id, pos_id)
    if identity not in terminal_identities and not _decimal_equal(
        fill_totals.get(identity), authority.quantity
    ):
        return _EntryMatch("pending")
    matching_positions = [
        row
        for row in getattr(snapshot, "positions", [])
        if _first_text(row, "posId", "pos_id") == pos_id
    ]
    if len(matching_positions) != 1:
        return _EntryMatch("conflict" if matching_positions else "pending")
    position = matching_positions[0]
    position_instrument = _first_text(position, "instId", "instrument_id")
    position_side = _first_text(position, "posSide", "position_side")
    position_size = _first_text(position, "pos", "size", "sz")
    if not all((position_instrument, position_side, position_size)):
        return _EntryMatch("pending")
    if (
        position_instrument.upper() != authority.instrument_id
        or position_side.lower() != authority.position_side
        or not _decimal_equal(position_size, authority.quantity)
        or _sha256(f"position:{pos_id}")
        in authority.pre_submit_position_refs
    ):
        return _EntryMatch("conflict")
    return _EntryMatch("confirmed", order_id=order_id, pos_id=pos_id)


def _entry_authority(
    session_factory: sessionmaker,
    *,
    operation: ExecutionOperationRecord,
    evidence: Mapping[str, Any],
) -> _EntryAuthority:
    with session_factory() as session:
        signal = session.get(TradeSignal, operation.trade_signal_id)
        if signal is None:
            raise ValueError("signal_missing")
        payload = _bounded_object(signal.payload_json, max_bytes=262_144)
        signal_venue = str(signal.venue)
        signal_action = str(signal.action)
        signal_symbol = str(signal.symbol or "").upper()
        signal_side = str(signal.side or "").lower()
    if (
        signal_venue != "deepcoin"
        or signal_action != "open_position"
        or signal_side not in {"long", "short"}
        or not signal_symbol
    ):
        raise ValueError("signal_identity_invalid")
    draft = payload.get("deepcoin_order_draft")
    if not isinstance(draft, dict):
        raise ValueError("draft_missing")
    order_legs = draft.get("order_legs")
    if not isinstance(order_legs, list) or not order_legs:
        raise ValueError("draft_legs_invalid")
    if isinstance(draft.get("authorized_leg_indices"), list):
        selected_indices = list(draft["authorized_leg_indices"])
    elif isinstance(draft.get("selected_entry_leg_indices"), list):
        selected_indices = list(draft["selected_entry_leg_indices"])
    else:
        selected_indices = list(range(1, len(order_legs) + 1))
    if (
        not selected_indices
        or any(
            type(index) is not int or not 1 <= index <= len(order_legs)
            for index in selected_indices
        )
        or len(set(selected_indices)) != len(selected_indices)
    ):
        raise ValueError("draft_selection_invalid")
    selected_legs = [order_legs[index - 1] for index in selected_indices]
    from telegram_kol_research.recovery_live_submit import (
        _submission_order_legs,
        _submission_source_leg_indices,
        build_deepcoin_market_order_payload,
    )

    submission_legs = _submission_order_legs(draft, selected_legs)
    source_indices = _submission_source_leg_indices(
        selected_indices=selected_indices,
        submission_order_legs=submission_legs,
    )
    offset = draft.get("_entry_leg_index_offset", 0)
    if type(offset) is not int:
        raise ValueError("draft_offset_invalid")
    actual_indices = [offset + int(index) for index in source_indices]
    leg_index = evidence.get("leg_index")
    expected_indices = evidence.get("expected_entry_leg_indices")
    if (
        type(leg_index) is not int
        or not isinstance(expected_indices, list)
        or not expected_indices
        or any(type(index) is not int for index in expected_indices)
        or expected_indices != actual_indices[: len(expected_indices)]
        or actual_indices.count(leg_index) != 1
        or leg_index not in expected_indices
    ):
        raise ValueError("entry_leg_identity_invalid")
    leg = submission_legs[actual_indices.index(leg_index)]
    if (
        not isinstance(leg, dict)
        or str(leg.get("order_type") or "").lower() != "market"
    ):
        raise ValueError("entry_leg_type_invalid")
    request = build_deepcoin_market_order_payload(draft, leg)
    request_fingerprint = _canonical_payload_fingerprint(request)
    client_order_id = str(request.get("clOrdId") or "")
    economics_fingerprint = _canonical_payload_fingerprint(
        {
            "client_order_id": client_order_id,
            "instrument_id": request.get("instId"),
            "leg_index": leg_index,
            "position_side": request.get("posSide"),
            "quantity": request.get("sz"),
            "side": request.get("side"),
        }
    )
    uid_scope_hash = evidence.get("uid_scope_hash")
    pre_submit_refs = evidence.get("pre_submit_position_refs")
    expected_key = (
        f"protected-entry:v1:signal:{operation.trade_signal_id}:"
        f"leg:{leg_index}:entry"
    )
    if (
        operation.parent_operation_id is not None
        or operation.operation_key != expected_key
        or operation.writer_attempted_at is None
        or operation.request_fingerprint != request_fingerprint
        or operation.economics_fingerprint != economics_fingerprint
        or evidence.get("client_order_ref") != _sha256(client_order_id)
        or not _safe_exchange_identity(client_order_id)
        or not _is_fingerprint(uid_scope_hash)
        or not isinstance(pre_submit_refs, list)
        or pre_submit_refs != sorted(set(pre_submit_refs))
        or any(not _is_fingerprint(value) for value in pre_submit_refs)
        or str(request.get("instId") or "").upper()
        != f"{signal_symbol}-USDT-SWAP"
        or str(request.get("posSide") or "").lower() != signal_side
    ):
        raise ValueError("entry_request_identity_invalid")
    return _EntryAuthority(
        client_order_id=client_order_id,
        client_order_ref=_sha256(client_order_id),
        instrument_id=str(request["instId"]).upper(),
        leg_index=leg_index,
        position_side=str(request["posSide"]).lower(),
        quantity=str(request["sz"]),
        side=str(request["side"]).lower(),
        request_fingerprint=request_fingerprint,
        economics_fingerprint=economics_fingerprint,
        uid_scope_hash=str(uid_scope_hash),
        pre_submit_position_refs=frozenset(pre_submit_refs),
    )


def _writer_attempt_authoritative(
    operation: ExecutionOperationRecord,
    *,
    attempts: Sequence[Any],
    normalized_path: str,
    phase: str,
    uid_scope_hash: str,
) -> bool:
    if operation.attempt_count != len(attempts):
        return False
    post_attempts = [attempt for attempt in attempts if attempt.method == "POST"]
    sent_attempts = [
        attempt
        for attempt in post_attempts
        if attempt.outcome_certainty != "not_sent"
    ]
    if len(sent_attempts) != 1 or not post_attempts:
        return False
    sent_attempt = sent_attempts[0]
    expected_certainty = {
        "entry_pending_readback": "accepted",
        "entry_unknown": "unknown",
        "protection_pending_readback": "accepted",
        "protection_unknown": "unknown",
    }.get(operation.state)
    if (
        expected_certainty is not None
        and sent_attempt.outcome_certainty != expected_certainty
    ):
        return False
    writer_at = _normalized_datetime(operation.writer_attempted_at)
    for expected_ordinal, attempt in enumerate(attempts, start=1):
        started = _normalized_datetime(attempt.started_at)
        completed = _normalized_datetime(attempt.completed_at)
        if attempt.ordinal != expected_ordinal or completed < started:
            return False
        if attempt.method != "POST":
            continue
        if (
            attempt.normalized_path != normalized_path
            or attempt.phase != phase
            or attempt.request_fingerprint != operation.request_fingerprint
            or attempt.uid_scope_hash != uid_scope_hash
            or started < writer_at
        ):
            return False
        if attempt.outcome_certainty == "accepted":
            if (
                attempt.error_category is not None
                or type(attempt.http_status) is not int
                or not 200 <= attempt.http_status < 300
                or str(attempt.business_code or "") != "0"
            ):
                return False
        elif attempt.outcome_certainty == "unknown":
            if attempt.error_category is None:
                return False
        elif attempt.outcome_certainty != "not_sent":
            return False
    return sent_attempt.outcome_certainty in {"accepted", "unknown"}


def _snapshot_after_sent_writer(
    snapshot: Any,
    *,
    attempts: Sequence[Any],
) -> bool:
    try:
        capture_started_at = _normalized_datetime(snapshot.capture_started_at)
        sent_completed_at = max(
            _normalized_datetime(attempt.completed_at)
            for attempt in attempts
            if attempt.method == "POST"
            and attempt.outcome_certainty != "not_sent"
        )
    except (AttributeError, TypeError, ValueError):
        return False
    return capture_started_at >= sent_completed_at


def _readback_operation_identity_valid(
    operation: ExecutionOperationRecord,
) -> bool:
    expected = {
        "entry_pending_readback": ("entry_readback", "accepted"),
        "entry_unknown": ("entry_readback", "unknown"),
        "protection_pending_readback": (
            "protection_readback",
            "accepted",
        ),
        "protection_unknown": ("protection_readback", "unknown"),
    }.get(operation.state)
    if operation.state == "protected":
        return bool(
            operation.phase == "protection_readback"
            and operation.outcome_certainty == "confirmed"
            and operation.writer_attempted_at is not None
            and operation.completed_at is not None
        )
    return bool(
        expected is not None
        and (operation.phase, operation.outcome_certainty) == expected
        and operation.writer_attempted_at is not None
        and operation.completed_at is None
    )


def _parent_protection_aggregate_identity_valid(
    operation: ExecutionOperationRecord,
) -> bool:
    if operation.writer_attempted_at is None or operation.completed_at is not None:
        return False
    if operation.state == "protection_prepared":
        return bool(
            operation.phase == "protection_submit"
            and operation.outcome_certainty == "confirmed"
            and operation.reason_code == "protection_intents_prepared"
        )
    if operation.state == "recovery_required":
        return bool(
            operation.phase in {"protection_readback", "reconciliation"}
            and operation.outcome_certainty == "unknown"
            and operation.reason_code == "protection_incomplete"
        )
    return False


def _entry_row_disposition(
    collection_name: str,
    row: Mapping[str, Any],
    *,
    authority: _EntryAuthority,
) -> tuple[str, tuple[str, str] | None, str | None]:
    client_order_id = _first_text(
        row, "clOrdId", "clientOrderId", "client_order_id"
    )
    order_id = _first_text(row, "ordId", "orderId", "order_id", "id")
    pos_id = _first_text(row, "posId", "pos_id")
    instrument = _first_text(row, "instId", "instrument_id")
    position_side = _first_text(row, "posSide", "position_side")
    side = _first_text(row, "side", "order_side")
    quantity = _first_text(
        row,
        "fillSz" if collection_name == "trade_fills" else "sz",
        "accFillSz",
        "sz",
        "size",
        "quantity",
    )
    status = _first_text(row, "state", "status").lower()
    if (
        not _safe_exchange_identity(client_order_id)
        or (order_id and not _safe_exchange_identity(order_id))
        or (pos_id and not _safe_exchange_identity(pos_id))
        or (instrument and instrument.upper() != authority.instrument_id)
        or (position_side and position_side.lower() != authority.position_side)
        or (side and side.lower() != authority.side)
        or (
            quantity
            and collection_name != "trade_fills"
            and not _decimal_equal(quantity, authority.quantity)
        )
        or (
            collection_name == "trade_fills"
            and quantity
            and (
                not _positive_decimal(quantity)
                or _decimal_greater(quantity, authority.quantity)
            )
        )
        or status in {"cancelled", "canceled", "rejected", "failed"}
    ):
        return "conflict", None, order_id or None
    if collection_name == "open_orders" or status in {
        "live",
        "open",
        "pending",
        "partially_filled",
        "partial",
    }:
        return "pending", None, order_id or None
    terminal_filled = status in {"filled", "completed", "done"}
    fill_evidence = collection_name == "trade_fills" and bool(quantity)
    if not (terminal_filled or fill_evidence):
        return "pending", None, order_id or None
    if not all(
        (
            order_id,
            pos_id,
            instrument,
            position_side,
            side,
            quantity,
        )
    ):
        return "pending", None, order_id or None
    return "confirmed", (order_id, pos_id), order_id


def _protection_disposition(
    session_factory: sessionmaker,
    *,
    operation: ExecutionOperationRecord,
    evidence: Mapping[str, Any],
    snapshot: Any,
    parent_record: ExecutionOperationRecord | None = None,
    parent_evidence: Mapping[str, Any] | None = None,
) -> str:
    if not _readback_operation_identity_valid(operation):
        return "conflict"
    intent_id = evidence.get("position_mutation_intent_id")
    protection_index = evidence.get("protection_index")
    child_required_count = evidence.get("required_protection_count")
    if (
        operation.parent_operation_id is None
        or type(intent_id) is not int
        or type(protection_index) is not int
        or protection_index < 0
        or type(child_required_count) is not int
        or child_required_count <= 0
        or protection_index >= child_required_count
        or operation.writer_attempted_at is None
    ):
        return "conflict"
    if parent_record is None or parent_evidence is None:
        try:
            parent_record, parent_evidence = _parent_authority(
                session_factory,
                parent_operation_id=int(operation.parent_operation_id),
            )
        except (TypeError, ValueError, RecursionError, UnicodeError):
            return "conflict"
    try:
        parent_entry_authority = _entry_authority(
            session_factory,
            operation=parent_record,
            evidence=parent_evidence,
        )
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return "conflict"
    if not _parent_protection_aggregate_identity_valid(parent_record):
        return "conflict"
    if parent_evidence.get("required_protection_count") != child_required_count:
        return "conflict"
    parent_bundle = load_operation_bundle(
        session_factory,
        operation_id=parent_record.id,
    )
    if not _writer_attempt_authoritative(
        parent_record,
        attempts=parent_bundle.attempts,
        normalized_path="/deepcoin/trade/order",
        phase="entry_submit",
        uid_scope_hash=parent_entry_authority.uid_scope_hash,
    ):
        return "conflict"
    if not _snapshot_after_sent_writer(
        snapshot,
        attempts=parent_bundle.attempts,
    ):
        return "conflict"
    with session_factory() as session:
        parent = session.get(DeepcoinExecutionOperation, parent_record.id)
        if parent is None:
            return "conflict"
        leg_index = parent_evidence.get("leg_index")
        expected_operation_key = (
            f"protected-entry:v1:signal:{operation.trade_signal_id}:"
            f"leg:{leg_index}:protection:{protection_index}"
        )
        intent = session.get(PositionMutationIntent, intent_id)
        binding = (
            session.get(ExecutionBinding, operation.execution_binding_id)
            if type(operation.execution_binding_id) is int
            else None
        )
        leg = (
            session.get(ExecutionOrderLeg, operation.execution_order_leg_id)
            if type(operation.execution_order_leg_id) is int
            else None
        )
        signal = session.get(TradeSignal, operation.trade_signal_id)
        signal_strategy_instance_id = (
            signal.strategy_instance_id if signal is not None else None
        )
        if (
            intent is None
            or signal is None
            or parent.contract_version != "1"
            or parent.operation_key != parent_entry_authority_operation_key(
                operation.trade_signal_id,
                leg_index,
            )
            or parent.request_fingerprint
            != parent_entry_authority.request_fingerprint
            or parent.economics_fingerprint
            != parent_entry_authority.economics_fingerprint
            or parent.trade_signal_id != operation.trade_signal_id
            or type(leg_index) is not int
            or operation.operation_key != expected_operation_key
            or intent.idempotency_key
            != (
                f"protected-entry:{operation.trade_signal_id}:"
                f"{leg_index}:set:{protection_index}"
            )
            or binding is None
            or leg is None
            or binding.venue != "deepcoin"
            or binding.chat_id != signal.chat_id
            or binding.message_id != signal.message_id
            or binding.symbol.upper()
            != parent_entry_authority.instrument_id.removesuffix("-USDT-SWAP")
            or binding.side.lower() != parent_entry_authority.position_side
            or binding.strategy_instance_id != signal_strategy_instance_id
            or leg.venue != "deepcoin"
            or leg.purpose != "entry"
            or leg.order_kind != "market"
            or leg.leg_index != leg_index
            or not _safe_exchange_identity(str(leg.order_id or ""))
            or leg.client_order_id != parent_entry_authority.client_order_id
            or leg.pos_id != intent.pos_id
            or not str(intent.idempotency_key).startswith("protected-entry:")
            or intent.venue != "deepcoin"
            or intent.operation != "set_position_sltp"
            or intent.status != "confirmed"
            or intent.request_fingerprint != operation.request_fingerprint
            or intent.execution_binding_id != operation.execution_binding_id
            or intent.execution_order_leg_id != operation.execution_order_leg_id
            or leg.execution_binding_id != binding.id
            or leg.strategy_instance_id != intent.strategy_instance_id
            or binding.strategy_instance_id != intent.strategy_instance_id
        ):
            return "conflict"
        try:
            request = load_validated_set_position_request(
                intent.request_json,
                request_fingerprint=str(intent.request_fingerprint),
                authority_fingerprint=str(intent.authority_fingerprint),
                require_baseline=True,
            )
        except PositionMutationIntentError:
            return "conflict"
        order_id = str(intent.order_id or "")
        if not _safe_exchange_identity(order_id) or not _safe_exchange_identity(
            str(intent.pos_id or "")
        ):
            return "conflict"
        ledgers = (
            session.query(PositionProtectionLedger)
            .filter(
                PositionProtectionLedger.venue == "deepcoin",
                PositionProtectionLedger.order_id == order_id,
            )
            .all()
        )
        if len(ledgers) != 1:
            return "conflict"
        ledger = ledgers[0]
        purpose = str(request.get("_ledger_purpose") or "")
        exchange_purpose = (
            "stop_loss" if purpose in {"stop_loss", "backup_stop"} else "take_profit"
        )
        trigger_field = (
            "slTriggerPx" if exchange_purpose == "stop_loss" else "tpTriggerPx"
        )
        protection_economics = _canonical_payload_fingerprint(
            {
                "pos_id": str(intent.pos_id or ""),
                "purpose": purpose,
                "size": (
                    str(request.get("sz")) if request.get("sz") is not None else None
                ),
                "trigger_price": str(request.get(trigger_field) or ""),
            }
        )
        try:
            response = _bounded_object(intent.response_json, max_bytes=4096)
        except (TypeError, ValueError, RecursionError, UnicodeError):
            return "conflict"
        response_order_id = _response_order_id(response)
        baseline_refs = set(request.get("_pre_submit_order_refs") or [])
        if (
            operation.economics_fingerprint != protection_economics
            or operation.phase != "protection_readback"
            or intent.confirmed_at is None
            or intent.submitted_at is None
            or intent.reserved_at is None
            or intent.submitted_at < intent.reserved_at
            or intent.confirmed_at < intent.submitted_at
            or response_order_id != order_id
            or _sha256(f"protection_order:{order_id}") in baseline_refs
            or ledger.status != "verified"
            or ledger.execution_binding_id != intent.execution_binding_id
            or ledger.execution_order_leg_id != intent.execution_order_leg_id
            or ledger.strategy_instance_id != intent.strategy_instance_id
            or ledger.pos_id != intent.pos_id
            or ledger.purpose != purpose
            or ledger.instrument_id != str(request.get("instId") or "")
            or ledger.side != str(request.get("posSide") or "")
            or not _decimal_equal(ledger.trigger_price, request.get(trigger_field))
            or not _optional_decimal_equal(ledger.size_text, request.get("sz"))
        ):
            return "conflict"
        expected = {
            "instrument_id": str(ledger.instrument_id),
            "order_id": order_id,
            "pos_id": str(ledger.pos_id),
            "side": str(ledger.side),
            "trigger": str(ledger.trigger_price or ""),
            "size": None if ledger.size_text is None else str(ledger.size_text),
            "purpose": exchange_purpose,
        }

    child_bundle = load_operation_bundle(
        session_factory,
        operation_id=operation.id,
    )
    if not _writer_attempt_authoritative(
        operation,
        attempts=child_bundle.attempts,
        normalized_path="/deepcoin/trade/set-position-sltp",
        phase="protection_submit",
        uid_scope_hash=parent_entry_authority.uid_scope_hash,
    ):
        return "conflict"
    if not _snapshot_after_sent_writer(
        snapshot,
        attempts=child_bundle.attempts,
    ):
        return "conflict"

    rows = [
        row
        for row in getattr(snapshot, "pending_trigger_orders", [])
        if _first_text(row, "ordId", "orderId", "order_id", "id")
        == expected["order_id"]
    ]
    if not rows:
        return "pending"
    if len(rows) != 1:
        return "conflict"
    row = rows[0]
    trigger_keys = (
        ("slTriggerPx", "slTriggerPrice", "triggerPrice")
        if expected["purpose"] == "stop_loss"
        else ("tpTriggerPx", "tpTriggerPrice", "triggerPrice")
    )
    row_instrument = _first_text(row, "instId", "instrument_id")
    row_pos_id = _first_text(row, "posId", "pos_id")
    row_side = _first_text(row, "posSide", "position_side")
    row_trigger = _first_text(row, *trigger_keys)
    row_size = _first_text(row, "sz", "size")
    if (
        (row_instrument and row_instrument.upper() != expected["instrument_id"].upper())
        or (row_pos_id and row_pos_id != expected["pos_id"])
        or (row_side and row_side.lower() != expected["side"].lower())
        or (row_trigger and not _decimal_equal(row_trigger, expected["trigger"]))
        or (row_size and not _optional_decimal_equal(row_size, expected["size"]))
    ):
        return "conflict"
    if not all((row_instrument, row_pos_id, row_side, row_trigger)):
        return "pending"
    if expected["size"] is not None and not row_size:
        return "pending"
    positions = [
        position
        for position in getattr(snapshot, "positions", [])
        if _first_text(position, "posId", "pos_id") == expected["pos_id"]
    ]
    if not positions:
        return "pending"
    if len(positions) != 1:
        return "conflict"
    position = positions[0]
    position_instrument = _first_text(position, "instId", "instrument_id")
    position_side = _first_text(position, "posSide", "position_side")
    position_size = _first_text(position, "pos", "size", "sz")
    if not all((position_instrument, position_side, position_size)):
        return "pending"
    if (
        position_instrument.upper() != expected["instrument_id"].upper()
        or position_side.lower() != expected["side"].lower()
        or not _positive_decimal(position_size)
        or (
            expected["size"] is not None
            and not _decimal_equal(position_size, expected["size"])
        )
    ):
        return "conflict"
    return "confirmed"


def _confirm_entry(
    session_factory: sessionmaker,
    *,
    operation: ExecutionOperationRecord,
    evidence: Mapping[str, Any],
    snapshot_evidence_id: int,
    order_id: str,
    pos_id: str,
    proof: _SnapshotProof,
    reconciled_at: datetime,
) -> None:
    transition_execution_operation(
        session_factory,
        operation_id=operation.id,
        expected_operation_key=operation.operation_key,
        expected_request_fingerprint=operation.request_fingerprint,
        expected_economics_fingerprint=operation.economics_fingerprint,
        expected_uid_scope_hash=proof.uid_scope_hash,
        expected_account_write_generation=proof.end_generation,
        expected_state=operation.state,
        expected_state_version=operation.state_version,
        phase="entry_readback",
        state="entry_confirmed",
        outcome_certainty="confirmed",
        reason_code="entry_readback_confirmed",
        evidence={
            **evidence,
            "order_ref": _sha256(f"order:{order_id}"),
            "position_ref": _sha256(f"position:{pos_id}"),
            "reconciliation_snapshot_id": snapshot_evidence_id,
        },
        writer_attempted_at=operation.writer_attempted_at,
        updated_at=reconciled_at,
    )
    try:
        _project_trade_signal(
            session_factory,
            trade_signal_id=operation.trade_signal_id,
            reconciled_at=reconciled_at,
        )
    except TradeSignalTransitionError:
        pass


def _confirm_protection(
    session_factory: sessionmaker,
    *,
    operation: ExecutionOperationRecord,
    evidence: Mapping[str, Any],
    snapshot_evidence_id: int,
    snapshot: Any,
    proof: _SnapshotProof,
    reconciled_at: datetime,
) -> None:
    transition_execution_operation(
        session_factory,
        operation_id=operation.id,
        expected_operation_key=operation.operation_key,
        expected_request_fingerprint=operation.request_fingerprint,
        expected_economics_fingerprint=operation.economics_fingerprint,
        expected_uid_scope_hash=proof.uid_scope_hash,
        expected_account_write_generation=proof.end_generation,
        expected_state=operation.state,
        expected_state_version=operation.state_version,
        phase="protection_readback",
        state="protected",
        outcome_certainty="confirmed",
        reason_code="protection_fully_confirmed",
        evidence={
            **evidence,
            "reconciliation_snapshot_id": snapshot_evidence_id,
        },
        writer_attempted_at=operation.writer_attempted_at,
        completed_at=reconciled_at,
        updated_at=reconciled_at,
    )
    try:
        _confirm_parent_protection_aggregate(
            session_factory,
            parent_operation_id=int(operation.parent_operation_id),
            snapshot=snapshot,
            proof=proof,
            reconciled_at=reconciled_at,
        )
    except DeepcoinOperationConflict:
        pass
    try:
        _project_trade_signal(
            session_factory,
            trade_signal_id=operation.trade_signal_id,
            reconciled_at=reconciled_at,
        )
    except TradeSignalTransitionError:
        pass


def _confirm_parent_protection_aggregate(
    session_factory: sessionmaker,
    *,
    parent_operation_id: int,
    snapshot: Any,
    proof: _SnapshotProof,
    reconciled_at: datetime,
) -> None:
    with session_factory() as session:
        parent = session.get(DeepcoinExecutionOperation, parent_operation_id)
        if parent is None or parent.contract_version != "1":
            raise DeepcoinOperationConflict("operation_identity_conflict")
        parent_record = load_operation_bundle(
            session_factory,
            operation_id=parent_operation_id,
        ).operation
        parent_evidence = _strict_object(parent.evidence_json)
        expected_uid_scope_hash = parent_evidence.get("uid_scope_hash")
        if (
            not _is_fingerprint(expected_uid_scope_hash)
            or proof.uid_scope_hash != expected_uid_scope_hash
        ):
            raise DeepcoinOperationConflict("snapshot_scope_conflict")
        required = parent_evidence.get("required_protection_count")
        children = (
            session.query(DeepcoinExecutionOperation)
            .filter(
                DeepcoinExecutionOperation.parent_operation_id == parent.id,
                DeepcoinExecutionOperation.phase == "protection_readback",
            )
            .order_by(DeepcoinExecutionOperation.id)
            .all()
        )
        indices = []
        for child in children:
            child_evidence = _strict_object(child.evidence_json)
            indices.append(child_evidence.get("protection_index"))
        complete = (
            type(required) is int
            and required > 0
            and len(children) == required
            and sorted(indices) == list(range(required))
            and all(
                child.state == "protected"
                and child.outcome_certainty == "confirmed"
                and child.completed_at is not None
                for child in children
            )
        )
        child_ids = [int(child.id) for child in children]
    if complete:
        for child_id in child_ids:
            child_record = load_operation_bundle(
                session_factory,
                operation_id=child_id,
            ).operation
            try:
                child_evidence = _strict_object(child_record.evidence_json)
            except (TypeError, ValueError, RecursionError, UnicodeError):
                complete = False
                break
            if (
                _protection_disposition(
                    session_factory,
                    operation=child_record,
                    evidence=child_evidence,
                    snapshot=snapshot,
                )
                != "confirmed"
            ):
                complete = False
                break
    if not complete or parent_record.state == "protected":
        return
    if parent_record.state not in {"protection_prepared", "recovery_required"}:
        raise DeepcoinOperationConflict("operation_state_conflict")
    transition_execution_operation(
        session_factory,
        operation_id=parent_record.id,
        expected_operation_key=parent_record.operation_key,
        expected_request_fingerprint=parent_record.request_fingerprint,
        expected_economics_fingerprint=parent_record.economics_fingerprint,
        expected_uid_scope_hash=proof.uid_scope_hash,
        expected_account_write_generation=proof.end_generation,
        expected_state=parent_record.state,
        expected_state_version=parent_record.state_version,
        phase="protection_readback",
        state="protected",
        outcome_certainty="confirmed",
        reason_code="protection_fully_confirmed",
        evidence={
            **parent_evidence,
            "confirmed_protection_count": int(required),
            "required_protection_count": int(required),
        },
        writer_attempted_at=parent_record.writer_attempted_at,
        updated_at=reconciled_at,
    )


def _freeze_conflict(
    session_factory: sessionmaker,
    *,
    operation: ExecutionOperationRecord,
    evidence: Mapping[str, Any],
    snapshot_evidence_id: int,
    reason_code: str,
    reconciled_at: datetime,
) -> None:
    transition_execution_operation(
        session_factory,
        operation_id=operation.id,
        expected_operation_key=operation.operation_key,
        expected_request_fingerprint=operation.request_fingerprint,
        expected_economics_fingerprint=operation.economics_fingerprint,
        expected_state=operation.state,
        expected_state_version=operation.state_version,
        phase="reconciliation",
        state="recovery_required",
        outcome_certainty="unknown",
        error_category="state_conflict",
        reason_code=reason_code,
        evidence={
            **evidence,
            "next_action": "supervision_only",
            "reconciliation_snapshot_id": snapshot_evidence_id,
        },
        writer_attempted_at=operation.writer_attempted_at,
        updated_at=reconciled_at,
    )


def _project_trade_signal(
    session_factory: sessionmaker,
    *,
    trade_signal_id: int,
    reconciled_at: datetime,
) -> None:
    with session_factory() as session:
        signal = session.get(TradeSignal, trade_signal_id)
        status = str(signal.status) if signal is not None else ""
    if status not in {
        "pending",
        "processing",
        "recovery_required",
        "active_protection_pending",
        "active_protected_deferred",
    }:
        return
    finalize_trade_signal_from_execution_operation(
        session_factory,
        signal_id=trade_signal_id,
        finalized_at=reconciled_at,
        expected_status=status,
        safe_error_code="protected_entry_reconciliation_pending",
    )


def _project_trade_signal_best_effort(
    session_factory: sessionmaker,
    *,
    trade_signal_id: int,
    reconciled_at: datetime,
) -> None:
    try:
        _project_trade_signal(
            session_factory,
            trade_signal_id=trade_signal_id,
            reconciled_at=reconciled_at,
        )
    except TradeSignalTransitionError:
        pass


def _parent_authority(
    session_factory: sessionmaker,
    *,
    parent_operation_id: int,
) -> tuple[ExecutionOperationRecord, dict[str, Any]]:
    with session_factory() as session:
        parent = session.get(DeepcoinExecutionOperation, parent_operation_id)
        if parent is None or parent.contract_version != "1":
            raise ValueError("parent_identity_invalid")
        evidence = _strict_object(parent.evidence_json)
    parent_record = load_operation_bundle(
        session_factory,
        operation_id=parent_operation_id,
    ).operation
    return parent_record, evidence


def _freeze_parent_conflict(
    session_factory: sessionmaker,
    *,
    parent_operation_id: int | None,
    snapshot_evidence_id: int,
    reconciled_at: datetime,
) -> None:
    if type(parent_operation_id) is not int:
        return
    try:
        parent = load_operation_bundle(
            session_factory,
            operation_id=parent_operation_id,
        ).operation
    except DeepcoinOperationConflict:
        return
    try:
        evidence = _strict_object(parent.evidence_json)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        evidence = {"reconciliation_error": "parent_evidence_invalid"}
    try:
        _freeze_conflict(
            session_factory,
            operation=parent,
            evidence=evidence,
            snapshot_evidence_id=snapshot_evidence_id,
            reason_code="protected_entry_reconciliation_identity_conflict",
            reconciled_at=reconciled_at,
        )
    except DeepcoinOperationConflict:
        return


def _replay_compatibility_projections(
    session_factory: sessionmaker,
    *,
    reconciled_at: datetime,
) -> None:
    with session_factory() as session:
        root_counts = (
            session.query(
                DeepcoinExecutionOperation.trade_signal_id.label("signal_id"),
                func.count(DeepcoinExecutionOperation.id).label("root_count"),
            )
            .filter(
                DeepcoinExecutionOperation.contract_version == "1",
                DeepcoinExecutionOperation.parent_operation_id.is_(None),
            )
            .group_by(DeepcoinExecutionOperation.trade_signal_id)
            .subquery()
        )
        terminal_statuses = {
            "submitted",
            "submission_failed_no_exposure",
            "failed",
            "rejected",
        }
        rows = (
            session.query(
                DeepcoinExecutionOperation.trade_signal_id,
                DeepcoinExecutionOperation.state,
                TradeSignal.status,
            )
            .join(
                TradeSignal,
                TradeSignal.id == DeepcoinExecutionOperation.trade_signal_id,
            )
            .join(
                root_counts,
                root_counts.c.signal_id
                == DeepcoinExecutionOperation.trade_signal_id,
            )
            .filter(
                DeepcoinExecutionOperation.contract_version == "1",
                DeepcoinExecutionOperation.parent_operation_id.is_(None),
                root_counts.c.root_count == 1,
                DeepcoinExecutionOperation.state.in_(
                    {
                        "entry_confirmed",
                        "protection_prepared",
                        "recovery_required",
                        "protected",
                    }
                ),
                TradeSignal.status.notin_(terminal_statuses),
                or_(
                    and_(
                        DeepcoinExecutionOperation.state.in_(
                            {"entry_confirmed", "protection_prepared"}
                        ),
                        TradeSignal.status != "active_protection_pending",
                    ),
                    and_(
                        DeepcoinExecutionOperation.state == "recovery_required",
                        TradeSignal.status != "recovery_required",
                    ),
                    and_(
                        DeepcoinExecutionOperation.state == "protected",
                        TradeSignal.status.notin_(
                            {"submitted", "active_protected_deferred"}
                        ),
                    ),
                ),
            )
            .order_by(
                func.coalesce(TradeSignal.attempts, 0),
                TradeSignal.updated_at,
                DeepcoinExecutionOperation.id,
            )
            .limit(_MAX_OPERATIONS_PER_CYCLE)
            .all()
        )
    for signal_id, operation_state, signal_status in rows:
        if (
            operation_state == "entry_confirmed"
            and signal_status == "active_protection_pending"
        ) or (
            operation_state == "recovery_required"
            and signal_status == "recovery_required"
        ) or (
            operation_state == "protected"
            and signal_status in {"submitted", "active_protected_deferred"}
        ):
            continue
        try:
            _project_trade_signal(
                session_factory,
                trade_signal_id=int(signal_id),
                reconciled_at=reconciled_at,
            )
        except TradeSignalTransitionError:
            continue


def _touch_replay_candidate(
    session_factory: sessionmaker,
    *,
    operation_id: int,
    touched_at: datetime,
) -> None:
    with session_factory() as session:
        if session.get_bind().dialect.name == "sqlite":
            from sqlalchemy import text

            session.execute(text("BEGIN IMMEDIATE"))
        row = session.get(DeepcoinExecutionOperation, int(operation_id))
        if row is None or row.state not in {
            "protection_prepared",
            "recovery_required",
        }:
            session.rollback()
            return
        row.updated_at = max(
            _normalized_datetime(touched_at),
            datetime.now(UTC),
        ).replace(tzinfo=None)
        session.commit()


def parent_entry_authority_operation_key(
    trade_signal_id: int,
    leg_index: object,
) -> str:
    if type(leg_index) is not int or leg_index <= 0:
        raise ValueError("leg_index_invalid")
    return (
        f"protected-entry:v1:signal:{int(trade_signal_id)}:"
        f"leg:{leg_index}:entry"
    )


def _response_order_id(response: Mapping[str, Any]) -> str | None:
    data = response.get("data")
    candidates: list[Any] = [data, response]
    if isinstance(data, list):
        candidates = [*data, response]
    identities: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        value = _first_text(
            candidate,
            "ordId",
            "orderId",
            "order_id",
            "orderSysID",
        )
        if value:
            if not _safe_exchange_identity(value):
                return None
            identities.add(value)
    return next(iter(identities)) if len(identities) == 1 else None


def _strict_object(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4096:
        raise ValueError("operation_evidence_invalid")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    value = json.loads(
        raw,
        object_pairs_hook=unique_object,
        parse_constant=lambda constant: (_ for _ in ()).throw(
            ValueError(constant)
        ),
    )
    if (
        not isinstance(value, dict)
        or json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        != raw
    ):
        raise ValueError("operation_evidence_invalid")
    if not _safe_evidence_value(value):
        raise ValueError("operation_evidence_invalid")
    return value


def _bounded_object(raw: object, *, max_bytes: int) -> dict[str, Any]:
    if (
        not isinstance(raw, str)
        or len(raw.encode("utf-8")) > max_bytes
    ):
        raise ValueError("bounded_object_invalid")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    value = json.loads(
        raw,
        object_pairs_hook=unique_object,
        parse_constant=lambda constant: (_ for _ in ()).throw(
            ValueError(constant)
        ),
    )
    if not isinstance(value, dict):
        raise ValueError("bounded_object_invalid")
    _bounded_row_fingerprint(value)
    return value


def _canonical_payload_fingerprint(value: Mapping[str, Any]) -> str:
    return _sha256(
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _safe_evidence_value(value: object) -> bool:
    node_count = 0

    def validate(item: object, depth: int) -> bool:
        nonlocal node_count
        node_count += 1
        if depth > 8 or node_count > 256:
            return False
        if item is None or isinstance(item, bool) or isinstance(item, int):
            return True
        if isinstance(item, float):
            return math.isfinite(item)
        if isinstance(item, str):
            return (
                len(item.encode("utf-8")) <= 4096
                and not contains_credential_marker(item)
            )
        if isinstance(item, Mapping):
            return all(
                isinstance(key, str)
                and 0 < len(key.encode("utf-8")) <= 128
                and not contains_credential_marker(key)
                and validate(child, depth + 1)
                for key, child in item.items()
            )
        if isinstance(item, list):
            return all(validate(child, depth + 1) for child in item)
        return False

    try:
        return validate(value, 0)
    except (RecursionError, UnicodeError):
        return False


def _composite_fingerprint(
    rows_by_kind: Mapping[str, list[Mapping[str, Any]]],
) -> str:
    collections = {}
    for kind, rows in rows_by_kind.items():
        row_hashes = sorted(_bounded_row_fingerprint(row) for row in rows)
        collections[str(kind)] = row_hashes
    return _sha256(
        json.dumps(
            collections,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _bounded_row_fingerprint(row: Mapping[str, Any]) -> str:
    node_count = 0

    def normalize(value: Any, depth: int) -> Any:
        nonlocal node_count
        node_count += 1
        if depth > _MAX_ROW_DEPTH or node_count > _MAX_ROW_NODES:
            raise ValueError("snapshot_row_complexity_exceeded")
        if value is None or isinstance(value, bool) or isinstance(value, int):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("snapshot_row_number_invalid")
            return value
        if isinstance(value, str):
            if len(value) > _MAX_ROW_TEXT:
                raise ValueError("snapshot_row_text_invalid")
            return value
        if isinstance(value, Mapping):
            return {
                str(key): normalize(child, depth + 1)
                for key, child in value.items()
                if isinstance(key, str) and 0 < len(key) <= 256
            }
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray, memoryview)
        ):
            return [normalize(child, depth + 1) for child in value]
        raise ValueError("snapshot_row_type_invalid")

    normalized = normalize(row, 0)
    if not isinstance(normalized, dict) or len(normalized) != len(row):
        raise ValueError("snapshot_row_key_invalid")
    return _sha256(
        json.dumps(
            normalized,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _first_text(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _safe_exchange_identity(value: object) -> bool:
    if not isinstance(value, str) or _SAFE_EXCHANGE_ID.fullmatch(value) is None:
        return False
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return not any(
        marker in normalized
        for marker in (
            "authorization",
            "bearer",
            "dcaccesskey",
            "dcaccesspassphrase",
            "dcaccesssign",
            "privatekey",
            "secret",
            "token",
        )
    )


def _positive_decimal(value: object) -> bool:
    try:
        return Decimal(str(value)) > 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def _decimal_equal(left: object, right: object) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _decimal_greater(left: object, right: object) -> bool:
    try:
        return Decimal(str(left)) > Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _optional_decimal_equal(left: object, right: object) -> bool:
    if left in (None, "") and right in (None, ""):
        return True
    return _decimal_equal(left, right)


def _is_fingerprint(value: object) -> bool:
    return isinstance(value, str) and _HEX_64.fullmatch(value) is not None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("reconciled_at_invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_snapshot_datetime(value: object, *, fallback: datetime) -> datetime:
    if not isinstance(value, datetime):
        return fallback
    return _normalized_datetime(value)

"""Durable trade-signal queue between strategy recognition and execution."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_execution_operations import (
    contains_credential_marker,
)
from telegram_kol_research.execution_bindings import build_strategy_instance_id
from telegram_kol_research.models import DeepcoinExecutionOperation
from telegram_kol_research.models import DeepcoinAccountWriteGeneration
from telegram_kol_research.models import DeepcoinRequestAttempt
from telegram_kol_research.models import DeepcoinSnapshotEvidence
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import ExecutionOrderLeg
from telegram_kol_research.models import PositionMutationIntent
from telegram_kol_research.models import StrategyLifecycle
from telegram_kol_research.models import TradeIdea
from telegram_kol_research.models import TradeSignal
from telegram_kol_research.models import TriggerProtectionIntent
from telegram_kol_research.protected_entry_projection import (
    SUBMISSION_FAILED_NO_EXPOSURE,
)
from telegram_kol_research.protected_entry_projection import SUBMITTED
from telegram_kol_research.protected_entry_projection import project_protected_entry_operation
from telegram_kol_research.protected_entry_projection import projection_uid_scope_hash


MANAGEMENT_TRADE_SIGNAL_ACTIONS = frozenset(
    {
        "close_position",
        "exit_position",
        "temporary_exit",
        "temporary_close",
        "partial_close_and_move_stop_to_entry",
        "adjust_position_tpsl",
        "adjust_stop_loss",
        "adjust_take_profit",
    }
)
MANUAL_MANAGEMENT_SOURCE_TYPES = frozenset(
    {"manual", "manual_operator", "operator_manual"}
)


@dataclass(slots=True)
class TradeSignalRecord:
    id: int
    signal_uid: str
    strategy_instance_id: str | None
    source_type: str
    venue: str
    kol_id: str
    chat_id: int
    message_id: int
    symbol: str
    side: str
    action: str
    status: str
    payload: Any
    attempts: int
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class TradeSignalExecutionEvidence:
    id: int
    status: str
    strategy_instance_id: str | None
    chat_id: int
    message_id: int
    symbol: str
    side: str
    draft: dict[str, Any]
    result: dict[str, Any] | None
    last_error: str | None


class TradeSignalFingerprintSyncError(RuntimeError):
    """The pending signal could not be synchronized without ambiguity."""


class TradeSignalReuseError(RuntimeError):
    """An immutable queued signal conflicts with the requested identity."""


class TradeSignalClaimError(RuntimeError):
    """A durable signal could not be claimed exactly once."""


class TradeSignalTransitionError(RuntimeError):
    """A claimed signal changed state before its terminal transition."""


_SAFE_EXECUTION_PROJECTION_CODE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}")


def _normalized_assembly_fingerprint(value: Any, *, error_code: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise TradeSignalFingerprintSyncError(error_code)
    return value.lower()


def _is_positive_assembly_id(value: Any) -> bool:
    return type(value) is int and value > 0


def synchronize_pending_entry_assembly_evidence(
    session_factory: sessionmaker,
    *,
    signal_id: int,
    strategy_instance_id: str,
    expected_payload: Mapping[str, Any],
    expected_fingerprint: str,
    finalized_evidence: Mapping[str, Any],
    synchronized_at: datetime,
) -> TradeSignalRecord:
    """Atomically replace both pending-signal copies of assembly evidence."""

    try:
        expected_payload_json = json.dumps(
            expected_payload,
            ensure_ascii=False,
            sort_keys=True,
        )
        updated_payload = json.loads(expected_payload_json)
    except (TypeError, ValueError):
        raise TradeSignalFingerprintSyncError(
            "entry_assembly_signal_payload_invalid"
        ) from None

    draft = updated_payload.get("deepcoin_order_draft")
    if not isinstance(draft, dict):
        raise TradeSignalFingerprintSyncError("entry_assembly_signal_draft_invalid")

    normalized_expected_fingerprint = _normalized_assembly_fingerprint(
        expected_fingerprint,
        error_code="entry_assembly_signal_fingerprint_mismatch",
    )
    top_evidence = updated_payload.get("entry_preamble_assembly")
    nested_evidence = draft.get("entry_preamble_assembly")

    try:
        final_evidence = json.loads(
            json.dumps(finalized_evidence, ensure_ascii=False, sort_keys=True)
        )
    except (TypeError, ValueError):
        raise TradeSignalFingerprintSyncError(
            "entry_assembly_signal_final_evidence_invalid"
        ) from None
    if not isinstance(final_evidence, dict):
        raise TradeSignalFingerprintSyncError(
            "entry_assembly_signal_final_evidence_invalid"
        )
    final_fingerprint = _normalized_assembly_fingerprint(
        final_evidence.get("assembly_fingerprint"),
        error_code="entry_assembly_signal_final_evidence_invalid",
    )
    if (
        not _is_positive_assembly_id(final_evidence.get("assembly_id"))
        or not isinstance(final_evidence.get("strategy_instance_id"), str)
        or final_evidence.get("strategy_instance_id") != strategy_instance_id
    ):
        raise TradeSignalFingerprintSyncError(
            "entry_assembly_signal_identity_mismatch"
        )
    final_evidence["assembly_fingerprint"] = final_fingerprint
    assembly_id = final_evidence["assembly_id"]

    evidence_copies = [
        evidence
        for evidence in (top_evidence, nested_evidence)
        if evidence is not None
    ]
    if any(not isinstance(evidence, dict) for evidence in evidence_copies):
        raise TradeSignalFingerprintSyncError(
            "entry_assembly_signal_evidence_invalid"
        )
    if any(
        _normalized_assembly_fingerprint(
            evidence.get("assembly_fingerprint"),
            error_code="entry_assembly_signal_fingerprint_mismatch",
        )
        != normalized_expected_fingerprint
        for evidence in evidence_copies
    ):
        raise TradeSignalFingerprintSyncError(
            "entry_assembly_signal_fingerprint_mismatch"
        )
    if any(
        not _is_positive_assembly_id(evidence.get("assembly_id"))
        or evidence.get("assembly_id") != assembly_id
        or not isinstance(evidence.get("strategy_instance_id"), str)
        or evidence.get("strategy_instance_id") != strategy_instance_id
        for evidence in evidence_copies
    ):
        raise TradeSignalFingerprintSyncError(
            "entry_assembly_signal_identity_mismatch"
        )

    updated_payload["entry_preamble_assembly"] = final_evidence
    draft["entry_preamble_assembly"] = json.loads(
        json.dumps(final_evidence, ensure_ascii=False, sort_keys=True)
    )
    updated_payload_json = json.dumps(
        updated_payload,
        ensure_ascii=False,
        sort_keys=True,
    )

    with session_factory() as session:
        result = session.execute(
            update(TradeSignal)
            .where(
                TradeSignal.id == int(signal_id),
                TradeSignal.status == "pending",
                TradeSignal.strategy_instance_id == strategy_instance_id,
                TradeSignal.payload_json == expected_payload_json,
            )
            .values(
                payload_json=updated_payload_json,
                updated_at=synchronized_at,
            )
        )
        if int(result.rowcount or 0) != 1:
            session.rollback()
            raise TradeSignalFingerprintSyncError(
                "entry_assembly_signal_cas_failed"
            )
        session.commit()

    updated = load_trade_signal(session_factory, int(signal_id))
    try:
        reloaded_payload_json = json.dumps(
            updated.payload,
            ensure_ascii=False,
            sort_keys=True,
        )
    except (TypeError, ValueError):
        reloaded_payload_json = ""
    top_final = (
        updated.payload.get("entry_preamble_assembly")
        if isinstance(updated.payload, Mapping)
        else None
    )
    draft_final = (
        updated.payload.get("deepcoin_order_draft")
        if isinstance(updated.payload, Mapping)
        else None
    )
    nested_final = (
        draft_final.get("entry_preamble_assembly")
        if isinstance(draft_final, Mapping)
        else None
    )
    if (
        updated.status != "pending"
        or updated.strategy_instance_id != strategy_instance_id
        or reloaded_payload_json != updated_payload_json
        or not isinstance(top_final, Mapping)
        or not isinstance(nested_final, Mapping)
        or not _is_positive_assembly_id(top_final.get("assembly_id"))
        or top_final.get("assembly_id") != assembly_id
        or not _is_positive_assembly_id(nested_final.get("assembly_id"))
        or nested_final.get("assembly_id") != assembly_id
        or not isinstance(top_final.get("strategy_instance_id"), str)
        or top_final.get("strategy_instance_id") != strategy_instance_id
        or not isinstance(nested_final.get("strategy_instance_id"), str)
        or nested_final.get("strategy_instance_id") != strategy_instance_id
        or top_final.get("assembly_fingerprint") != final_fingerprint
        or nested_final.get("assembly_fingerprint") != final_fingerprint
    ):
        raise TradeSignalFingerprintSyncError(
            "entry_assembly_signal_reload_validation_failed"
        )
    return updated


def build_trade_signal_uid(
    *,
    venue: str,
    source_type: str,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    action: str,
) -> str:
    return (
        f"{venue.lower()}:{source_type.lower()}:{int(chat_id)}:{int(message_id)}:"
        f"{symbol.upper()}:{side.lower()}:{action.lower()}"
    )


def enqueue_trade_signal(
    session_factory: sessionmaker,
    *,
    venue: str,
    source_type: str,
    kol_id: str,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    action: str,
    payload: dict[str, Any],
    strategy_instance_id: str | None = None,
    enqueued_at: datetime | None = None,
) -> TradeSignalRecord:
    """Create or refresh one durable trade signal."""

    normalized_venue = venue.lower()
    normalized_source_type = source_type.lower()
    normalized_symbol = symbol.upper()
    normalized_side = side.lower()
    normalized_action = action.lower()
    signal_uid = build_trade_signal_uid(
        venue=normalized_venue,
        source_type=normalized_source_type,
        chat_id=chat_id,
        message_id=message_id,
        symbol=normalized_symbol,
        side=normalized_side,
        action=normalized_action,
    )
    resolved_strategy_instance_id = strategy_instance_id or build_strategy_instance_id(
        venue=normalized_venue,
        chat_id=chat_id,
        message_id=message_id,
        symbol=normalized_symbol,
        side=normalized_side,
    )
    now = enqueued_at or datetime.now(UTC)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    with session_factory() as session:
        row = (
            session.query(TradeSignal)
            .filter(TradeSignal.signal_uid == signal_uid)
            .one_or_none()
        )
        if row is None:
            row = TradeSignal(
                signal_uid=signal_uid,
                venue=normalized_venue,
                source_type=normalized_source_type,
                kol_id=kol_id,
                chat_id=chat_id,
                message_id=message_id,
                symbol=normalized_symbol,
                side=normalized_side,
                action=normalized_action,
                payload_json=payload_json,
            )
            session.add(row)
            session.flush()

        row.strategy_instance_id = resolved_strategy_instance_id
        row.kol_id = kol_id
        row.payload_json = payload_json
        if row.status in {"failed", "rejected"}:
            row.status = "pending"
            row.last_error = None
        row.updated_at = now
        session.commit()
        return _row_to_record(row)


def load_or_create_trade_signal(
    session_factory: sessionmaker,
    *,
    venue: str,
    source_type: str,
    kol_id: str,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    action: str,
    payload: dict[str, Any],
    strategy_instance_id: str | None = None,
    enqueued_at: datetime | None = None,
) -> TradeSignalRecord:
    """Create once, then reuse the durable signal without rewriting history."""

    normalized_venue = venue.lower()
    normalized_source_type = source_type.lower()
    normalized_symbol = symbol.upper()
    normalized_side = side.lower()
    normalized_action = action.lower()
    signal_uid = build_trade_signal_uid(
        venue=normalized_venue,
        source_type=normalized_source_type,
        chat_id=chat_id,
        message_id=message_id,
        symbol=normalized_symbol,
        side=normalized_side,
        action=normalized_action,
    )
    resolved_strategy_instance_id = strategy_instance_id or build_strategy_instance_id(
        venue=normalized_venue,
        chat_id=chat_id,
        message_id=message_id,
        symbol=normalized_symbol,
        side=normalized_side,
    )
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    now = enqueued_at or datetime.now(UTC)

    def validate_existing(row: TradeSignal) -> TradeSignalRecord:
        if row.strategy_instance_id != resolved_strategy_instance_id:
            raise TradeSignalReuseError("trade_signal_reuse_identity_mismatch")
        return _row_to_record(row)

    with session_factory() as session:
        existing = (
            session.query(TradeSignal)
            .filter(TradeSignal.signal_uid == signal_uid)
            .one_or_none()
        )
        if existing is not None:
            return validate_existing(existing)
        row = TradeSignal(
            signal_uid=signal_uid,
            strategy_instance_id=resolved_strategy_instance_id,
            venue=normalized_venue,
            source_type=normalized_source_type,
            kol_id=kol_id,
            chat_id=chat_id,
            message_id=message_id,
            symbol=normalized_symbol,
            side=normalized_side,
            action=normalized_action,
            payload_json=payload_json,
            updated_at=now,
        )
        session.add(row)
        try:
            session.commit()
            return _row_to_record(row)
        except IntegrityError:
            session.rollback()
            existing = (
                session.query(TradeSignal)
                .filter(TradeSignal.signal_uid == signal_uid)
                .one_or_none()
            )
            if existing is None:
                raise
            return validate_existing(existing)


def load_trade_signal(
    session_factory: sessionmaker,
    signal_id: int,
) -> TradeSignalRecord:
    with session_factory() as session:
        row = session.get(TradeSignal, signal_id)
        if row is None:
            raise LookupError("trade signal not found")
        return _row_to_record(row)


def load_trade_signal_execution_evidence(
    session_factory: sessionmaker,
    *,
    signal_id: int,
) -> TradeSignalExecutionEvidence:
    """Load the exact durable entry signal and its terminal result evidence."""

    with session_factory() as session:
        row = session.get(TradeSignal, int(signal_id))
        if row is None:
            raise LookupError("trade signal not found")
        payload = _row_payload(row)
        draft = payload.get("deepcoin_order_draft")
        if not isinstance(draft, dict):
            draft = {}
        try:
            result = json.loads(row.result_json) if row.result_json else None
        except (json.JSONDecodeError, TypeError):
            result = None
        if not isinstance(result, dict):
            result = None
        return TradeSignalExecutionEvidence(
            id=int(row.id),
            status=str(row.status),
            strategy_instance_id=row.strategy_instance_id,
            chat_id=int(row.chat_id),
            message_id=int(row.message_id),
            symbol=str(row.symbol),
            side=str(row.side),
            draft=dict(draft),
            result=result,
            last_error=row.last_error,
        )


def list_pending_trade_signals(
    session_factory: sessionmaker,
    *,
    venue: str = "deepcoin",
    limit: int = 50,
) -> list[TradeSignalRecord]:
    with session_factory() as session:
        rows = (
            session.query(TradeSignal)
            .filter(TradeSignal.venue == venue.lower())
            .filter(TradeSignal.status == "pending")
            .order_by(TradeSignal.created_at.asc(), TradeSignal.id.asc())
            .limit(limit)
            .all()
        )
        return [_row_to_record(row) for row in rows]


def claim_pending_trade_signal(
    session_factory: sessionmaker,
    *,
    signal_id: int,
    claimed_at: datetime | None = None,
) -> TradeSignalRecord:
    """Atomically claim one pending signal without automatic crash recovery."""

    now = claimed_at or datetime.now(UTC)
    with session_factory() as session:
        result = session.execute(
            update(TradeSignal)
            .where(
                TradeSignal.id == int(signal_id),
                TradeSignal.status == "pending",
            )
            .values(status="processing", updated_at=now)
        )
        if int(result.rowcount or 0) != 1:
            session.rollback()
            row = session.get(TradeSignal, int(signal_id))
            status = str(row.status) if row is not None else "missing"
            raise TradeSignalClaimError(
                f"trade_signal_claim_failed:{status}"
            )
        session.commit()
    return load_trade_signal(session_factory, int(signal_id))


def audit_pending_legacy_management_signals(
    session_factory: sessionmaker,
    *,
    venue: str = "deepcoin",
    limit: int = 100,
) -> dict[str, Any]:
    """List redacted pending management rows which have no exact batch ID.

    This is deliberately read-only.  It does not claim, refresh, convert, or
    execute any signal, and it never returns the arbitrary stored payload.
    ``scan_truncated`` makes the fixed scan cap explicit when ``total`` is only
    the count within the bounded scan.
    """

    bounded_limit = max(1, min(int(limit), 500))
    scan_limit = 5_000
    with session_factory() as session:
        rows = (
            session.query(TradeSignal)
            .filter(TradeSignal.venue == venue.lower())
            .filter(TradeSignal.status == "pending")
            .filter(TradeSignal.action.in_(sorted(MANAGEMENT_TRADE_SIGNAL_ACTIONS)))
            .order_by(TradeSignal.created_at.asc(), TradeSignal.id.asc())
            .limit(scan_limit + 1)
            .all()
        )
        scan_truncated = len(rows) > scan_limit
        rows = rows[:scan_limit]
        legacy_rows = [
            row
            for row in rows
            if canonical_management_batch_id(_row_payload(row)) is None
        ]
        selected = legacy_rows[:bounded_limit]
        by_action: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for row in legacy_rows:
            by_action[row.action] = by_action.get(row.action, 0) + 1
            by_status[row.status] = by_status.get(row.status, 0) + 1
        return {
            "total": len(legacy_rows),
            "returned": len(selected),
            "truncated": scan_truncated or len(selected) < len(legacy_rows),
            "scan_truncated": scan_truncated,
            "by_action": dict(sorted(by_action.items())),
            "by_status": dict(sorted(by_status.items())),
            "items": [
                {
                    "id": row.id,
                    "action": row.action,
                    "status": row.status,
                    "source_type": row.source_type,
                    "chat_id": row.chat_id,
                    "message_id": row.message_id,
                }
                for row in selected
            ],
        }


def _row_payload(row: TradeSignal) -> dict[str, Any]:
    try:
        payload = json.loads(row.payload_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def canonical_management_batch_id(payload: Any) -> int | None:
    """Parse only canonical positive IDs shared by audit and dispatch."""

    if not isinstance(payload, Mapping):
        return None
    batch_id = payload.get("management_batch_id")
    if isinstance(batch_id, bool):
        return None
    if isinstance(batch_id, int):
        return batch_id if batch_id > 0 else None
    if isinstance(batch_id, str) and batch_id.isdigit() and not batch_id.startswith("0"):
        normalized = int(batch_id)
        return normalized if normalized > 0 else None
    return None


def finalize_trade_signal_from_execution_operation(
    session_factory: sessionmaker,
    *,
    signal_id: int,
    finalized_at: datetime | None = None,
    expected_status: str = "processing",
    safe_error_code: str | None = None,
    result: Mapping[str, Any] | None = None,
) -> str:
    """Project one locked protected-entry aggregate into its compatibility row.

    The execution operation remains authoritative.  This function never derives
    control state from ``safe_error_code`` or another display string.
    """

    now = finalized_at or datetime.now(UTC)
    if safe_error_code is not None and (
        not isinstance(safe_error_code, str)
        or _SAFE_EXECUTION_PROJECTION_CODE.fullmatch(safe_error_code) is None
        or contains_credential_marker(safe_error_code)
    ):
        safe_error_code = "protected_entry_execution_failed"
    result_json = _safe_projection_result_json(result)

    with session_factory() as session:
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        row = session.get(TradeSignal, int(signal_id))
        if (
            row is None
            or row.status != expected_status
            or row.action != "open_position"
        ):
            session.rollback()
            raise TradeSignalTransitionError(
                "trade_signal_projection_transition_failed"
            )
        parents = (
            session.query(DeepcoinExecutionOperation)
            .filter(
                DeepcoinExecutionOperation.trade_signal_id == int(signal_id),
                DeepcoinExecutionOperation.parent_operation_id.is_(None),
            )
            .order_by(DeepcoinExecutionOperation.id)
            .all()
        )
        if len(parents) != 1 or parents[0].contract_version != "1":
            session.rollback()
            raise TradeSignalTransitionError(
                "trade_signal_projection_parent_conflict"
            )
        parent = parents[0]
        children = (
            session.query(DeepcoinExecutionOperation)
            .filter(
                DeepcoinExecutionOperation.parent_operation_id
                == int(parent.id)
            )
            .order_by(DeepcoinExecutionOperation.id)
            .all()
        )
        operation_ids = [int(parent.id), *(int(child.id) for child in children)]
        attempts = (
            session.query(DeepcoinRequestAttempt)
            .filter(
                DeepcoinRequestAttempt.deepcoin_execution_operation_id.in_(
                    operation_ids
                )
            )
            .order_by(
                DeepcoinRequestAttempt.deepcoin_execution_operation_id,
                DeepcoinRequestAttempt.ordinal,
                DeepcoinRequestAttempt.id,
            )
            .all()
        )
        current_write_generation = None
        parent_uid_scope_hash = projection_uid_scope_hash(parent)
        if isinstance(parent_uid_scope_hash, str):
            generation = (
                session.query(DeepcoinAccountWriteGeneration)
                .filter(
                    DeepcoinAccountWriteGeneration.uid_scope_hash
                    == parent_uid_scope_hash
                )
                .one_or_none()
            )
            if generation is not None:
                current_write_generation = int(generation.generation)
        snapshots = (
            session.query(DeepcoinSnapshotEvidence)
            .filter(
                DeepcoinSnapshotEvidence.deepcoin_execution_operation_id.in_(
                    operation_ids
                )
            )
            .order_by(
                DeepcoinSnapshotEvidence.deepcoin_execution_operation_id,
                DeepcoinSnapshotEvidence.ordinal,
                DeepcoinSnapshotEvidence.id,
            )
            .all()
        )
        projection = project_protected_entry_operation(
            parent=parent,
            children=children,
            attempts=attempts,
            snapshots=snapshots,
            current_write_generation=current_write_generation,
            verified_child_operation_ids=_locked_verified_child_operation_ids(
                session,
                trade_signal=row,
                parent=parent,
                children=children,
            ),
        )
        row.status = projection
        row.last_error = None if projection == SUBMITTED else safe_error_code
        row.result_json = result_json if projection == SUBMITTED else None
        row.attempts = int(row.attempts or 0) + (
            0 if projection == SUBMITTED else 1
        )
        row.processed_at = now if projection in {
            SUBMITTED,
            SUBMISSION_FAILED_NO_EXPOSURE,
        } else None
        row.updated_at = now
        if (
            projection == SUBMISSION_FAILED_NO_EXPOSURE
            and row.source_type != "strategy_revision"
        ):
            _mark_lifecycle_auto_trade_failed(session, row, now)
        session.commit()
        return projection


def _locked_verified_child_operation_ids(
    session,
    *,
    trade_signal: TradeSignal,
    parent: DeepcoinExecutionOperation,
    children: list[DeepcoinExecutionOperation],
) -> frozenset[int]:
    if not trade_signal.strategy_instance_id:
        return frozenset()
    verified: set[int] = set()
    for child in children:
        binding_id = child.execution_binding_id
        leg_id = child.execution_order_leg_id
        if type(binding_id) is not int or type(leg_id) is not int:
            continue
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.get(ExecutionOrderLeg, leg_id)
        evidence = _strict_projection_object(child.evidence_json)
        if (
            binding is None
            or leg is None
            or evidence is None
            or binding.strategy_instance_id != trade_signal.strategy_instance_id
            or leg.execution_binding_id != binding_id
            or leg.strategy_instance_id != trade_signal.strategy_instance_id
            or leg.venue != "deepcoin"
            or leg.purpose != "entry"
        ):
            continue
        if child.phase == "protection_readback":
            intent_id = evidence.get("position_mutation_intent_id")
            intent = (
                session.get(PositionMutationIntent, intent_id)
                if type(intent_id) is int
                else None
            )
            if (
                intent is None
                or intent.venue != "deepcoin"
                or intent.operation != "set_position_sltp"
                or intent.strategy_instance_id
                != trade_signal.strategy_instance_id
                or intent.execution_binding_id != binding_id
                or intent.execution_order_leg_id != leg_id
                or intent.request_fingerprint != child.request_fingerprint
                or intent.status != "confirmed"
            ):
                continue
        else:
            child_leg_index = evidence.get("leg_index")
            intent = (
                session.query(TriggerProtectionIntent)
                .filter(
                    TriggerProtectionIntent.venue == "deepcoin",
                    TriggerProtectionIntent.execution_order_leg_id == leg_id,
                )
                .one_or_none()
            )
            if (
                type(child_leg_index) is not int
                or leg.leg_index != child_leg_index
                or (
                    child.state == "completed"
                    and (
                        intent is None
                        or intent.execution_binding_id != binding_id
                        or intent.request_fingerprint
                        != child.request_fingerprint
                        or not intent.parent_trigger_order_id
                    )
                )
                or (child.state == "pre_submit_deferred" and intent is not None)
            ):
                continue
        verified.add(int(child.id))
    return frozenset(verified)


def _strict_projection_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        return None

    def reject_duplicates(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = item
        return result

    try:
        parsed = json.loads(
            value,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(constant)
            ),
        )
        if (
            not isinstance(parsed, dict)
            or json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            != value
        ):
            return None
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return None
    return parsed


def _safe_projection_result_json(
    result: Mapping[str, Any] | None,
) -> str | None:
    if result is None:
        return None
    seen: set[int] = set()
    node_count = 0

    def validate(value: Any, depth: int = 0) -> bool:
        nonlocal node_count
        node_count += 1
        if depth > 12 or node_count > 1_024:
            return False
        if value is None or isinstance(value, bool):
            return True
        if isinstance(value, int):
            return not isinstance(value, bool)
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, str):
            return (
                len(value.encode("utf-8")) <= 4_096
                and not contains_credential_marker(value)
            )
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in seen:
                return False
            seen.add(identity)
            try:
                return all(
                    isinstance(key, str)
                    and len(key.encode("utf-8")) <= 128
                    and not contains_credential_marker(key)
                    and validate(child, depth + 1)
                    for key, child in value.items()
                )
            finally:
                seen.remove(identity)
        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in seen:
                return False
            seen.add(identity)
            try:
                return all(validate(child, depth + 1) for child in value)
            finally:
                seen.remove(identity)
        return False

    if validate(result):
        try:
            serialized = json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            if len(serialized.encode("utf-8")) <= 65_536:
                return serialized
        except (TypeError, ValueError, RecursionError):
            pass
    return json.dumps(
        {"result_redacted": True, "status": "submitted"},
        ensure_ascii=False,
        sort_keys=True,
    )


def freeze_trade_signal_for_protected_entry_recovery(
    session_factory: sessionmaker,
    *,
    signal_id: int,
    frozen_at: datetime | None = None,
    expected_status: str = "processing",
    safe_error_code: str | None = None,
) -> None:
    """Freeze a v1-admitted entry that failed before its parent reservation."""

    now = frozen_at or datetime.now(UTC)
    if safe_error_code is not None and (
        not isinstance(safe_error_code, str)
        or _SAFE_EXECUTION_PROJECTION_CODE.fullmatch(safe_error_code) is None
        or contains_credential_marker(safe_error_code)
    ):
        safe_error_code = "protected_entry_execution_failed"
    with session_factory() as session:
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        result = session.execute(
            update(TradeSignal)
            .where(
                TradeSignal.id == int(signal_id),
                TradeSignal.status == expected_status,
                TradeSignal.action == "open_position",
            )
            .values(
                status="recovery_required",
                last_error=safe_error_code,
                attempts=TradeSignal.attempts + 1,
                processed_at=None,
                updated_at=now,
            )
        )
        if int(result.rowcount or 0) != 1:
            session.rollback()
            raise TradeSignalTransitionError(
                "trade_signal_projection_transition_failed"
            )
        session.commit()


def mark_trade_signal_submitted(
    session_factory: sessionmaker,
    *,
    signal_id: int,
    result: dict[str, Any],
    processed_at: datetime | None = None,
    expected_status: str | None = None,
) -> None:
    now = processed_at or datetime.now(UTC)
    with session_factory() as session:
        if expected_status is not None:
            transition = session.execute(
                update(TradeSignal)
                .where(
                    TradeSignal.id == int(signal_id),
                    TradeSignal.status == expected_status,
                )
                .values(
                    status="submitted",
                    result_json=json.dumps(
                        result, ensure_ascii=False, sort_keys=True
                    ),
                    last_error=None,
                    processed_at=now,
                    updated_at=now,
                )
            )
            if int(transition.rowcount or 0) != 1:
                session.rollback()
                raise TradeSignalTransitionError(
                    "trade_signal_submit_transition_failed"
                )
            session.commit()
            return
        row = session.get(TradeSignal, signal_id)
        if row is None:
            raise LookupError("trade signal not found")
        row.status = "submitted"
        row.result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
        row.last_error = None
        row.processed_at = now
        row.updated_at = now
        session.commit()


def mark_trade_signal_failed(
    session_factory: sessionmaker,
    *,
    signal_id: int,
    error: str,
    failed_at: datetime | None = None,
    expected_status: str | None = None,
    terminal_status: str = "failed",
) -> None:
    now = failed_at or datetime.now(UTC)
    with session_factory() as session:
        if expected_status is not None:
            transition = session.execute(
                update(TradeSignal)
                .where(
                    TradeSignal.id == int(signal_id),
                    TradeSignal.status == expected_status,
                )
                .values(
                    status=terminal_status,
                    last_error=error,
                    attempts=TradeSignal.attempts + 1,
                    updated_at=now,
                )
            )
            if int(transition.rowcount or 0) != 1:
                session.rollback()
                raise TradeSignalTransitionError(
                    "trade_signal_failure_transition_failed"
                )
            session.expire_all()
        row = session.get(TradeSignal, signal_id)
        if row is None:
            raise LookupError("trade signal not found")
        if expected_status is None:
            row.status = terminal_status
            row.last_error = error
            row.attempts = int(row.attempts or 0) + 1
            row.updated_at = now
        if row.action == "open_position":
            _mark_lifecycle_auto_trade_failed(session, row, now)
        session.commit()


def _mark_lifecycle_auto_trade_failed(session, row: TradeSignal, failed_at: datetime) -> None:
    lifecycle = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == row.chat_id)
        .filter(StrategyLifecycle.message_id == row.message_id)
        .filter(StrategyLifecycle.symbol == row.symbol)
        .filter(StrategyLifecycle.side == row.side)
        .filter(StrategyLifecycle.lifecycle_status.in_(["pending_entry", "entered"]))
        .order_by(StrategyLifecycle.id.desc())
        .first()
    )
    if lifecycle is None:
        return

    lifecycle.lifecycle_status = "invalidated"
    lifecycle.exit_reason = "auto_trade_failed"
    lifecycle.exited_at = failed_at
    lifecycle.updated_at = failed_at
    if lifecycle.trade_idea_id is not None:
        trade_idea = session.get(TradeIdea, lifecycle.trade_idea_id)
        if trade_idea is not None and trade_idea.status == "open":
            trade_idea.status = "closed"
            trade_idea.closed_at = failed_at


def _row_to_record(row: TradeSignal) -> TradeSignalRecord:
    try:
        payload = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    return TradeSignalRecord(
        id=row.id,
        signal_uid=row.signal_uid,
        strategy_instance_id=row.strategy_instance_id,
        source_type=row.source_type,
        venue=row.venue,
        kol_id=row.kol_id,
        chat_id=row.chat_id,
        message_id=row.message_id,
        symbol=row.symbol,
        side=row.side,
        action=row.action,
        status=row.status,
        payload=payload,
        attempts=row.attempts,
        last_error=row.last_error,
    )

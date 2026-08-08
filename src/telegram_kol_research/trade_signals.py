"""Durable trade-signal queue between strategy recognition and execution."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.execution_bindings import build_strategy_instance_id
from telegram_kol_research.models import StrategyLifecycle
from telegram_kol_research.models import TradeIdea
from telegram_kol_research.models import TradeSignal


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


class TradeSignalFingerprintSyncError(RuntimeError):
    """The pending signal could not be synchronized without ambiguity."""


class TradeSignalReuseError(RuntimeError):
    """An immutable queued signal conflicts with the requested identity."""


class TradeSignalClaimError(RuntimeError):
    """A durable signal could not be claimed exactly once."""


class TradeSignalTransitionError(RuntimeError):
    """A claimed signal changed state before its terminal transition."""


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
                    status="failed",
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
            row.status = "failed"
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

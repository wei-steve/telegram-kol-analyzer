"""Durable, fail-closed replacement of pending strategy entry legs."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    StrategyLifecycle,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
    StrategyThread,
    utc_now,
)


PENDING_ENTRY_STATES = frozenset({"pending", "submitted", "open", "active"})
FILLED_ENTRY_STATES = frozenset({"filled", "partial_closed"})
TERMINAL_REVISION_STATES = frozenset(
    {"succeeded", "recovery_required", "failed", "blocked"}
)
REPLACEMENT_WRITE_BOUNDARY_STATES = frozenset(
    {"submitting_replacements", "reconciling"}
)
REVISION_ADVANCE_CLAIM_LEASE = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class StrategyRevisionResult:
    status: str
    batch_id: int | None = None
    reason_code: str | None = None
    remaining_fraction: float | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _entry_leg_size(leg: ExecutionOrderLeg) -> Decimal | None:
    try:
        payload = json.loads(leg.request_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    for key in ("sz", "quantity", "size"):
        if payload.get(key) in (None, ""):
            continue
        try:
            size = Decimal(str(payload[key]))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return size if size.is_finite() and size > 0 else None
    return None


def plan_strategy_revision(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    strategy_thread_id: int | None,
    replacement: dict[str, Any],
    explicit_new_thread: bool = False,
    planned_at: datetime | None = None,
) -> StrategyRevisionResult:
    """Freeze exact old entry legs before any exchange cancellation."""

    if explicit_new_thread:
        return StrategyRevisionResult(status="new_thread_required")
    if strategy_thread_id is None:
        return StrategyRevisionResult(
            status="blocked",
            reason_code="revision_target_not_unique",
        )
    now = planned_at or datetime.now(UTC)
    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        thread = session.get(StrategyThread, int(strategy_thread_id))
        if raw_message is None or thread is None:
            return StrategyRevisionResult(
                status="blocked",
                reason_code="revision_target_not_found",
            )
        if int(raw_message.chat_id) != int(thread.chat_id):
            return StrategyRevisionResult(
                status="blocked",
                reason_code="revision_target_source_mismatch",
            )
        lifecycle = (
            session.get(StrategyLifecycle, int(thread.current_lifecycle_id))
            if thread.current_lifecycle_id is not None
            else None
        )
        if (
            lifecycle is None
            or lifecycle.strategy_thread_id != int(thread.id)
            or lifecycle.execution_binding_id is None
        ):
            return StrategyRevisionResult(
                status="blocked",
                reason_code="revision_target_not_unique",
            )
        binding = session.get(
            ExecutionBinding,
            int(lifecycle.execution_binding_id),
        )
        if binding is None:
            return StrategyRevisionResult(
                status="blocked",
                reason_code="revision_binding_missing",
            )
        entry_legs = (
            session.query(ExecutionOrderLeg)
            .filter(
                ExecutionOrderLeg.execution_binding_id == int(binding.id),
                ExecutionOrderLeg.purpose == "entry",
            )
            .order_by(ExecutionOrderLeg.leg_index.asc(), ExecutionOrderLeg.id.asc())
            .all()
        )
        if not entry_legs:
            return StrategyRevisionResult(
                status="blocked",
                reason_code="revision_entry_legs_missing",
            )
        classified: list[tuple[ExecutionOrderLeg, str]] = []
        for leg in entry_legs:
            if _entry_leg_size(leg) is None:
                return StrategyRevisionResult(
                    status="blocked",
                    reason_code="revision_entry_leg_size_invalid",
                )
            state = str(leg.status or "").lower()
            if leg.pos_id and state in FILLED_ENTRY_STATES | {"active", "open"}:
                classified.append((leg, "retain_filled"))
            elif not leg.pos_id and state in PENDING_ENTRY_STATES:
                if not leg.order_id and not leg.client_order_id:
                    return StrategyRevisionResult(
                        status="blocked",
                        reason_code="revision_pending_leg_identity_missing",
                    )
                classified.append((leg, "cancel_pending"))
            elif state in {"cancelled", "rejected", "failed", "expired"}:
                classified.append((leg, "already_terminal"))
            else:
                return StrategyRevisionResult(
                    status="blocked",
                    reason_code="revision_leg_state_ambiguous",
                )
        fingerprint_payload = {
            "raw_message_id": int(raw_message_id),
            "strategy_thread_id": int(strategy_thread_id),
            "target_lifecycle_id": int(lifecycle.id),
            "execution_binding_id": int(binding.id),
            "replacement": replacement,
            "legs": [
                {
                    "id": int(leg.id),
                    "status": leg.status,
                    "order_id": leg.order_id,
                    "client_order_id": leg.client_order_id,
                    "pos_id": leg.pos_id,
                    "action": action,
                }
                for leg, action in classified
            ],
        }
        fingerprint = hashlib.sha256(
            _canonical_json(fingerprint_payload).encode("utf-8")
        ).hexdigest()
        existing = (
            session.query(StrategyRevisionBatch)
            .filter(
                StrategyRevisionBatch.idempotency_fingerprint == fingerprint
            )
            .one_or_none()
        )
        if existing is not None:
            return StrategyRevisionResult(
                status=str(existing.status),
                batch_id=int(existing.id),
                reason_code=existing.reason_code,
            )
        batch = StrategyRevisionBatch(
            idempotency_fingerprint=fingerprint,
            raw_message_id=int(raw_message_id),
            strategy_thread_id=int(strategy_thread_id),
            target_lifecycle_id=int(lifecycle.id),
            execution_binding_id=int(binding.id),
            status="planned",
            replacement_json=_canonical_json(replacement),
            planned_at=now,
            updated_at=now,
        )
        session.add(batch)
        session.flush()
        for leg, action in classified:
            session.add(
                StrategyRevisionLeg(
                    revision_batch_id=int(batch.id),
                    execution_order_leg_id=int(leg.id),
                    action=action,
                    prior_status=str(leg.status),
                    status=(
                        "retained"
                        if action == "retain_filled"
                        else "terminal"
                        if action == "already_terminal"
                        else "planned"
                    ),
                    order_id=leg.order_id,
                    client_order_id=leg.client_order_id,
                    pos_id=leg.pos_id,
                    updated_at=now,
                )
            )
        session.commit()
        return StrategyRevisionResult(
            status="planned",
            batch_id=int(batch.id),
        )


def _mark_recovery_required(
    session_factory,
    *,
    batch_id: int,
    revision_leg_id: int | None,
    reason_code: str,
    response: Any = None,
    advanced_at: datetime,
) -> StrategyRevisionResult:
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        if batch is None:
            raise LookupError("strategy revision batch not found")
        batch.status = "recovery_required"
        batch.reason_code = reason_code
        batch.advance_claim_token = None
        batch.advance_claimed_at = None
        batch.updated_at = advanced_at
        if revision_leg_id is not None:
            leg = session.get(StrategyRevisionLeg, int(revision_leg_id))
            if leg is not None:
                leg.status = "submit_unknown"
                leg.response_json = (
                    _canonical_json(response) if response is not None else None
                )
                leg.error_json = _canonical_json({"reason": reason_code})
                leg.updated_at = advanced_at
        session.commit()
    return StrategyRevisionResult(
        status="recovery_required",
        batch_id=int(batch_id),
        reason_code=reason_code,
    )


def advance_strategy_revision(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    cancel_leg_writer: Callable[..., dict[str, Any]],
    replacement_writer: Callable[..., dict[str, Any]],
    read_leg_state: Callable[..., dict[str, Any]] | None = None,
    advanced_at: datetime | None = None,
) -> StrategyRevisionResult:
    """Advance cancellation then replacement, never retrying unknown writes."""

    now = advanced_at or datetime.now(UTC)
    claim_token = uuid.uuid4().hex
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        if batch is None:
            raise LookupError("strategy revision batch not found")
        if batch.status in TERMINAL_REVISION_STATES:
            return StrategyRevisionResult(
                status=str(batch.status),
                batch_id=int(batch.id),
                reason_code=batch.reason_code,
            )
        if batch.advance_claim_token:
            claimed_at = batch.advance_claimed_at
            comparable_now = now
            if claimed_at is not None:
                if claimed_at.tzinfo is None and comparable_now.tzinfo is not None:
                    claimed_at = claimed_at.replace(tzinfo=UTC)
                elif claimed_at.tzinfo is not None and comparable_now.tzinfo is None:
                    comparable_now = comparable_now.replace(tzinfo=UTC)
            if (
                claimed_at is not None
                and comparable_now - claimed_at
                >= REVISION_ADVANCE_CLAIM_LEASE
            ):
                stale_batch_id = int(batch.id)
                session.rollback()
                return _mark_recovery_required(
                    session_factory,
                    batch_id=stale_batch_id,
                    revision_leg_id=None,
                    reason_code="revision_advance_claim_stale",
                    advanced_at=now,
                )
            return StrategyRevisionResult(
                status="in_progress",
                batch_id=int(batch.id),
                reason_code="revision_advance_already_claimed",
            )
        if batch.status in REPLACEMENT_WRITE_BOUNDARY_STATES:
            return StrategyRevisionResult(
                status=str(batch.status),
                batch_id=int(batch.id),
                reason_code="revision_replacement_reconciliation_required",
            )
        claimed = session.execute(
            update(StrategyRevisionBatch)
            .where(
                StrategyRevisionBatch.id == int(batch_id),
                StrategyRevisionBatch.status == str(batch.status),
                StrategyRevisionBatch.advance_claim_token.is_(None),
            )
            .values(
                status="cancelling_old_entries",
                advance_claim_token=claim_token,
                advance_claimed_at=now,
                updated_at=now,
            )
        ).rowcount
        session.commit()
        if claimed != 1:
            return StrategyRevisionResult(
                status="in_progress",
                batch_id=int(batch_id),
                reason_code="revision_advance_claim_conflict",
            )

    with session_factory() as session:
        revision_legs = (
            session.query(StrategyRevisionLeg)
            .filter(StrategyRevisionLeg.revision_batch_id == int(batch_id))
            .order_by(StrategyRevisionLeg.id.asc())
            .all()
        )
        revision_leg_ids = [int(row.id) for row in revision_legs]

    for revision_leg_id in revision_leg_ids:
        with session_factory() as session:
            revision_leg = session.get(StrategyRevisionLeg, revision_leg_id)
            batch = session.get(StrategyRevisionBatch, int(batch_id))
            if revision_leg is None or batch is None:
                raise LookupError("strategy revision state disappeared")
            if revision_leg.action != "cancel_pending" or revision_leg.status in {
                "cancelled",
                "retained",
                "terminal",
            }:
                continue
            if revision_leg.status == "submit_unknown":
                return _mark_recovery_required(
                    session_factory,
                    batch_id=batch_id,
                    revision_leg_id=revision_leg_id,
                    reason_code="revision_cancel_outcome_unknown",
                    advanced_at=now,
                )
            writer_args = {
                "batch_id": int(batch.id),
                "strategy_thread_id": int(batch.strategy_thread_id),
                "execution_binding_id": int(batch.execution_binding_id),
                "execution_order_leg_id": int(
                    revision_leg.execution_order_leg_id
                ),
                "order_id": revision_leg.order_id,
                "client_order_id": revision_leg.client_order_id,
            }
        observed = (
            read_leg_state(**writer_args)
            if read_leg_state is not None
            else {"status": "pending"}
        )
        observed_status = str(observed.get("status") or "").lower()
        if observed_status in {"filled", "active", "position_open"}:
            pos_id = str(observed.get("pos_id") or "")
            with session_factory() as session:
                revision_leg = session.get(StrategyRevisionLeg, revision_leg_id)
                execution_leg = session.get(
                    ExecutionOrderLeg,
                    int(revision_leg.execution_order_leg_id),
                )
                batch = session.get(StrategyRevisionBatch, int(batch_id))
                lifecycle = session.get(
                    StrategyLifecycle,
                    int(batch.target_lifecycle_id),
                )
                revision_leg.action = "retain_filled"
                revision_leg.status = "retained"
                revision_leg.pos_id = pos_id or execution_leg.pos_id
                revision_leg.updated_at = now
                execution_leg.status = "filled"
                if pos_id:
                    execution_leg.pos_id = pos_id
                    execution_leg.attribution_status = "verified"
                execution_leg.updated_at = now
                lifecycle.lifecycle_status = "entered"
                lifecycle.entered_at = lifecycle.entered_at or now
                lifecycle.updated_at = now
                session.commit()
            continue
        with session_factory() as session:
            revision_leg = session.get(StrategyRevisionLeg, revision_leg_id)
            if revision_leg.status != "planned":
                return _mark_recovery_required(
                    session_factory,
                    batch_id=batch_id,
                    revision_leg_id=revision_leg_id,
                    reason_code="revision_cancel_restart_requires_reconciliation",
                    advanced_at=now,
                )
            revision_leg.status = "cancel_submitting"
            revision_leg.updated_at = now
            session.commit()
        try:
            response = cancel_leg_writer(**writer_args)
        except Exception:
            return _mark_recovery_required(
                session_factory,
                batch_id=batch_id,
                revision_leg_id=revision_leg_id,
                reason_code="revision_cancel_outcome_unknown",
                advanced_at=now,
            )
        response_status = str(response.get("status") or "").lower()
        if response_status not in {
            "confirmed",
            "confirmed_cancelled",
            "cancelled",
        }:
            return _mark_recovery_required(
                session_factory,
                batch_id=batch_id,
                revision_leg_id=revision_leg_id,
                reason_code="revision_cancel_outcome_unknown",
                response=response,
                advanced_at=now,
            )
        with session_factory() as session:
            revision_leg = session.get(StrategyRevisionLeg, revision_leg_id)
            execution_leg = session.get(
                ExecutionOrderLeg,
                int(revision_leg.execution_order_leg_id),
            )
            revision_leg.status = "cancelled"
            revision_leg.response_json = _canonical_json(response)
            revision_leg.updated_at = now
            execution_leg.status = "cancelled"
            execution_leg.updated_at = now
            session.commit()

    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        legs = (
            session.query(StrategyRevisionLeg)
            .filter(StrategyRevisionLeg.revision_batch_id == int(batch_id))
            .all()
        )
        if any(
            leg.status not in {"cancelled", "retained", "terminal"}
            for leg in legs
        ):
            return _mark_recovery_required(
                session_factory,
                batch_id=batch_id,
                revision_leg_id=None,
                reason_code="revision_old_entries_not_terminal",
                advanced_at=now,
            )
        batch.status = "old_entries_terminal"
        batch.updated_at = now
        execution_legs = {
            int(row.id): row
            for row in session.query(ExecutionOrderLeg)
            .filter(
                ExecutionOrderLeg.id.in_(
                    [int(leg.execution_order_leg_id) for leg in legs]
                )
            )
            .all()
        }
        sizes = {
            int(leg.execution_order_leg_id): _entry_leg_size(
                execution_legs[int(leg.execution_order_leg_id)]
            )
            for leg in legs
        }
        if any(size is None for size in sizes.values()):
            return _mark_recovery_required(
                session_factory,
                batch_id=batch_id,
                revision_leg_id=None,
                reason_code="revision_entry_leg_size_invalid",
                advanced_at=now,
            )
        total_size = sum(sizes.values(), Decimal("0"))
        retained_size = sum(
            (
                sizes[int(leg.execution_order_leg_id)]
                for leg in legs
                if leg.status == "retained"
            ),
            Decimal("0"),
        )
        remaining_fraction = float(
            max(Decimal("0"), (total_size - retained_size) / total_size)
        )
        replacement = json.loads(batch.replacement_json)
        if remaining_fraction <= 0:
            batch.status = "succeeded"
            batch.advance_claim_token = None
            batch.advance_claimed_at = None
            batch.completed_at = now
            session.commit()
            return StrategyRevisionResult(
                status="succeeded",
                batch_id=int(batch.id),
                remaining_fraction=0.0,
            )
        batch.status = "submitting_replacements"
        batch.updated_at = now
        writer_args = {
            "batch_id": int(batch.id),
            "strategy_thread_id": int(batch.strategy_thread_id),
            "target_lifecycle_id": int(batch.target_lifecycle_id),
            "execution_binding_id": int(batch.execution_binding_id),
            "replacement": replacement,
            "remaining_fraction": remaining_fraction,
            "retained_execution_leg_ids": [
                int(leg.execution_order_leg_id)
                for leg in legs
                if leg.status == "retained"
            ],
        }
        if batch.advance_claim_token != claim_token:
            session.rollback()
            return StrategyRevisionResult(
                status="in_progress",
                batch_id=int(batch.id),
                reason_code="revision_advance_claim_lost",
            )
        session.commit()
    try:
        replacement_response = replacement_writer(**writer_args)
    except Exception:
        return _mark_recovery_required(
            session_factory,
            batch_id=batch_id,
            revision_leg_id=None,
            reason_code="revision_replacement_submit_unknown",
            advanced_at=now,
        )
    replacement_status = str(replacement_response.get("status") or "").lower()
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        batch.replacement_response_json = _canonical_json(replacement_response)
        batch.updated_at = now
        if replacement_status in {"confirmed", "succeeded"}:
            batch.status = "succeeded"
            batch.advance_claim_token = None
            batch.advance_claimed_at = None
            batch.completed_at = now
            session.commit()
            return StrategyRevisionResult(
                status="succeeded",
                batch_id=int(batch.id),
                remaining_fraction=remaining_fraction,
            )
        if replacement_status == "submitted":
            batch.status = "reconciling"
            batch.advance_claim_token = None
            batch.advance_claimed_at = None
            session.commit()
            return StrategyRevisionResult(
                status="reconciling",
                batch_id=int(batch.id),
                remaining_fraction=remaining_fraction,
            )
        session.commit()
    return _mark_recovery_required(
        session_factory,
        batch_id=batch_id,
        revision_leg_id=None,
        reason_code="revision_replacement_submit_unknown",
        response=replacement_response,
        advanced_at=now,
    )

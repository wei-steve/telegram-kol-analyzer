"""Crash-safe orchestration for deleted Telegram strategy sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

from sqlalchemy import or_, update

from telegram_kol_research.models import (
    ExecutionEvent,
    ExecutionOrderLeg,
    SourceMessageDeletionExit,
)
from telegram_kol_research.terminal_entry_cleanup import (
    cleanup_terminal_entry_legs,
)


_ACTIVE_STATES = (
    "pending",
    "cancelling_entries",
    "closing_positions",
    "reconciling",
)


@dataclass(frozen=True, slots=True)
class SourceMessageDeletionWorkerResult:
    discovered: int = 0
    cancelled: int = 0
    planned_exits: int = 0
    finalized: int = 0
    waiting: int = 0
    recovery_required: int = 0


def run_source_message_deletion_worker_tick(
    session_factory,
    *,
    deepcoin_client_factory,
    contract_spec_provider=None,
    max_jobs: int = 10,
    processed_at: datetime | None = None,
    cleanup_executor=cleanup_terminal_entry_legs,
    exit_planner=None,
    snapshot_loader=None,
) -> SourceMessageDeletionWorkerResult:
    """Process a bounded number of exact deleted-source jobs."""

    now = processed_at or datetime.now(UTC)
    counts = {
        "discovered": 0,
        "cancelled": 0,
        "planned_exits": 0,
        "finalized": 0,
        "waiting": 0,
        "recovery_required": 0,
    }
    client = None

    def get_client():
        nonlocal client
        if client is None:
            client = deepcoin_client_factory()
        return client

    for _ in range(max(0, int(max_jobs))):
        claim = _claim_next_job(session_factory, claimed_at=now)
        if claim is None:
            break
        counts["discovered"] += 1
        exit_id, state, claim_token = claim
        if state != "cancelling_entries":
            _release_claim(
                session_factory,
                exit_id=exit_id,
                claim_token=claim_token,
                state=state,
                reason="stage_not_implemented",
                updated_at=now,
            )
            counts["waiting"] += 1
            continue

        with session_factory() as session:
            deletion_exit = session.get(SourceMessageDeletionExit, exit_id)
            lifecycle_id = (
                int(deletion_exit.target_lifecycle_id)
                if deletion_exit is not None
                and deletion_exit.target_lifecycle_id is not None
                else None
            )
            binding_id = (
                int(deletion_exit.execution_binding_id)
                if deletion_exit is not None
                and deletion_exit.execution_binding_id is not None
                else None
            )
        if lifecycle_id is None:
            _transition_claimed(
                session_factory,
                exit_id=exit_id,
                claim_token=claim_token,
                new_state="recovery_required",
                reason="exact_lifecycle_missing",
                error="exact_lifecycle_missing",
                updated_at=now,
            )
            counts["recovery_required"] += 1
            continue
        if binding_id is None:
            _transition_claimed(
                session_factory,
                exit_id=exit_id,
                claim_token=claim_token,
                new_state="reconciling",
                reason="no_execution_binding",
                updated_at=now,
            )
            counts["waiting"] += 1
            continue

        try:
            cleanup = cleanup_executor(
                session_factory,
                lifecycle_id=lifecycle_id,
                deepcoin_client=get_client(),
                reason="source_message_deleted",
                cleaned_at=now,
            )
        except Exception as exc:
            _transition_claimed(
                session_factory,
                exit_id=exit_id,
                claim_token=claim_token,
                new_state="recovery_required",
                reason="entry_cancel_exception",
                error=f"entry_cancel_exception:{type(exc).__name__}:{exc}",
                updated_at=now,
            )
            counts["recovery_required"] += 1
            continue

        if cleanup.status not in {"resolved", "already_absent"}:
            _transition_claimed(
                session_factory,
                exit_id=exit_id,
                claim_token=claim_token,
                new_state="recovery_required",
                reason=f"entry_cancel_{cleanup.status}",
                error=f"entry_cancel_outcome_{cleanup.status}",
                updated_at=now,
            )
            counts["recovery_required"] += 1
            continue

        signal_ids = _trade_signal_ids_for_events(
            session_factory,
            event_ids=cleanup.event_ids,
        )
        with session_factory() as session:
            owned_positions = (
                session.query(ExecutionOrderLeg.id)
                .filter(
                    ExecutionOrderLeg.execution_binding_id == binding_id,
                    ExecutionOrderLeg.purpose == "entry",
                    ExecutionOrderLeg.pos_id.is_not(None),
                    ExecutionOrderLeg.attribution_status == "verified",
                )
                .first()
                is not None
            )
            if cleanup.leg_ids:
                (
                    session.query(ExecutionOrderLeg)
                    .filter(ExecutionOrderLeg.id.in_(cleanup.leg_ids))
                    .update(
                        {
                            ExecutionOrderLeg.terminal_reason: (
                                "source_message_deleted_entry_cancelled"
                            )
                        },
                        synchronize_session=False,
                    )
                )
            session.commit()
        _transition_claimed(
            session_factory,
            exit_id=exit_id,
            claim_token=claim_token,
            new_state="closing_positions" if owned_positions else "reconciling",
            reason="entry_cancellation_confirmed",
            cancellation_signal_ids=signal_ids,
            updated_at=now,
        )
        counts["cancelled"] += 1

    return SourceMessageDeletionWorkerResult(**counts)


def _claim_next_job(session_factory, *, claimed_at: datetime):
    stale_before = claimed_at - timedelta(minutes=5)
    with session_factory() as session:
        candidates = (
            session.query(SourceMessageDeletionExit.id, SourceMessageDeletionExit.state)
            .filter(SourceMessageDeletionExit.state.in_(_ACTIVE_STATES))
            .filter(
                or_(
                    SourceMessageDeletionExit.claim_token.is_(None),
                    SourceMessageDeletionExit.claimed_at <= stale_before,
                )
            )
            .order_by(SourceMessageDeletionExit.id.asc())
            .limit(20)
            .all()
        )
    for exit_id, current_state in candidates:
        token = uuid4().hex
        claimed_state = (
            "cancelling_entries" if current_state == "pending" else current_state
        )
        with session_factory() as session:
            result = session.execute(
                update(SourceMessageDeletionExit)
                .where(
                    SourceMessageDeletionExit.id == int(exit_id),
                    SourceMessageDeletionExit.state == current_state,
                    or_(
                        SourceMessageDeletionExit.claim_token.is_(None),
                        SourceMessageDeletionExit.claimed_at <= stale_before,
                    ),
                )
                .values(
                    state=claimed_state,
                    claim_token=token,
                    claimed_at=claimed_at,
                    attempt_count=SourceMessageDeletionExit.attempt_count + 1,
                    updated_at=claimed_at,
                )
            )
            session.commit()
            if result.rowcount == 1:
                return int(exit_id), str(claimed_state), token
    return None


def _transition_claimed(
    session_factory,
    *,
    exit_id: int,
    claim_token: str,
    new_state: str,
    reason: str,
    updated_at: datetime,
    error: str | None = None,
    cancellation_signal_ids: tuple[int, ...] | None = None,
) -> bool:
    values = {
        "state": new_state,
        "claim_token": None,
        "claimed_at": None,
        "last_reason": reason,
        "last_error": error,
        "updated_at": updated_at,
    }
    if cancellation_signal_ids is not None:
        values["cancellation_signal_ids_json"] = json.dumps(
            list(cancellation_signal_ids)
        )
    with session_factory() as session:
        result = session.execute(
            update(SourceMessageDeletionExit)
            .where(
                SourceMessageDeletionExit.id == int(exit_id),
                SourceMessageDeletionExit.claim_token == claim_token,
            )
            .values(**values)
        )
        session.commit()
        return result.rowcount == 1


def _release_claim(
    session_factory,
    *,
    exit_id: int,
    claim_token: str,
    state: str,
    reason: str,
    updated_at: datetime,
) -> bool:
    return _transition_claimed(
        session_factory,
        exit_id=exit_id,
        claim_token=claim_token,
        new_state=state,
        reason=reason,
        updated_at=updated_at,
    )


def _trade_signal_ids_for_events(
    session_factory,
    *,
    event_ids,
) -> tuple[int, ...]:
    if not event_ids:
        return ()
    with session_factory() as session:
        rows = (
            session.query(ExecutionEvent.trade_signal_id)
            .filter(
                ExecutionEvent.id.in_(tuple(int(value) for value in event_ids)),
                ExecutionEvent.trade_signal_id.is_not(None),
            )
            .all()
        )
    return tuple(sorted({int(value) for (value,) in rows}))

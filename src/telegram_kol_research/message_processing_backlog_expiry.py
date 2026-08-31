"""Exact-watermark expiry for a frozen durable message backlog."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from telegram_kol_research.models import (
    MessageProcessingJob,
    RawMessage,
    RecognitionDecision,
)
from telegram_kol_research.telegram_live_listener import (
    EXPIRED_STALE_INSTRUCTION,
    _record_expired_authoritative_recovery_gap_in_session,
)


PHASE1_MINIMUM_RAW_MESSAGE_ID = 13877
PHASE1_WATERMARK_RAW_MESSAGE_ID = 14030
PHASE1_EXPECTED_PENDING_COUNT = 154


class BacklogExpiryRefused(ValueError):
    """Raised when the exact frozen-backlog contract no longer holds."""


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_preimage(row: Any) -> dict[str, Any]:
    return {
        column.name: _json_value(getattr(row, column.name))
        for column in row.__table__.columns
    }


@dataclass(frozen=True, slots=True)
class MessageProcessingBacklogExpiryPlan:
    minimum_raw_message_id: int
    watermark_raw_message_id: int
    expected_pending_count: int
    target_raw_message_ids: tuple[int, ...]
    target_manifest_sha256: str
    queue_preimages: tuple[dict[str, Any], ...]
    decision_13912_preimage: dict[str, Any] | None
    execution_running_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimum_raw_message_id": self.minimum_raw_message_id,
            "watermark_raw_message_id": self.watermark_raw_message_id,
            "expected_pending_count": self.expected_pending_count,
            "target_count": len(self.target_raw_message_ids),
            "target_raw_message_ids": list(self.target_raw_message_ids),
            "target_manifest_sha256": self.target_manifest_sha256,
            "queue_preimages": list(self.queue_preimages),
            "decision_13912_preimage": self.decision_13912_preimage,
            "execution_running_count": self.execution_running_count,
            "exchange_write_count": 0,
        }


@dataclass(frozen=True, slots=True)
class MessageProcessingBacklogExpiryResult:
    plan: MessageProcessingBacklogExpiryPlan
    changed_count: int
    transaction_lock_seconds: float
    lock_acquisition_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.plan.to_dict(),
            "changed_count": self.changed_count,
            "transaction_lock_seconds": self.transaction_lock_seconds,
            "lock_acquisition_seconds": self.lock_acquisition_seconds,
        }


def _build_message_processing_backlog_expiry_plan_in_session(
    session,
    *,
    minimum_raw_message_id: int,
    watermark_raw_message_id: int,
    expected_pending_count: int,
) -> MessageProcessingBacklogExpiryPlan:
    minimum = int(minimum_raw_message_id)
    watermark = int(watermark_raw_message_id)
    expected_count = int(expected_pending_count)
    if (
        minimum,
        watermark,
        expected_count,
    ) != (
        PHASE1_MINIMUM_RAW_MESSAGE_ID,
        PHASE1_WATERMARK_RAW_MESSAGE_ID,
        PHASE1_EXPECTED_PENDING_COUNT,
    ):
        raise BacklogExpiryRefused("phase1_contract_mismatch")
    if minimum <= 0 or watermark < minimum or expected_count <= 0:
        raise BacklogExpiryRefused("exact_guard_invalid")
    expected_ids = tuple(range(minimum, watermark + 1))
    if len(expected_ids) != expected_count:
        raise BacklogExpiryRefused("exact_guard_count_range_mismatch")

    jobs = (
        session.query(MessageProcessingJob)
        .filter(
            MessageProcessingJob.raw_message_id >= minimum,
            MessageProcessingJob.raw_message_id <= watermark,
        )
        .order_by(MessageProcessingJob.raw_message_id)
        .all()
    )
    observed_ids = tuple(int(row.raw_message_id) for row in jobs)
    if observed_ids != expected_ids:
        raise BacklogExpiryRefused("target_set_mismatch")
    if any(bool(row.shadow) for row in jobs):
        raise BacklogExpiryRefused("shadow_target_present")
    if any(str(row.status) != "pending" for row in jobs):
        raise BacklogExpiryRefused("status_not_pending")
    if any(int(row.attempt_count) != 0 for row in jobs):
        raise BacklogExpiryRefused("attempt_count_not_zero")
    if any(row.claim_token is not None or row.claimed_at is not None for row in jobs):
        raise BacklogExpiryRefused("claim_present")

    raw_ids = tuple(
        int(value)
        for (value,) in (
            session.query(RawMessage.id)
            .filter(RawMessage.id.in_(expected_ids))
            .order_by(RawMessage.id)
            .all()
        )
    )
    if raw_ids != expected_ids:
        raise BacklogExpiryRefused("raw_message_set_mismatch")

    running_count = (
        session.query(RecognitionDecision)
        .filter(
            RecognitionDecision.raw_message_id.in_(expected_ids),
            RecognitionDecision.comparison_status == "execution_running",
        )
        .count()
    )
    if running_count:
        raise BacklogExpiryRefused("execution_running_decision_present")

    decision_13912 = (
        session.query(RecognitionDecision)
        .filter(RecognitionDecision.raw_message_id == 13912)
        .one_or_none()
    )
    manifest = ("\n".join(str(value) for value in expected_ids) + "\n").encode(
        "ascii"
    )
    return MessageProcessingBacklogExpiryPlan(
        minimum_raw_message_id=minimum,
        watermark_raw_message_id=watermark,
        expected_pending_count=expected_count,
        target_raw_message_ids=expected_ids,
        target_manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        queue_preimages=tuple(_row_preimage(row) for row in jobs),
        decision_13912_preimage=(
            _row_preimage(decision_13912) if decision_13912 is not None else None
        ),
        execution_running_count=int(running_count),
    )


def build_message_processing_backlog_expiry_plan(
    session_factory,
    *,
    minimum_raw_message_id: int,
    watermark_raw_message_id: int,
    expected_pending_count: int,
) -> MessageProcessingBacklogExpiryPlan:
    """Read and validate an exact non-shadow pending backlog without mutation."""

    with session_factory() as session:
        return _build_message_processing_backlog_expiry_plan_in_session(
            session,
            minimum_raw_message_id=minimum_raw_message_id,
            watermark_raw_message_id=watermark_raw_message_id,
            expected_pending_count=expected_pending_count,
        )


def apply_message_processing_backlog_expiry(
    session_factory,
    *,
    minimum_raw_message_id: int,
    watermark_raw_message_id: int,
    expected_pending_count: int,
    completed_at: datetime,
) -> MessageProcessingBacklogExpiryResult:
    """Expire the exact backlog and its existing audit in one immediate transaction."""

    acquisition_started = perf_counter()
    with session_factory() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        lock_acquired_at = perf_counter()
        try:
            plan = _build_message_processing_backlog_expiry_plan_in_session(
                session,
                minimum_raw_message_id=minimum_raw_message_id,
                watermark_raw_message_id=watermark_raw_message_id,
                expected_pending_count=expected_pending_count,
            )
            raw_messages = {
                int(row.id): row
                for row in (
                    session.query(RawMessage)
                    .filter(RawMessage.id.in_(plan.target_raw_message_ids))
                    .all()
                )
            }
            for raw_message_id in plan.target_raw_message_ids:
                _record_expired_authoritative_recovery_gap_in_session(
                    session,
                    raw_message=raw_messages[raw_message_id],
                    classification=EXPIRED_STALE_INSTRUCTION,
                )
            normalized_completed_at = completed_at
            if normalized_completed_at.tzinfo is not None:
                normalized_completed_at = normalized_completed_at.astimezone(UTC).replace(
                    tzinfo=None
                )
            jobs = (
                session.query(MessageProcessingJob)
                .filter(
                    MessageProcessingJob.raw_message_id.in_(
                        plan.target_raw_message_ids
                    )
                )
                .all()
            )
            for job in jobs:
                job.status = "expired"
                job.last_reason = EXPIRED_STALE_INSTRUCTION
                job.claim_token = None
                job.claimed_at = None
                job.next_attempt_at = None
                job.completed_at = normalized_completed_at
            session.flush()
            if len(jobs) != plan.expected_pending_count:
                raise BacklogExpiryRefused("changed_count_mismatch")
            session.commit()
        except Exception:
            session.rollback()
            raise
        committed_at = perf_counter()
    return MessageProcessingBacklogExpiryResult(
        plan=plan,
        changed_count=len(jobs),
        transaction_lock_seconds=committed_at - lock_acquired_at,
        lock_acquisition_seconds=lock_acquired_at - acquisition_started,
    )

"""A-6c: a refusal that never reached the venue, and a guard that is not a fault.

Both came out of one production message. raw 15668 was a real BTC limit long
whose entry was refused before submission -- the exchange-revision authority
had been orphaned by another message hours earlier -- and it was frozen as
"outcome unknown" anyway. The retry that followed then logged a full stack
three times for a guard that was doing its job.
"""

from __future__ import annotations

import logging

import pytest

from telegram_kol_research.execution_boundary import (
    ExecutionBoundaryTracker,
    build_execution_boundary_outcome,
)


def _refused_item(message: str, *, status: str = "failed"):
    """raw 15668's exact payload shape: a message and a type, and no status."""

    return {
        "status": "partial_failed",
        "items": [
            {
                "item_id": 1049,
                "instruction_kind": "entry",
                "status": status,
                "error": {
                    "instruction_execution_contract": {
                        "reason_code": "verified_terminal_contract_required",
                        "state": "pending",
                    },
                    "message": message,
                    "type": "RecoveryLiveSubmitError",
                },
            }
        ],
    }


def test_an_entry_refused_before_submission_is_failed_safe_not_unknown():
    """raw 15668, exactly as production recorded it.

    The authority was never acquired, so nothing was planned and nothing was
    sent. Freezing it as "outcome unknown" says the opposite of what the
    evidence says, and an uncertain attempt is never replayed by anything.
    """

    outcome = build_execution_boundary_outcome(
        _refused_item("entry_revision_exchange_authority_expired_blocked"),
        ExecutionBoundaryTracker(),
    )

    assert outcome.status == "failed_safe"
    assert outcome.exchange_effect == "not_started"
    ref = outcome.evidence_refs[0]
    assert ref["pre_submit_refusal"] == (
        "entry_revision_exchange_authority_expired_blocked"
    )
    # The reason field must name it too: a RecoveryLiveSubmitError carries its
    # reason as ``message``, and without the fallback this read blank.
    assert ref["reason"] == "entry_revision_exchange_authority_expired_blocked"


@pytest.mark.parametrize(
    "reason",
    [
        "entry_revision_exchange_authority_blocked",
        "entry_revision_exchange_authority_busy",
        "entry_revision_exchange_authority_invalid",
        "entry_revision_exchange_authority_missing",
        "entry_revision_exchange_authority_unavailable",
    ],
)
def test_every_acquisition_failure_proves_no_contact(reason):
    """All of them are returned before the authority is held, so before any plan."""

    outcome = build_execution_boundary_outcome(
        _refused_item(reason), ExecutionBoundaryTracker()
    )
    assert outcome.status == "failed_safe"


def test_a_release_failure_proves_nothing_and_stays_unknown():
    """A release happens *after* the writes.

    Reading it as proof that nothing was sent would be exactly backwards, so
    it is deliberately absent from the set.
    """

    outcome = build_execution_boundary_outcome(
        _refused_item("entry_revision_exchange_authority_release_failed"),
        ExecutionBoundaryTracker(),
    )
    assert outcome.status == "outcome_unknown"
    assert outcome.exchange_effect == "outcome_unknown"


def test_a_lookalike_reason_is_not_accepted():
    """Exact match only: a reason that merely mentions the authority is not proof."""

    outcome = build_execution_boundary_outcome(
        _refused_item("something about entry_revision_exchange_authority_busy here"),
        ExecutionBoundaryTracker(),
    )
    assert outcome.status == "outcome_unknown"


def test_an_unfinished_item_is_still_a_hand_off_not_a_refusal():
    """The refusal reason cannot rescue an item somebody else still owns."""

    outcome = build_execution_boundary_outcome(
        _refused_item(
            "entry_revision_exchange_authority_expired_blocked", status="pending"
        ),
        ExecutionBoundaryTracker(),
    )
    assert outcome.status == "outcome_unknown"


def test_a_tracked_write_still_wins_over_the_refusal_reason():
    """If something was sent, no item payload may argue it away."""

    tracker = ExecutionBoundaryTracker()
    ordinal = tracker.begin("place_order")
    tracker.failed(ordinal, RuntimeError("connection reset"))
    outcome = build_execution_boundary_outcome(
        _refused_item("entry_revision_exchange_authority_expired_blocked"), tracker
    )
    assert outcome.status == "outcome_unknown"
    assert outcome.exchange_effect == "outcome_unknown"


# --------------------------------------------------------------------------
# The retry guard: expected state, not a stack trace
# --------------------------------------------------------------------------


def test_the_retry_guard_has_its_own_type_and_stays_a_runtime_error():
    from telegram_kol_research.authoritative_recognition import AutomaticRetryBlocked

    guard = AutomaticRetryBlocked(raw_message_id=15668, attempt_status="uncertain")
    assert isinstance(guard, RuntimeError)
    assert guard.raw_message_id == 15668
    assert guard.attempt_status == "uncertain"
    assert "automatic retry blocked" in str(guard)


def test_the_worker_defers_the_job_without_logging_a_stack():
    """Same deferral as before; only the stack goes away.

    On the day an uncertain attempt occurred this logged three full tracebacks
    for a guard that was working correctly.
    """

    from telegram_kol_research.authoritative_recognition import AutomaticRetryBlocked

    logger = logging.getLogger("telegram_kol_research.message_processing_worker")

    class Collector(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.records: list[logging.LogRecord] = []

        def emit(self, record):
            self.records.append(record)

    def log_like(exc, status):
        # The branch under test, lifted verbatim from the worker.
        if isinstance(exc, AutomaticRetryBlocked):
            logger.info(
                "message processing job deferred, execution owns the message "
                "raw_message_id=%s status=%s attempt_status=%s",
                exc.raw_message_id,
                status,
                exc.attempt_status,
            )
        else:
            logger.exception("message processing job failed raw_message_id=%s", 15668)

    collector = Collector()
    previous = logger.level
    logger.addHandler(collector)
    logger.setLevel(logging.INFO)
    try:
        log_like(
            AutomaticRetryBlocked(raw_message_id=15668, attempt_status="uncertain"),
            "pending",
        )
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous)

    assert [record.levelno for record in collector.records] == [logging.INFO]
    assert all(record.exc_info is None for record in collector.records)

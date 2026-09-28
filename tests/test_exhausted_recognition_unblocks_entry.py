"""2026-09-28: a spent recognition retry must stop holding the next entry.

Replays 陈哥's chat as production recorded it. Raw 19490 ("止盈止损有调整我删了
重新发。") carried ``cancel_entry`` evidence, its authoritative recognition
failed five times, and its processing job went ``failed`` at 03:23:38. The
decision row still read ``skipped / mimo_authoritative_failed`` -- the value
the admission barrier treats as "a decision may still arrive" -- so the
corrected BTC long 19491 posted five seconds after it sat deferred behind it
until a manual rewrite released it at 03:38. Left alone it would have expired
at the six-hour deadline.

Two fixes, both covered here: the worker rewrites the reason to
``mimo_authoritative_failed_exhausted`` in the transaction that fails the job
and then wakes the blocked entry (4a); the barrier reads a failed job as
terminal even on a row nobody rewrote (4b).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research import entry_assembly_admission as admission_module
from telegram_kol_research.authoritative_execution_attempts import (
    ExecutionOwnerIdentity,
)
from telegram_kol_research.authoritative_execution_schema import (
    apply_recognition_execution_schema,
    build_recognition_execution_schema_plan,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_assembly_admission import (
    assess_entry_assembly_admission,
)
from telegram_kol_research.message_processing_worker import (
    AuthoritativeProcessingFailed,
    MessageProcessingClaim,
    run_message_processing_worker_tick,
)
from telegram_kol_research.models import (
    EntryAssemblyAttempt,
    MessageEvidenceVersion,
    MessageProcessingJob,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
)
from telegram_kol_research.recognition_failure_attribution import (
    MIMO_AUTHORITATIVE_FAILED,
    MIMO_AUTHORITATIVE_FAILED_EXHAUSTED,
)


CHAT_ID = -1002337721508
BLOCKER_RAW_ID = 19490
STRATEGY_RAW_ID = 19491
BLOCKER_POSTED_AT = datetime(2026, 9, 28, 3, 13, 35, tzinfo=UTC)
STRATEGY_POSTED_AT = datetime(2026, 9, 28, 3, 13, 40, tzinfo=UTC)
FIFTH_FAILURE_AT = datetime(2026, 9, 28, 3, 23, 38, tzinfo=UTC)
STRATEGY_ASSESSED_AT = datetime(2026, 9, 28, 3, 25, 20, tzinfo=UTC)
_CLAIM_TOKEN = "chen-19490-fifth-attempt"
_CANCEL_ENTRY_EVIDENCE = json.dumps(
    {
        "recognition_result": "非策略",
        "strategy": {},
        "lifecycle_event": {
            "event_type": "cancel_entry",
            "target_lifecycle_id": 1327,
        },
    },
    ensure_ascii=False,
)


def _wakeup_owner() -> ExecutionOwnerIdentity:
    return ExecutionOwnerIdentity("worker", "test-instance", 123, "boot", "456")


def _chen_chat(tmp_path, *, job_status: str, attempt_count: int):
    session_factory = create_session_factory(tmp_path / "chen-19490.db")
    engine = session_factory.kw["bind"]
    plan = build_recognition_execution_schema_plan(engine)
    apply_recognition_execution_schema(engine, expected_plan_sha256=plan.plan_sha256)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    id=BLOCKER_RAW_ID,
                    chat_id=CHAT_ID,
                    message_id=10791,
                    posted_at=BLOCKER_POSTED_AT,
                    text="止盈止损有调整我删了重新发。",
                ),
                RawMessage(
                    id=STRATEGY_RAW_ID,
                    chat_id=CHAT_ID,
                    message_id=10792,
                    posted_at=STRATEGY_POSTED_AT,
                    text="BTC 83000-83300 做多，止损 81400，止盈 85600-87000",
                ),
            ]
        )
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=STRATEGY_RAW_ID,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            confidence=1,
            recognition_generation="chen-19491",
        )
        session.add(candidate)
        session.add(
            MessageEvidenceVersion(
                raw_message_id=BLOCKER_RAW_ID,
                version=1,
                input_fingerprint="chen-19490",
                model="mimo",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=1,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json=_CANCEL_ENTRY_EVIDENCE,
            )
        )
        session.add(
            RecognitionDecision(
                raw_message_id=BLOCKER_RAW_ID,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="识别失败",
                authoritative_payload_json="{}",
                agreement_status="authoritative_failed",
                differences_json="[]",
                prompt_versions_json="{}",
                automation_status="skipped",
                automation_reason=MIMO_AUTHORITATIVE_FAILED,
                created_at=BLOCKER_POSTED_AT.replace(tzinfo=None),
                updated_at=BLOCKER_POSTED_AT.replace(tzinfo=None),
            )
        )
        job = MessageProcessingJob(
            raw_message_id=BLOCKER_RAW_ID,
            chat_id=CHAT_ID,
            status=job_status,
            attempt_count=attempt_count,
            claim_token=_CLAIM_TOKEN if job_status == "claimed" else None,
            claimed_at=(
                FIFTH_FAILURE_AT.replace(tzinfo=None)
                if job_status == "claimed"
                else None
            ),
            last_reason="processing_error:AuthoritativeProcessingFailed",
        )
        session.add(job)
        session.commit()
        return session_factory, int(candidate.id), int(job.id)


def _assess(session_factory, candidate_id, *, at=STRATEGY_ASSESSED_AT):
    return assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=STRATEGY_RAW_ID,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=at,
    )


def _decision_reason(session_factory) -> str | None:
    with session_factory() as session:
        return (
            session.query(RecognitionDecision.automation_reason)
            .filter(RecognitionDecision.raw_message_id == BLOCKER_RAW_ID)
            .scalar()
        )


def test_replay_before_the_fix_a_failed_job_held_the_entry(tmp_path, monkeypatch):
    """Production as it was: job ``failed``, reason never rewritten, no 4b."""

    session_factory, candidate_id, _ = _chen_chat(
        tmp_path, job_status="failed", attempt_count=5
    )
    monkeypatch.setattr(admission_module, "_JOB_EXHAUSTED_STATUSES", frozenset())

    decision = _assess(session_factory, candidate_id)

    assert decision.status == "deferred"
    assert decision.reason_code == "adjacent_entry_context_pending"
    assert decision.blocking_raw_message_ids == (BLOCKER_RAW_ID,)
    with session_factory() as session:
        attempt = session.query(EntryAssemblyAttempt).one()
        assert attempt.status == "pending"
        assert json.loads(attempt.blocking_raw_message_ids_json) == [BLOCKER_RAW_ID]


def test_replay_with_the_read_side_fix_a_failed_job_no_longer_blocks(tmp_path):
    """4b: the unrewritten row is terminal once its job is ``failed``."""

    session_factory, candidate_id, _ = _chen_chat(
        tmp_path, job_status="failed", attempt_count=5
    )
    assert _decision_reason(session_factory) == MIMO_AUTHORITATIVE_FAILED

    decision = _assess(session_factory, candidate_id)

    assert decision.status != "deferred"
    assert BLOCKER_RAW_ID not in decision.blocking_raw_message_ids


@pytest.mark.parametrize("job_status", ["pending", "claimed"])
def test_a_failure_that_will_still_be_retried_keeps_blocking(tmp_path, job_status):
    session_factory, candidate_id, _ = _chen_chat(
        tmp_path, job_status=job_status, attempt_count=3
    )

    decision = _assess(session_factory, candidate_id)

    assert decision.status == "deferred"
    assert decision.blocking_raw_message_ids == (BLOCKER_RAW_ID,)


def test_an_exhausted_reason_is_terminal_without_reading_the_job(tmp_path):
    """The rewritten value alone is enough -- it is not a retryable reason."""

    session_factory, candidate_id, job_id = _chen_chat(
        tmp_path, job_status="pending", attempt_count=3
    )
    with session_factory() as session:
        session.query(RecognitionDecision).update(
            {RecognitionDecision.automation_reason: MIMO_AUTHORITATIVE_FAILED_EXHAUSTED}
        )
        session.delete(session.get(MessageProcessingJob, job_id))
        session.commit()

    decision = _assess(session_factory, candidate_id)

    assert MIMO_AUTHORITATIVE_FAILED_EXHAUSTED not in (
        admission_module._NON_TERMINAL_SKIP_REASONS
    )
    assert decision.status != "deferred"


def _fifth_failure_claim(job_id: int) -> MessageProcessingClaim:
    return MessageProcessingClaim(
        job_id=job_id,
        raw_message_id=BLOCKER_RAW_ID,
        chat_id=CHAT_ID,
        attempt_count=4,
        claim_token=_CLAIM_TOKEN,
        source_reason="worker_claimed",
    )


async def _authoritative_failure(*_args, **_kwargs):
    raise AuthoritativeProcessingFailed(
        "authoritative processor returned authoritative_failed"
    )


def _production_wakeup(session_factory, monkeypatch, executed):
    """The web_app hook, with only the exchange-writing executor stubbed."""

    from telegram_kol_research import entry_assembly_wakeup_executions
    from telegram_kol_research.web_app import _run_exhausted_recognition_entry_wakeup

    def record_execution(session_factory_, *, wake_claim, **_kwargs):
        executed.append(wake_claim)

    monkeypatch.setattr(
        entry_assembly_wakeup_executions,
        "run_claimed_entry_assembly_wakeup",
        record_execution,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            runtime_role="worker",
            session_factory=session_factory,
            recognition_execution_owner=_wakeup_owner(),
            recognition_execution_registry=None,
            auto_trade_executor=object(),
        )
    )
    return lambda raw_message_id: _run_exhausted_recognition_entry_wakeup(
        app, raw_message_id
    )


def test_the_final_failure_marks_the_decision_exhausted_and_wakes_the_entry(
    tmp_path, monkeypatch
):
    """4a, end to end through the worker tick and the production hook."""

    session_factory, candidate_id, job_id = _chen_chat(
        tmp_path, job_status="claimed", attempt_count=4
    )
    # The attempt as production had it: deferred behind 19490.
    assert _assess(
        session_factory, candidate_id, at=FIFTH_FAILURE_AT - timedelta(seconds=30)
    ).blocking_raw_message_ids == (BLOCKER_RAW_ID,)
    executed = []

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=FIFTH_FAILURE_AT,
            job_processor=_authoritative_failure,
            loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
            entry_assembly_wakeup=_production_wakeup(
                session_factory, monkeypatch, executed
            ),
            _preclaimed_jobs=[_fifth_failure_claim(job_id)],
        )
    )

    assert result.failed == 1
    with session_factory() as session:
        job = session.get(MessageProcessingJob, job_id)
        assert (job.status, job.attempt_count) == ("failed", 5)
        decision = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == BLOCKER_RAW_ID)
            .one()
        )
        assert decision.automation_status == "skipped"
        assert decision.automation_reason == MIMO_AUTHORITATIVE_FAILED_EXHAUSTED
        assert decision.updated_at == FIFTH_FAILURE_AT.replace(tzinfo=None)
        attempt = session.query(EntryAssemblyAttempt).one()
        assert attempt.status == "claimed"
        assert json.loads(attempt.blocking_raw_message_ids_json) == []
    assert [
        (claim.strategy_raw_message_id, claim.trigger_raw_message_id)
        for claim in executed
    ] == [(STRATEGY_RAW_ID, BLOCKER_RAW_ID)]


def test_a_retryable_failure_neither_rewrites_nor_wakes(tmp_path):
    session_factory, candidate_id, job_id = _chen_chat(
        tmp_path, job_status="claimed", attempt_count=2
    )
    woken = []
    claim = MessageProcessingClaim(
        job_id=job_id,
        raw_message_id=BLOCKER_RAW_ID,
        chat_id=CHAT_ID,
        attempt_count=2,
        claim_token=_CLAIM_TOKEN,
        source_reason="worker_claimed",
    )

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=FIFTH_FAILURE_AT,
            job_processor=_authoritative_failure,
            loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
            entry_assembly_wakeup=woken.append,
            _preclaimed_jobs=[claim],
        )
    )

    assert result.retried == 1
    assert woken == []
    assert _decision_reason(session_factory) == MIMO_AUTHORITATIVE_FAILED


def test_a_final_failure_on_another_decision_is_left_alone(tmp_path):
    """Only ``skipped / mimo_authoritative_failed`` is rewritten, and only it wakes."""

    session_factory, candidate_id, job_id = _chen_chat(
        tmp_path, job_status="claimed", attempt_count=4
    )
    with session_factory() as session:
        session.query(RecognitionDecision).update(
            {
                RecognitionDecision.automation_status: "completed",
                RecognitionDecision.automation_reason: "auto_trade_executed",
            }
        )
        session.commit()
    woken = []

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=FIFTH_FAILURE_AT,
            job_processor=_authoritative_failure,
            loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
            entry_assembly_wakeup=woken.append,
            _preclaimed_jobs=[_fifth_failure_claim(job_id)],
        )
    )

    assert result.failed == 1
    assert woken == []
    assert _decision_reason(session_factory) == "auto_trade_executed"


def test_a_failing_wakeup_does_not_break_the_lane(tmp_path, caplog):
    session_factory, candidate_id, job_id = _chen_chat(
        tmp_path, job_status="claimed", attempt_count=4
    )

    def broken_wakeup(raw_message_id):
        raise RuntimeError("entry_assembly_wakeup_not_owned_by_runtime_role")

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=FIFTH_FAILURE_AT,
            job_processor=_authoritative_failure,
            loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
            entry_assembly_wakeup=broken_wakeup,
            _preclaimed_jobs=[_fifth_failure_claim(job_id)],
        )
    )

    assert result.failed == 1
    assert _decision_reason(session_factory) == MIMO_AUTHORITATIVE_FAILED_EXHAUSTED
    assert "entry assembly wakeup after exhausted recognition failed" in caplog.text
    # The read side still releases it on the reconciler's next recheck.
    assert _assess(session_factory, candidate_id).status != "deferred"


def test_the_worker_launch_passes_the_wakeup_hook():
    import inspect

    from telegram_kol_research import web_app as web_app_module

    source = inspect.getsource(web_app_module)
    start = source.index("app.state.message_processing_worker_runner(")
    end = source.index("# The claim loop is supervised rather than bare", start)
    assert "entry_assembly_wakeup=" in source[start:end]
    assert "_run_exhausted_recognition_entry_wakeup" in source[start:end]


# --------------------------------------------------------------------------
# Fail-closed guard: a later, unreadable cancellation of this very entry
# --------------------------------------------------------------------------

LATER_CANCEL_RAW_ID = 19493
LATER_CANCEL_POSTED_AT = datetime(2026, 9, 28, 3, 14, 10, tzinfo=UTC)


def _later_cancel_evidence(
    *,
    symbol="BTC",
    side="long",
    target_lifecycle_id=None,
    event_type="cancel_entry",
    management_action=None,
    message_classes=None,
):
    payload = {
        "recognition_result": "非策略",
        "strategy": {},
        "lifecycle_event": {
            "event_type": event_type,
            "target_lifecycle_id": target_lifecycle_id,
            "symbol": symbol,
            "side": side,
            "management_action": management_action,
        },
    }
    if message_classes is not None:
        payload["message_classes"] = message_classes
    return json.dumps(payload, ensure_ascii=False)


def _entry_then_later_cancel(
    tmp_path,
    *,
    evidence_json: str,
    reason: str = MIMO_AUTHORITATIVE_FAILED,
    job_status: str = "failed",
    own_lifecycle_id: int | None = None,
    other_lifecycle_id: int | None = None,
    other_lifecycle_status: str | None = None,
    attempt_count: int = 5,
):
    """19491's entry, then a later "BTC多单取消，不进了" whose recognition died."""

    session_factory = create_session_factory(tmp_path / "later-cancel.db")
    engine = session_factory.kw["bind"]
    plan = build_recognition_execution_schema_plan(engine)
    apply_recognition_execution_schema(engine, expected_plan_sha256=plan.plan_sha256)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    id=STRATEGY_RAW_ID,
                    chat_id=CHAT_ID,
                    message_id=10792,
                    posted_at=STRATEGY_POSTED_AT,
                    text="BTC 83000-83300 做多，止损 81400，止盈 85600-87000",
                ),
                RawMessage(
                    id=LATER_CANCEL_RAW_ID,
                    chat_id=CHAT_ID,
                    message_id=10794,
                    posted_at=LATER_CANCEL_POSTED_AT,
                    text="BTC多单取消，不进了",
                ),
            ]
        )
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=STRATEGY_RAW_ID,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            confidence=1,
            recognition_generation="chen-19491",
        )
        session.add(candidate)
        session.flush()
        if own_lifecycle_id is not None:
            from telegram_kol_research.models import StrategyLifecycle

            session.add(
                StrategyLifecycle(
                    id=own_lifecycle_id,
                    signal_candidate_id=candidate.id,
                    chat_id=CHAT_ID,
                    message_id=10792,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=STRATEGY_POSTED_AT.replace(tzinfo=None),
                )
            )
        if other_lifecycle_id is not None:
            # A lifecycle that is NOT this entry's own -- e.g. the old,
            # pre-existing thread a later "取消" is pinned onto exactly, the
            # way 陈哥's 19490 pinned onto lifecycle 1327. Its own
            # ``signal_candidate_id``/``message_id`` deliberately do not
            # match ``strategy``, so ``_own_lifecycle_ids`` never includes it.
            from telegram_kol_research.models import StrategyLifecycle

            session.add(
                StrategyLifecycle(
                    id=other_lifecycle_id,
                    chat_id=CHAT_ID,
                    message_id=10758,
                    symbol="BTC",
                    side="long",
                    lifecycle_status=other_lifecycle_status or "expired",
                    signal_at=(STRATEGY_POSTED_AT - timedelta(days=1)).replace(
                        tzinfo=None
                    ),
                )
            )
        session.add(
            MessageEvidenceVersion(
                raw_message_id=LATER_CANCEL_RAW_ID,
                version=1,
                input_fingerprint="chen-19493",
                model="mimo",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=1,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json=evidence_json,
            )
        )
        session.add(
            RecognitionDecision(
                raw_message_id=LATER_CANCEL_RAW_ID,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="识别失败",
                authoritative_payload_json="{}",
                agreement_status="authoritative_failed",
                differences_json="[]",
                prompt_versions_json="{}",
                automation_status="skipped",
                automation_reason=reason,
            )
        )
        session.add(
            MessageProcessingJob(
                raw_message_id=LATER_CANCEL_RAW_ID,
                chat_id=CHAT_ID,
                status=job_status,
                attempt_count=attempt_count,
                claim_token=_CLAIM_TOKEN if job_status == "claimed" else None,
                claimed_at=(
                    FIFTH_FAILURE_AT.replace(tzinfo=None)
                    if job_status == "claimed"
                    else None
                ),
                last_reason="processing_error:AuthoritativeProcessingFailed",
            )
        )
        session.commit()
        return session_factory, int(candidate.id)


def test_the_replayed_19490_is_released_because_it_came_first(tmp_path):
    """(a) 19490 precedes 19491 and names lifecycle 1327 exactly: released."""

    session_factory, candidate_id, _ = _chen_chat(
        tmp_path, job_status="failed", attempt_count=5
    )

    assert _assess(session_factory, candidate_id).status != "deferred"


@pytest.mark.parametrize(
    "reason", [MIMO_AUTHORITATIVE_FAILED, MIMO_AUTHORITATIVE_FAILED_EXHAUSTED]
)
def test_a_later_unreadable_cancellation_of_this_entry_keeps_blocking(
    tmp_path, reason
):
    """(b) Both exhausted forms: job ``failed``, or the rewritten reason."""

    session_factory, candidate_id = _entry_then_later_cancel(
        tmp_path, evidence_json=_later_cancel_evidence(), reason=reason
    )

    decision = _assess(session_factory, candidate_id)

    assert decision.status == "deferred"
    assert decision.blocking_raw_message_ids == (LATER_CANCEL_RAW_ID,)


def test_a_later_cancellation_naming_an_expired_lifecycle_keeps_blocking(tmp_path):
    """(e) An exact target to a same-symbol/side lifecycle that is EXPIRED.

    This is exactly the shape the reader-side guard's first cut missed: first
    pass readily pins a "取消，不进了" onto an old, already-``expired`` thread
    of the same symbol/side (陈哥's 19490 -> lifecycle 1327). An exact target
    that no longer exists as a live position proves nothing about which
    position the cancellation means, so it must not excuse releasing -- it
    falls through to the symbol/side check, which matches, so this still
    blocks.
    """

    session_factory, candidate_id = _entry_then_later_cancel(
        tmp_path,
        evidence_json=_later_cancel_evidence(
            target_lifecycle_id=1327,
            message_classes=[
                {
                    "class": "策略管理",
                    "target": {
                        "resolution": "exact",
                        "lifecycle_id": 1327,
                        "symbol": "BTC",
                        "side": "long",
                    },
                }
            ],
        ),
        other_lifecycle_id=1327,
        other_lifecycle_status="expired",
    )

    decision = _assess(session_factory, candidate_id)

    assert decision.status == "deferred"
    assert decision.blocking_raw_message_ids == (LATER_CANCEL_RAW_ID,)


def test_a_later_cancellation_naming_a_live_lifecycle_is_released(tmp_path):
    """(f) The same exact target, but the targeted lifecycle is still LIVE
    (``entered``): it really is some other, still-open position, so this
    releases.
    """

    session_factory, candidate_id = _entry_then_later_cancel(
        tmp_path,
        evidence_json=_later_cancel_evidence(
            target_lifecycle_id=1327,
            message_classes=[
                {
                    "class": "策略管理",
                    "target": {
                        "resolution": "exact",
                        "lifecycle_id": 1327,
                        "symbol": "BTC",
                        "side": "long",
                    },
                }
            ],
        ),
        other_lifecycle_id=1327,
        other_lifecycle_status="entered",
    )

    assert _assess(session_factory, candidate_id).status != "deferred"


def test_a_later_cancellation_naming_a_missing_lifecycle_keeps_blocking(tmp_path):
    """An exact target to an id that does not exist at all behaves like the
    expired case: it cannot prove "someone else's position", so it falls
    through to symbol/side, which matches here.
    """

    session_factory, candidate_id = _entry_then_later_cancel(
        tmp_path,
        evidence_json=_later_cancel_evidence(target_lifecycle_id=999999),
    )

    decision = _assess(session_factory, candidate_id)

    assert decision.status == "deferred"
    assert decision.blocking_raw_message_ids == (LATER_CANCEL_RAW_ID,)


def test_a_later_cancellation_naming_this_entrys_own_lifecycle_keeps_blocking(
    tmp_path,
):
    session_factory, candidate_id = _entry_then_later_cancel(
        tmp_path,
        evidence_json=_later_cancel_evidence(target_lifecycle_id=1400),
        own_lifecycle_id=1400,
    )

    assert _assess(session_factory, candidate_id).status == "deferred"


def test_a_later_exit_for_another_symbol_is_released(tmp_path):
    """(d) Risk-reducing, but for ETH while the entry is BTC."""

    session_factory, candidate_id = _entry_then_later_cancel(
        tmp_path,
        evidence_json=_later_cancel_evidence(
            symbol="ETHUSDT", side="long", event_type="exit_position"
        ),
    )

    assert _assess(session_factory, candidate_id).status != "deferred"


def test_a_later_exit_for_the_opposite_side_is_released(tmp_path):
    session_factory, candidate_id = _entry_then_later_cancel(
        tmp_path,
        evidence_json=_later_cancel_evidence(side="short", event_type="exit_position"),
    )

    assert _assess(session_factory, candidate_id).status != "deferred"


def test_a_later_cancellation_with_no_symbol_keeps_blocking(tmp_path):
    session_factory, candidate_id = _entry_then_later_cancel(
        tmp_path,
        evidence_json=_later_cancel_evidence(symbol=None, side=None),
    )

    assert _assess(session_factory, candidate_id).status == "deferred"


def test_a_management_action_alone_makes_it_risk_reducing(tmp_path):
    session_factory, candidate_id = _entry_then_later_cancel(
        tmp_path,
        evidence_json=_later_cancel_evidence(
            event_type="position_update", management_action="full_exit"
        ),
    )

    assert _assess(session_factory, candidate_id).status == "deferred"


def test_a_later_message_that_takes_no_risk_off_is_released(tmp_path):
    session_factory, candidate_id = _entry_then_later_cancel(
        tmp_path,
        evidence_json=_later_cancel_evidence(
            event_type="position_update", management_action="move_stop_to_break_even"
        ),
    )

    assert _assess(session_factory, candidate_id).status != "deferred"


def test_a_later_cancellation_still_being_retried_blocks_as_before(tmp_path):
    session_factory, candidate_id = _entry_then_later_cancel(
        tmp_path,
        evidence_json=_later_cancel_evidence(target_lifecycle_id=1327),
        job_status="pending",
    )

    assert _assess(session_factory, candidate_id).status == "deferred"


def test_the_worker_wakeup_does_not_release_what_the_guard_holds(
    tmp_path, monkeypatch
):
    """The completed-message wakeup skips re-assessment, so it must not be used
    to release an entry the later cancellation may target."""

    session_factory, candidate_id = _entry_then_later_cancel(
        tmp_path,
        evidence_json=_later_cancel_evidence(),
        job_status="claimed",
        attempt_count=4,
    )
    with session_factory() as session:
        job_id = session.query(MessageProcessingJob.id).scalar()
    assert _assess(
        session_factory, candidate_id, at=FIFTH_FAILURE_AT - timedelta(seconds=30)
    ).blocking_raw_message_ids == (LATER_CANCEL_RAW_ID,)
    executed = []

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=FIFTH_FAILURE_AT,
            job_processor=_authoritative_failure,
            loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
            entry_assembly_wakeup=_production_wakeup(
                session_factory, monkeypatch, executed
            ),
            _preclaimed_jobs=[
                MessageProcessingClaim(
                    job_id=job_id,
                    raw_message_id=LATER_CANCEL_RAW_ID,
                    chat_id=CHAT_ID,
                    attempt_count=4,
                    claim_token=_CLAIM_TOKEN,
                    source_reason="worker_claimed",
                )
            ],
        )
    )

    assert result.failed == 1
    assert executed == []
    with session_factory() as session:
        attempt = session.query(EntryAssemblyAttempt).one()
        assert attempt.status == "pending"
        assert json.loads(attempt.blocking_raw_message_ids_json) == [
            LATER_CANCEL_RAW_ID
        ]
    assert _decision_reason_for(session_factory, LATER_CANCEL_RAW_ID) == (
        MIMO_AUTHORITATIVE_FAILED_EXHAUSTED
    )
    # And the reconciler's re-assessment keeps holding it too.
    assert _assess(session_factory, candidate_id).status == "deferred"


def _decision_reason_for(session_factory, raw_message_id):
    with session_factory() as session:
        return (
            session.query(RecognitionDecision.automation_reason)
            .filter(RecognitionDecision.raw_message_id == raw_message_id)
            .scalar()
        )

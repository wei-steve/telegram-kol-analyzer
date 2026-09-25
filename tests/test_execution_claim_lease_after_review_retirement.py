"""The execution claim lease survives the semantic-review retirement intact.

``recognition_decisions.comparison_status`` and ``comparison_claim_token`` are
named after a comparison design that has not existed for a long time. What they
actually carry is the lease that decides **who may write to the exchange for
this message** -- fourteen modules read or write them. The semantic-disagreement
review used the same two columns for its own, separate lease, on a disjoint set
of status values, and was retired on 2026-09-25.

Deleting the review's writers while leaving the execution lease untouched is the
one thing in that retirement that could have cost real money, so this module
pins the lease end to end: claim, release, refusal, the uncertain freeze, and
every consumer that keys off an expired ``execution_*`` row. It is deliberately
separate from ``test_recognition_decisions`` and ``test_authoritative_execution_attempts``
so that a future edit to either cannot quietly take this coverage with it.
"""

from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research import authoritative_execution_attempts as attempts
from telegram_kol_research.authoritative_execution_schema import (
    apply_recognition_execution_schema,
    build_recognition_execution_schema_plan,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    MessageProcessingJob,
    RawMessage,
    RecognitionDecision,
)
from telegram_kol_research.recognition_execution_scanner import (
    scan_recognition_execution_cycle,
)
from telegram_kol_research.recognition_decisions import (
    AuthoritativeExecutionInProgress,
    RecognitionDecisionRecord,
    claim_authoritative_execution,
    finalize_authoritative_automation_outcome,
    save_pending_authoritative_decision,
    save_terminal_authoritative_decision,
)


NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)

#: The four values the lease moves through. ``pending`` / ``running`` /
#: ``failed`` used to sit in this same column for the review's own lease and are
#: written by nothing after the retirement.
LEASE_STATUSES = ("execution_pending", "execution_running", "execution_uncertain", "completed")


def _record(raw_message_id, payload=None):
    return RecognitionDecisionRecord(
        raw_message_id=raw_message_id,
        input_kind="text",
        authoritative_model="mimo-v2.5",
        authoritative_status="非策略",
        authoritative_payload=payload or {"recognition_result": "非策略"},
        auxiliary_model=None,
        auxiliary_status=None,
        auxiliary_payload=None,
        agreement_status="agreed",
        differences=[],
    )


def _session_factory(tmp_path, *, with_execution_schema=False):
    session_factory = create_session_factory(tmp_path / "lease.db")
    if with_execution_schema:
        plan = build_recognition_execution_schema_plan(session_factory.kw["bind"])
        apply_recognition_execution_schema(
            session_factory.kw["bind"], expected_plan_sha256=plan.plan_sha256
        )
    return session_factory


def _raw_message(session_factory, message_id=1):
    with session_factory() as session:
        raw = RawMessage(chat_id=-100, message_id=message_id, text="BTC short")
        session.add(raw)
        session.commit()
        return int(raw.id)


def _row(session_factory):
    with session_factory() as session:
        return session.query(RecognitionDecision).one()


def _owner():
    return attempts.ExecutionOwnerIdentity(
        runtime_role="worker",
        instance_id="instance",
        pid=4242,
        boot_id="boot",
        process_start_ticks="7",
        systemd_invocation_id="invocation",
    )


def test_persisting_authority_opens_the_lease_with_its_own_generation(tmp_path):
    session_factory = _session_factory(tmp_path)
    raw_id = _raw_message(session_factory)

    saved = save_pending_authoritative_decision(session_factory, _record(raw_id))

    assert saved.comparison_status == "execution_pending"
    assert saved.comparison_claim_token
    assert saved.comparison_started_at is None
    assert saved.comparison_next_attempt_at is None


def test_only_the_exact_generation_claims_the_lease(tmp_path):
    session_factory = _session_factory(tmp_path)
    raw_id = _raw_message(session_factory)
    saved = save_pending_authoritative_decision(session_factory, _record(raw_id))

    assert (
        claim_authoritative_execution(
            session_factory,
            raw_message_id=raw_id,
            authoritative_generation="not-the-generation",
        )
        is False
    )
    assert _row(session_factory).comparison_status == "execution_pending"

    assert claim_authoritative_execution(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=saved.comparison_claim_token,
    )
    held = _row(session_factory)
    assert held.comparison_status == "execution_running"
    assert held.comparison_claim_token == saved.comparison_claim_token

    # A second claim of the same generation finds no ``execution_pending`` row.
    assert (
        claim_authoritative_execution(
            session_factory,
            raw_message_id=raw_id,
            authoritative_generation=saved.comparison_claim_token,
        )
        is False
    )


def test_finalizing_releases_the_lease_and_only_for_the_holder(tmp_path):
    session_factory = _session_factory(tmp_path)
    raw_id = _raw_message(session_factory)
    saved = save_pending_authoritative_decision(session_factory, _record(raw_id))
    assert claim_authoritative_execution(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=saved.comparison_claim_token,
    )

    with pytest.raises(RuntimeError, match="stale"):
        finalize_authoritative_automation_outcome(
            session_factory,
            raw_message_id=raw_id,
            authoritative_generation="somebody-else",
            automation_status="submitted",
            automation_reason="wrong_holder",
        )
    assert _row(session_factory).comparison_status == "execution_running"

    finalized = finalize_authoritative_automation_outcome(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=saved.comparison_claim_token,
        automation_status="submitted",
        automation_reason="close_position",
    )
    assert finalized.comparison_status == "completed"
    assert finalized.comparison_claim_token is None
    assert finalized.comparison_started_at is None
    assert finalized.comparison_next_attempt_at is None
    assert finalized.automation_status == "submitted"


@pytest.mark.parametrize("held_status", ["execution_running", "execution_uncertain"])
def test_a_held_or_frozen_lease_refuses_to_be_overwritten(tmp_path, held_status):
    session_factory = _session_factory(tmp_path)
    raw_id = _raw_message(session_factory)
    saved = save_pending_authoritative_decision(session_factory, _record(raw_id))
    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        row.comparison_status = held_status
        session.commit()

    with pytest.raises(AuthoritativeExecutionInProgress) as pending_exc:
        save_pending_authoritative_decision(
            session_factory,
            _record(raw_id, {"recognition_result": "策略"}),
        )
    assert pending_exc.value.comparison_status == held_status

    with pytest.raises(AuthoritativeExecutionInProgress) as terminal_exc:
        save_terminal_authoritative_decision(
            session_factory,
            _record(raw_id, {"recognition_result": "策略"}),
        )
    assert terminal_exc.value.comparison_status == held_status

    still_held = _row(session_factory)
    assert still_held.comparison_status == held_status
    assert still_held.comparison_claim_token == saved.comparison_claim_token


def test_an_attempt_that_starts_a_side_effect_and_loses_its_answer_freezes_the_lease(
    tmp_path,
):
    session_factory = _session_factory(tmp_path, with_execution_schema=True)
    raw_id = _raw_message(session_factory)
    saved = save_pending_authoritative_decision(session_factory, _record(raw_id))

    claim = attempts.claim_authoritative_execution_attempt(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=str(saved.comparison_claim_token),
        owner=_owner(),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    assert _row(session_factory).comparison_status == "execution_running"

    assert attempts.mark_authoritative_side_effect_started(
        session_factory,
        attempt_id=claim.attempt_id,
        raw_message_id=raw_id,
        authoritative_generation=str(saved.comparison_claim_token),
        claim_token=claim.claim_token,
        started_at=NOW,
    )
    assert attempts.mark_authoritative_execution_uncertain(
        session_factory,
        attempt_id=claim.attempt_id,
        claim_token=claim.claim_token,
        uncertain_at=NOW,
        error_class="TimeoutError",
        error_summary="no answer",
        evidence_refs=[{"kind": "deepcoin_write", "id": "order-1"}],
    )

    frozen = _row(session_factory)
    assert frozen.comparison_status == "execution_uncertain"
    # The freeze keeps the token: nothing may re-open the lease by guessing it.
    assert frozen.comparison_claim_token == saved.comparison_claim_token


def test_finalizing_a_recorded_attempt_releases_the_lease_without_a_review_flag(
    tmp_path,
):
    session_factory = _session_factory(tmp_path, with_execution_schema=True)
    raw_id = _raw_message(session_factory)
    saved = save_pending_authoritative_decision(session_factory, _record(raw_id))

    claim = attempts.claim_authoritative_execution_attempt(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=str(saved.comparison_claim_token),
        owner=_owner(),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    assert attempts.mark_authoritative_side_effect_started(
        session_factory,
        attempt_id=claim.attempt_id,
        raw_message_id=raw_id,
        authoritative_generation=str(saved.comparison_claim_token),
        claim_token=claim.claim_token,
        started_at=NOW,
    )
    assert attempts.record_authoritative_automation_outcome(
        session_factory,
        attempt_id=claim.attempt_id,
        claim_token=claim.claim_token,
        automation_status="submitted",
        automation_reason="entry_submitted",
        exchange_effect="confirmed_applied",
        evidence_refs=[{"kind": "trade_signal", "id": 7}],
        recorded_at=NOW,
    )

    finalized = attempts.finalize_recorded_authoritative_execution(
        session_factory,
        attempt_id=claim.attempt_id,
        claim_token=claim.claim_token,
        finalized_at=NOW,
    )

    assert finalized.comparison_status == "completed"
    assert finalized.comparison_claim_token is None
    assert finalized.comparison_started_at is None
    assert finalized.agreement_status == "review_disabled"


@pytest.mark.parametrize("held_status", ["execution_running", "execution_uncertain"])
def test_the_expiry_scanner_still_finds_a_lease_left_behind(tmp_path, held_status):
    """A decision stuck holding the lease is still reported, not swept silently.

    This is the consumer that expires a lease in production: the job finished,
    the decision row is still ``execution_*``, and nobody owns it any more.
    """

    session_factory = _session_factory(tmp_path, with_execution_schema=True)
    with session_factory() as session:
        raw = RawMessage(chat_id=-100, message_id=77, text="BTC short")
        session.add(raw)
        session.flush()
        session.add(
            MessageProcessingJob(
                raw_message_id=raw.id,
                chat_id=-100,
                status="succeeded",
                attempt_count=1,
                completed_at=NOW,
                enqueued_at=NOW,
            )
        )
        session.add(
            RecognitionDecision(
                raw_message_id=raw.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="\u975e\u7b56\u7565",
                authoritative_payload_json='{"recognition_result":"x"}',
                agreement_status="pending",
                differences_json="[]",
                prompt_versions_json="{}",
                comparison_status=held_status,
                comparison_claim_token="generation-77",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
        raw_id = int(raw.id)

    findings = scan_recognition_execution_cycle(
        session_factory,
        runtime_role="worker",
        now=NOW,
    )

    stuck = [
        item
        for item in findings
        if item.family == "succeeded_job_running_decision" and item.row_id == raw_id
    ]
    assert stuck, findings


def test_the_retired_review_statuses_are_written_by_nobody(tmp_path):
    """No path leaves a row in a status only the retired review could clear.

    ``pending`` / ``running`` / ``failed`` were the review's lease values in
    this same column. A row parked in one of them after the retirement would be
    a row no loop will ever pick up again.
    """

    session_factory = _session_factory(tmp_path)
    raw_id = _raw_message(session_factory)

    saved = save_pending_authoritative_decision(session_factory, _record(raw_id))
    assert _row(session_factory).comparison_status in LEASE_STATUSES

    assert claim_authoritative_execution(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=saved.comparison_claim_token,
    )
    assert _row(session_factory).comparison_status in LEASE_STATUSES

    finalize_authoritative_automation_outcome(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=saved.comparison_claim_token,
        automation_status="skipped",
        automation_reason="not_a_strategy",
    )
    assert _row(session_factory).comparison_status == "completed"

    save_terminal_authoritative_decision(
        session_factory,
        _record(raw_id, {"recognition_result": "识别失败"}),
    )
    assert _row(session_factory).comparison_status == "completed"

import importlib
import importlib.util
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    save_pending_authoritative_decision,
)


NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)


def _attempts_module():
    name = "telegram_kol_research.authoritative_execution_attempts"
    assert importlib.util.find_spec(name) is not None, "attempt persistence module is missing"
    return importlib.import_module(name)


def _schema_module():
    name = "telegram_kol_research.authoritative_execution_schema"
    assert importlib.util.find_spec(name) is not None, "explicit schema module is missing"
    return importlib.import_module(name)


def _prepared(tmp_path):
    session_factory = create_session_factory(tmp_path / "attempts.db")
    schema = _schema_module()
    plan = schema.build_recognition_execution_schema_plan(session_factory.kw["bind"])
    schema.apply_recognition_execution_schema(
        session_factory.kw["bind"],
        expected_plan_sha256=plan.plan_sha256,
    )
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=1, text="message")
        session.add(raw)
        session.commit()
        raw_id = int(raw.id)
    decision = save_pending_authoritative_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_id,
            input_kind="text",
            authoritative_model="mimo-v2.5",
            authoritative_status="非策略",
            authoritative_payload={"recognition_result": "非策略"},
            auxiliary_model=None,
            auxiliary_status=None,
            auxiliary_payload=None,
            agreement_status="agreed",
            differences=[],
        ),
    )
    return session_factory, raw_id, str(decision.comparison_claim_token)


def _owner(attempts):
    return attempts.ExecutionOwnerIdentity(
        runtime_role="worker",
        instance_id="instance-a",
        pid=123,
        boot_id="boot-a",
        process_start_ticks="456",
        systemd_invocation_id="invocation-a",
    )


def test_exact_token_attempt_state_machine_fails_closed(tmp_path):
    attempts = _attempts_module()
    session_factory, raw_id, generation = _prepared(tmp_path)
    claim = attempts.claim_authoritative_execution_attempt(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=generation,
        owner=_owner(attempts),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=2),
    )

    assert attempts.mark_authoritative_side_effect_started(
        session_factory,
        attempt_id=claim.attempt_id,
        raw_message_id=raw_id,
        authoritative_generation=generation,
        claim_token="stale-token",
        started_at=NOW,
    ) is False
    assert attempts.mark_authoritative_side_effect_started(
        session_factory,
        attempt_id=claim.attempt_id,
        raw_message_id=raw_id,
        authoritative_generation="stale-generation",
        claim_token=claim.claim_token,
        started_at=NOW,
    ) is False
    assert attempts.mark_authoritative_side_effect_started(
        session_factory,
        attempt_id=claim.attempt_id,
        raw_message_id=raw_id,
        authoritative_generation=generation,
        claim_token=claim.claim_token,
        started_at=NOW,
    ) is True
    assert attempts.fail_safe_authoritative_execution_attempt(
        session_factory,
        attempt_id=claim.attempt_id,
        claim_token=claim.claim_token,
        failed_at=NOW,
        error_class="RuntimeError",
        error_summary="must not clear after boundary",
    ) is False
    with pytest.raises(ValueError, match="outcome_unknown"):
        attempts.record_authoritative_automation_outcome(
            session_factory,
            attempt_id=claim.attempt_id,
            claim_token=claim.claim_token,
            automation_status="unknown",
            automation_reason="venue_timeout",
            exchange_effect="outcome_unknown",
            evidence_refs=[],
            recorded_at=NOW,
        )
    assert attempts.mark_authoritative_execution_uncertain(
        session_factory,
        attempt_id=claim.attempt_id,
        claim_token=claim.claim_token,
        uncertain_at=NOW,
        error_class="ProcessLost",
        error_summary="outcome unknown",
    ) is True
    snapshot = attempts.load_authoritative_execution_attempt(
        session_factory,
        attempt_id=claim.attempt_id,
    )
    assert snapshot.status == "uncertain"
    assert snapshot.side_effect_started_at is not None
    assert snapshot.exchange_effect == "outcome_unknown"


def test_claim_and_attempt_insert_are_one_transaction_and_generation_is_unique(tmp_path):
    attempts = _attempts_module()
    session_factory, raw_id, generation = _prepared(tmp_path)
    first = attempts.claim_authoritative_execution_attempt(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=generation,
        owner=_owner(attempts),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=2),
    )
    assert first.attempt_id > 0

    with pytest.raises(RuntimeError, match="authoritative execution claim failed"):
        attempts.claim_authoritative_execution_attempt(
            session_factory,
            raw_message_id=raw_id,
            authoritative_generation=generation,
            owner=_owner(attempts),
            claimed_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=2),
        )


def test_outcome_recorded_is_finalize_only_and_never_replays_adapter(tmp_path):
    attempts = _attempts_module()
    session_factory, raw_id, generation = _prepared(tmp_path)
    claim = attempts.claim_authoritative_execution_attempt(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=generation,
        owner=_owner(attempts),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=2),
    )
    assert attempts.mark_authoritative_side_effect_started(
        session_factory,
        attempt_id=claim.attempt_id,
        raw_message_id=raw_id,
        authoritative_generation=generation,
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
    adapter_calls = []

    finalized = attempts.finalize_recorded_authoritative_execution(
        session_factory,
        attempt_id=claim.attempt_id,
        claim_token=claim.claim_token,
        semantic_review_enabled=False,
        finalized_at=NOW,
        adapter=lambda: adapter_calls.append("called"),
    )

    assert finalized.comparison_status == "completed"
    assert adapter_calls == []
    assert attempts.load_authoritative_execution_attempt(
        session_factory, attempt_id=claim.attempt_id
    ).status == "succeeded"

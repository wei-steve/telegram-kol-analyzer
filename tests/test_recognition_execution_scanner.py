from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_kol_research.authoritative_execution_schema import (
    apply_recognition_execution_schema,
    build_recognition_execution_schema_plan,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    AuthoritativeExecutionAttempt,
    EntryAssemblyAttempt,
    MessageProcessingJob,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
)
from telegram_kol_research.recognition_execution_scanner import (
    RecognitionExecutionFinding,
    owner_identity_is_alive,
    scan_recognition_execution_cycle,
)
from telegram_kol_research.authoritative_execution_attempts import (
    ExecutionOwnerIdentity,
    claim_authoritative_execution_attempt,
    mark_authoritative_side_effect_started,
    record_authoritative_automation_outcome,
)
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    save_pending_authoritative_decision,
)


NOW = datetime(2026, 9, 2, 19, tzinfo=UTC)


def test_missing_owner_identity_evidence_is_unknown_not_dead(monkeypatch):
    row = type(
        "Attempt",
        (),
        {
            "owner_boot_id": "unavailable",
            "owner_pid": 999999,
            "owner_process_start_ticks": "unavailable",
        },
    )()
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, **_kwargs: "current-boot",
    )

    assert owner_identity_is_alive(row) is None


def _prepared(tmp_path):
    session_factory = create_session_factory(tmp_path / "scanner.db")
    engine = session_factory.kw["bind"]
    plan = build_recognition_execution_schema_plan(engine)
    apply_recognition_execution_schema(engine, expected_plan_sha256=plan.plan_sha256)
    return session_factory


def _add_stuck(session_factory, message_id, *, status="execution_running"):
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=message_id, text="message")
        session.add(raw)
        session.flush()
        session.add(
            MessageProcessingJob(
                raw_message_id=raw.id,
                chat_id=1,
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
                authoritative_model="mimo",
                authoritative_status="非策略",
                authoritative_payload_json='{"recognition_result":"非策略"}',
                agreement_status="pending",
                differences_json="[]",
                prompt_versions_json="{}",
                comparison_status=status,
                comparison_claim_token=f"generation-{message_id}",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
        return int(raw.id)


def test_succeeded_job_running_decision_is_detected_in_one_scan_cycle(tmp_path):
    session_factory = _prepared(tmp_path)
    raw_id = _add_stuck(session_factory, 1)
    captured = []

    findings = scan_recognition_execution_cycle(
        session_factory,
        runtime_role="worker",
        now=NOW,
        incident_sink=captured.append,
    )

    assert any(
        item.family == "succeeded_job_running_decision"
        and item.raw_message_id == raw_id
        for item in findings
    )
    assert captured == list(findings)


def test_later_scan_family_failure_does_not_drop_earlier_incident(
    tmp_path, monkeypatch
):
    session_factory = _prepared(tmp_path)
    first = RecognitionExecutionFinding(
        family="succeeded_job_running_decision",
        row_id=7,
        raw_message_id=7,
        phase="execution_running",
        fingerprint="f" * 64,
        action="observe_only",
    )
    calls = []

    def fake_scan_family(*_args, family, **_kwargs):
        calls.append(family)
        if family == "succeeded_job_running_decision":
            return [first]
        if family == "legacy_running_decision":
            raise RuntimeError("query unavailable")
        return []

    monkeypatch.setattr(
        "telegram_kol_research.recognition_execution_scanner._scan_family",
        fake_scan_family,
    )
    captured = []

    findings = scan_recognition_execution_cycle(
        session_factory,
        runtime_role="worker",
        now=NOW,
        incident_sink=captured.append,
    )

    assert captured[0] == first
    assert first in findings
    assert any(item.action == "family_scan_raised" for item in findings)
    assert len(calls) == 5


def test_succeeded_job_uncertain_decision_is_detected_in_one_scan_cycle(tmp_path):
    session_factory = _prepared(tmp_path)
    raw_id = _add_stuck(
        session_factory, 1, status="execution_uncertain"
    )

    findings = scan_recognition_execution_cycle(
        session_factory,
        runtime_role="worker",
        now=NOW,
    )

    assert any(
        item.family == "succeeded_job_running_decision"
        and item.raw_message_id == raw_id
        and item.phase == "execution_uncertain"
        for item in findings
    )


def test_legacy_claimed_wakeup_parent_without_child_is_observation_only(tmp_path):
    session_factory = _prepared(tmp_path)
    with session_factory() as session:
        strategy = RawMessage(chat_id=1, message_id=71, text="strategy")
        trigger = RawMessage(chat_id=1, message_id=72, text="trigger")
        session.add_all([strategy, trigger])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=strategy.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            confidence=0.9,
        )
        session.add(candidate)
        session.flush()
        parent = EntryAssemblyAttempt(
            strategy_raw_message_id=strategy.id,
            signal_candidate_id=candidate.id,
            candidate_generation="legacy-generation",
            cutoff_posted_at=NOW,
            cutoff_message_id=72,
            cutoff_raw_message_id=trigger.id,
            blocking_raw_message_ids_json="[]",
            status="claimed",
            fingerprint="legacy-claimed-parent",
            wake_claim_token="legacy-token",
            wake_claimed_at=NOW - timedelta(minutes=10),
            created_at=NOW - timedelta(minutes=10),
            updated_at=NOW - timedelta(minutes=10),
        )
        session.add(parent)
        session.commit()
        parent_id = int(parent.id)

    findings = scan_recognition_execution_cycle(
        session_factory,
        runtime_role="worker",
        now=NOW,
    )

    assert any(
        item.family == "legacy_claimed_wakeup_parent"
        and item.row_id == parent_id
        and item.action == "observe_only"
        for item in findings
    )
    with session_factory() as session:
        assert session.get(EntryAssemblyAttempt, parent_id).status == "claimed"


def test_durable_keyset_cursor_reaches_rows_beyond_limit(tmp_path):
    session_factory = _prepared(tmp_path)
    raw_ids = [_add_stuck(session_factory, value) for value in range(1, 5)]
    seen = set()

    for _ in range(3):
        findings = scan_recognition_execution_cycle(
            session_factory,
            runtime_role="worker",
            now=NOW,
            limit=2,
        )
        seen.update(
            item.raw_message_id
            for item in findings
            if item.family == "succeeded_job_running_decision"
        )

    assert set(raw_ids).issubset(seen)


def test_web_role_does_not_scan_or_advance_cursor(tmp_path):
    session_factory = _prepared(tmp_path)
    _add_stuck(session_factory, 1)

    assert scan_recognition_execution_cycle(
        session_factory, runtime_role="web", now=NOW
    ) == ()


def _add_expired_attempt(session_factory, *, after_boundary):
    with session_factory() as session:
        raw = RawMessage(chat_id=9, message_id=99, text="attempt")
        session.add(raw)
        session.commit()
        raw_id = int(raw.id)
    decision = save_pending_authoritative_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_id,
            input_kind="text",
            authoritative_model="mimo",
            authoritative_status="非策略",
            authoritative_payload={"recognition_result": "非策略"},
            auxiliary_model=None,
            auxiliary_status=None,
            auxiliary_payload=None,
            agreement_status="pending",
            differences=[],
        ),
    )
    claim = claim_authoritative_execution_attempt(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=decision.comparison_claim_token,
        owner=ExecutionOwnerIdentity("worker", "dead", 999, "boot", "1"),
        claimed_at=NOW - timedelta(minutes=3),
        lease_expires_at=NOW - timedelta(minutes=1),
    )
    if after_boundary:
        assert mark_authoritative_side_effect_started(
            session_factory,
            attempt_id=claim.attempt_id,
            raw_message_id=raw_id,
            authoritative_generation=decision.comparison_claim_token,
            claim_token=claim.claim_token,
            started_at=NOW - timedelta(minutes=2),
        )
    return raw_id


def test_expired_dead_preboundary_attempt_terminalizes_failed_safe(tmp_path):
    session_factory = _prepared(tmp_path)
    raw_id = _add_expired_attempt(session_factory, after_boundary=False)
    findings = scan_recognition_execution_cycle(
        session_factory,
        runtime_role="worker",
        now=NOW,
        owner_liveness=lambda _: False,
    )
    assert any(item.action == "failed_safe" for item in findings)
    with session_factory() as session:
        assert session.query(RecognitionDecision).filter_by(
            raw_message_id=raw_id
        ).one().comparison_status == "completed"


def test_live_unexpired_attempt_is_not_reported_as_an_orphan(tmp_path):
    session_factory = _prepared(tmp_path)
    with session_factory() as session:
        raw = RawMessage(chat_id=9, message_id=98, text="active")
        session.add(raw)
        session.commit()
        raw_id = int(raw.id)
    decision = save_pending_authoritative_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_id,
            input_kind="text",
            authoritative_model="mimo",
            authoritative_status="非策略",
            authoritative_payload={"recognition_result": "非策略"},
            auxiliary_model=None,
            auxiliary_status=None,
            auxiliary_payload=None,
            agreement_status="pending",
            differences=[],
        ),
    )
    claim = claim_authoritative_execution_attempt(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=decision.comparison_claim_token,
        owner=ExecutionOwnerIdentity("worker", "alive", 123, "boot", "1"),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=2),
    )

    findings = scan_recognition_execution_cycle(
        session_factory,
        runtime_role="worker",
        now=NOW,
        owner_liveness=lambda _: True,
    )

    assert not any(
        item.family == "active_authoritative_attempt"
        and item.row_id == claim.attempt_id
        for item in findings
    )


def test_succeeded_prior_job_does_not_make_live_reanalysis_an_orphan(tmp_path):
    session_factory = _prepared(tmp_path)
    with session_factory() as session:
        raw = RawMessage(chat_id=9, message_id=97, text="active reanalysis")
        session.add(raw)
        session.flush()
        raw_id = int(raw.id)
        session.add(
            MessageProcessingJob(
                raw_message_id=raw_id,
                chat_id=9,
                status="succeeded",
                attempt_count=1,
                enqueued_at=NOW - timedelta(minutes=10),
                completed_at=NOW - timedelta(minutes=9),
            )
        )
        session.commit()
    decision = save_pending_authoritative_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_id,
            input_kind="text",
            authoritative_model="mimo",
            authoritative_status="非策略",
            authoritative_payload={"recognition_result": "非策略"},
            auxiliary_model=None,
            auxiliary_status=None,
            auxiliary_payload=None,
            agreement_status="pending",
            differences=[],
        ),
    )
    claim_authoritative_execution_attempt(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=decision.comparison_claim_token,
        owner=ExecutionOwnerIdentity("worker", "alive", 123, "boot", "1"),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=2),
    )

    findings = scan_recognition_execution_cycle(
        session_factory,
        runtime_role="worker",
        now=NOW,
        owner_liveness=lambda _: True,
    )

    assert not any(
        item.family == "succeeded_job_running_decision"
        and item.raw_message_id == raw_id
        for item in findings
    )


def test_expired_dead_postboundary_attempt_freezes_uncertain(tmp_path):
    session_factory = _prepared(tmp_path)
    raw_id = _add_expired_attempt(session_factory, after_boundary=True)
    findings = scan_recognition_execution_cycle(
        session_factory,
        runtime_role="worker",
        now=NOW,
        owner_liveness=lambda _: False,
    )
    assert any(item.action == "marked_uncertain" for item in findings)
    with session_factory() as session:
        assert session.query(RecognitionDecision).filter_by(
            raw_message_id=raw_id
        ).one().comparison_status == "execution_uncertain"


def _add_recorded_attempt(session_factory, message_id):
    with session_factory() as session:
        raw = RawMessage(chat_id=9, message_id=message_id, text="recorded")
        session.add(raw)
        session.commit()
        raw_id = int(raw.id)
    decision = save_pending_authoritative_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_id,
            input_kind="text",
            authoritative_model="mimo",
            authoritative_status="非策略",
            authoritative_payload={"recognition_result": "非策略"},
            auxiliary_model=None,
            auxiliary_status=None,
            auxiliary_payload=None,
            agreement_status="pending",
            differences=[],
        ),
    )
    claim = claim_authoritative_execution_attempt(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=decision.comparison_claim_token,
        owner=ExecutionOwnerIdentity("worker", "dead", 999, "boot", "1"),
        claimed_at=NOW - timedelta(minutes=3),
        lease_expires_at=NOW - timedelta(minutes=1),
    )
    assert mark_authoritative_side_effect_started(
        session_factory,
        attempt_id=claim.attempt_id,
        raw_message_id=raw_id,
        authoritative_generation=decision.comparison_claim_token,
        claim_token=claim.claim_token,
        started_at=NOW - timedelta(minutes=2),
    )
    assert record_authoritative_automation_outcome(
        session_factory,
        attempt_id=claim.attempt_id,
        claim_token=claim.claim_token,
        automation_status="submitted",
        automation_reason="entry_submitted",
        exchange_effect="confirmed_applied",
        evidence_refs=[{"kind": "deepcoin_write", "ordinal": 1}],
        recorded_at=NOW - timedelta(minutes=1),
    )
    return claim.attempt_id


def test_finalize_failure_is_reported_and_does_not_starve_later_rows(
    tmp_path, monkeypatch
):
    session_factory = _prepared(tmp_path)
    first_id = _add_recorded_attempt(session_factory, 201)
    second_id = _add_recorded_attempt(session_factory, 202)
    monkeypatch.setattr(
        "telegram_kol_research.recognition_execution_scanner.finalize_recorded_authoritative_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("finalize persistence unavailable")
        ),
    )

    findings = scan_recognition_execution_cycle(
        session_factory,
        runtime_role="worker",
        now=NOW,
        limit=10,
        owner_liveness=lambda _: False,
    )

    failed_ids = {
        item.row_id
        for item in findings
        if item.family == "active_authoritative_attempt"
        and item.action == "finalize_raised"
    }
    assert failed_ids == {first_id, second_id}


def test_live_owner_keeps_unexpired_recorded_outcome_for_its_own_finalize(tmp_path):
    session_factory = _prepared(tmp_path)
    attempt_id = _add_recorded_attempt(session_factory, 203)
    with session_factory() as session:
        attempt = session.get(AuthoritativeExecutionAttempt, attempt_id)
        attempt.owner_instance_id = "alive"
        attempt.owner_pid = 123
        attempt.lease_expires_at = NOW + timedelta(minutes=2)
        session.commit()

    findings = scan_recognition_execution_cycle(
        session_factory,
        runtime_role="worker",
        now=NOW,
        owner_liveness=lambda _: True,
    )

    assert not any(
        item.family == "active_authoritative_attempt"
        and item.row_id == attempt_id
        for item in findings
    )
    with session_factory() as session:
        assert session.get(AuthoritativeExecutionAttempt, attempt_id).status == (
            "outcome_recorded"
        )

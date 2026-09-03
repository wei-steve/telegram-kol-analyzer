from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.authoritative_execution_attempts import (
    ExecutionOwnerIdentity,
)
from telegram_kol_research.authoritative_recognition import (
    _run_entry_assembly_wakeups,
)
from telegram_kol_research.authoritative_execution_schema import (
    apply_recognition_execution_schema,
    build_recognition_execution_schema_plan,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_assembly_admission import (
    claim_ready_entry_assembly_wakeups,
)
from telegram_kol_research.entry_assembly_wakeup_executions import (
    mark_wakeup_side_effect_started,
    record_wakeup_outcome,
    run_claimed_entry_assembly_wakeup,
)
from telegram_kol_research.execution_boundary import ExecutionBoundaryOutcome
from telegram_kol_research.models import (
    EntryAssemblyAttempt,
    EntryAssemblyWakeupExecution,
    RawMessage,
    SignalCandidate,
)
from telegram_kol_research.recognition_execution_runtime import (
    RecognitionExecutionRegistry,
)


NOW = datetime(2026, 9, 2, 18, tzinfo=UTC)


def _owner():
    return ExecutionOwnerIdentity("worker", "instance", 123, "boot", "456")


def _prepared(tmp_path):
    session_factory = create_session_factory(tmp_path / "wakeup.db")
    engine = session_factory.kw["bind"]
    plan = build_recognition_execution_schema_plan(engine)
    apply_recognition_execution_schema(engine, expected_plan_sha256=plan.plan_sha256)
    with session_factory() as session:
        strategy = RawMessage(chat_id=1, message_id=1, text="strategy")
        trigger = RawMessage(chat_id=1, message_id=2, text="trigger")
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
            candidate_generation="generation",
            cutoff_posted_at=NOW,
            cutoff_message_id=2,
            cutoff_raw_message_id=trigger.id,
            blocking_raw_message_ids_json=f"[{trigger.id}]",
            status="pending",
            fingerprint="fingerprint",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(parent)
        session.commit()
        return session_factory, int(parent.id), int(trigger.id)


def test_owner_claim_atomically_creates_independent_child_fence(tmp_path):
    session_factory, parent_id, trigger_id = _prepared(tmp_path)

    claims = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=trigger_id,
        now=NOW,
        execution_owner=_owner(),
    )

    assert len(claims) == 1
    assert claims[0].child_execution_id is not None
    with session_factory() as session:
        parent = session.get(EntryAssemblyAttempt, parent_id)
        child = session.query(EntryAssemblyWakeupExecution).one()
        assert parent.status == "claimed"
        assert child.status == "claimed"
        assert child.claim_token == parent.wake_claim_token
        assert child.strategy_raw_message_id == claims[0].strategy_raw_message_id
        assert child.trigger_raw_message_id == trigger_id


@pytest.mark.parametrize("blockers_json", ("[]", "not-json"))
def test_empty_or_malformed_blockers_fail_closed_without_unrelated_wakeup(
    tmp_path, blockers_json
):
    session_factory, parent_id, _trigger_id = _prepared(tmp_path)
    with session_factory() as session:
        parent = session.get(EntryAssemblyAttempt, parent_id)
        parent.blocking_raw_message_ids_json = blockers_json
        session.commit()

    assert claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=99999,
        now=NOW,
        execution_owner=_owner(),
    ) == ()
    with session_factory() as session:
        assert session.get(EntryAssemblyAttempt, parent_id).status == "pending"
        assert session.query(EntryAssemblyWakeupExecution).count() == 0


def test_wakeup_claim_never_preclaims_a_batch_ahead_of_adapter_admission(tmp_path):
    session_factory, parent_id, trigger_id = _prepared(tmp_path)
    with session_factory() as session:
        first = session.get(EntryAssemblyAttempt, parent_id)
        second_strategy = RawMessage(chat_id=2, message_id=3, text="second strategy")
        session.add(second_strategy)
        session.flush()
        second_candidate = SignalCandidate(
            raw_message_id=second_strategy.id,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            confidence=0.9,
        )
        session.add(second_candidate)
        session.flush()
        session.add(
            EntryAssemblyAttempt(
                strategy_raw_message_id=second_strategy.id,
                signal_candidate_id=second_candidate.id,
                candidate_generation="generation-2",
                cutoff_posted_at=NOW,
                cutoff_message_id=2,
                cutoff_raw_message_id=trigger_id,
                blocking_raw_message_ids_json=f"[{trigger_id}]",
                status="pending",
                fingerprint="fingerprint-2",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        assert first.status == "pending"
        session.commit()

    claims = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=trigger_id,
        now=NOW,
        limit=20,
        execution_owner=_owner(),
    )

    assert len(claims) == 1
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).filter_by(status="claimed").count() == 1
        assert session.query(EntryAssemblyAttempt).filter_by(status="pending").count() == 1


def test_wakeup_runner_consumes_all_matching_parents_one_fenced_child_at_a_time(
    tmp_path,
):
    session_factory, parent_id, trigger_id = _prepared(tmp_path)
    with session_factory() as session:
        second_strategy = RawMessage(chat_id=2, message_id=3, text="second strategy")
        session.add(second_strategy)
        session.flush()
        second_candidate = SignalCandidate(
            raw_message_id=second_strategy.id,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            confidence=0.9,
        )
        session.add(second_candidate)
        session.flush()
        session.add(
            EntryAssemblyAttempt(
                strategy_raw_message_id=second_strategy.id,
                signal_candidate_id=second_candidate.id,
                candidate_generation="generation-2",
                cutoff_posted_at=NOW,
                cutoff_message_id=2,
                cutoff_raw_message_id=trigger_id,
                blocking_raw_message_ids_json=f"[{trigger_id}]",
                status="pending",
                fingerprint="fingerprint-2",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    calls = []

    def executor(raw_message_id):
        with session_factory() as session:
            active = (
                session.query(EntryAssemblyWakeupExecution)
                .filter(
                    EntryAssemblyWakeupExecution.status.in_(
                        ("claimed", "executing", "outcome_recorded")
                    )
                )
                .count()
            )
        calls.append((raw_message_id, active))
        return ExecutionBoundaryOutcome(
            status="completed",
            exchange_effect="not_started",
            raw_status="blocked",
            reason_code="policy",
            evidence_refs=(),
            public_result={"status": "blocked", "reason": "policy"},
        )

    _run_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=trigger_id,
        auto_trade_executor=executor,
        execution_owner=_owner(),
        execution_registry=RecognitionExecutionRegistry(),
    )

    assert len(calls) == 2
    assert all(active == 1 for _, active in calls)
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).filter_by(status="woken").count() == 2
        assert session.query(EntryAssemblyWakeupExecution).filter_by(status="succeeded").count() == 2


def test_child_boundary_cas_requires_parent_generation_and_token(tmp_path):
    session_factory, parent_id, trigger_id = _prepared(tmp_path)
    claim = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=trigger_id,
        now=NOW,
        execution_owner=_owner(),
    )[0]

    assert mark_wakeup_side_effect_started(
        session_factory,
        child_execution_id=claim.child_execution_id,
        entry_assembly_attempt_id=parent_id,
        wake_generation=claim.wake_generation + 1,
        strategy_raw_message_id=claim.strategy_raw_message_id,
        trigger_raw_message_id=claim.trigger_raw_message_id,
        claim_token=claim.claim_token,
        started_at=NOW,
    ) is False


def test_confirmed_wakeup_outcome_requires_committed_side_effect_boundary(tmp_path):
    session_factory, parent_id, trigger_id = _prepared(tmp_path)
    claim = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=trigger_id,
        now=NOW,
        execution_owner=_owner(),
    )[0]
    outcome = ExecutionBoundaryOutcome(
        status="completed",
        exchange_effect="confirmed_applied",
        raw_status="submitted",
        reason_code="entry_submitted",
        evidence_refs=({"kind": "deepcoin_write", "ordinal": 1},),
        public_result={"status": "submitted"},
    )

    assert record_wakeup_outcome(
        session_factory,
        child_execution_id=claim.child_execution_id,
        claim_token=claim.claim_token,
        outcome=outcome,
        recorded_at=NOW,
    ) is False
    assert mark_wakeup_side_effect_started(
        session_factory,
        child_execution_id=claim.child_execution_id,
        entry_assembly_attempt_id=parent_id,
        wake_generation=claim.wake_generation,
        strategy_raw_message_id=claim.strategy_raw_message_id,
        trigger_raw_message_id=claim.trigger_raw_message_id,
        claim_token="stale-token",
        started_at=NOW,
    ) is False


def test_child_boundary_cas_rejects_strategy_or_trigger_raw_message_mismatch(tmp_path):
    session_factory, parent_id, trigger_id = _prepared(tmp_path)
    claim = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=trigger_id,
        now=NOW,
        execution_owner=_owner(),
    )[0]

    for strategy_id, completed_id in (
        (claim.strategy_raw_message_id + 1, claim.trigger_raw_message_id),
        (claim.strategy_raw_message_id, claim.trigger_raw_message_id + 1),
    ):
        assert mark_wakeup_side_effect_started(
            session_factory,
            child_execution_id=claim.child_execution_id,
            entry_assembly_attempt_id=parent_id,
            wake_generation=claim.wake_generation,
            strategy_raw_message_id=strategy_id,
            trigger_raw_message_id=completed_id,
            claim_token=claim.claim_token,
            started_at=NOW,
        ) is False


def test_drain_refuses_before_parent_or_child_wakeup_claim(tmp_path):
    session_factory, parent_id, trigger_id = _prepared(tmp_path)
    registry = RecognitionExecutionRegistry()
    registry.stop_admission()

    with pytest.raises(RuntimeError, match="draining"):
        claim_ready_entry_assembly_wakeups(
            session_factory,
            completed_raw_message_id=trigger_id,
            now=NOW,
            execution_owner=_owner(),
            execution_registry=registry,
        )

    with session_factory() as session:
        parent = session.get(EntryAssemblyAttempt, parent_id)
        assert parent.status == "pending"
        assert session.query(EntryAssemblyWakeupExecution).count() == 0


def test_drain_race_returns_parent_to_pending_before_adapter(tmp_path):
    session_factory, parent_id, trigger_id = _prepared(tmp_path)
    claim = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=trigger_id,
        now=NOW,
        execution_owner=_owner(),
    )[0]
    registry = RecognitionExecutionRegistry()
    registry.stop_admission()

    with pytest.raises(RuntimeError, match="draining"):
        run_claimed_entry_assembly_wakeup(
            session_factory,
            wake_claim=claim,
            auto_trade_executor=lambda _: pytest.fail("adapter must not run"),
            execution_registry=registry,
        )

    with session_factory() as session:
        parent = session.get(EntryAssemblyAttempt, parent_id)
        child = session.get(EntryAssemblyWakeupExecution, claim.child_execution_id)
        assert parent.status == "pending"
        assert child.status == "failed_safe"

    retry = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=trigger_id,
        now=NOW + timedelta(seconds=1),
        execution_owner=_owner(),
        execution_registry=RecognitionExecutionRegistry(),
    )
    assert len(retry) == 1
    assert retry[0].wake_generation == claim.wake_generation + 1


def test_wakeup_drain_race_preserves_original_error_when_fail_safe_raises(
    tmp_path, monkeypatch
):
    session_factory, _parent_id, trigger_id = _prepared(tmp_path)
    claim = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=trigger_id,
        now=NOW,
        execution_owner=_owner(),
    )[0]
    original = RuntimeError("recognition_execution_draining")

    class _RaceRegistry:
        def admitted(self, _token):
            class _RejectedScope:
                def __enter__(self):
                    raise original

                def __exit__(self, *_args):
                    return None

            return _RejectedScope()

    incidents = []
    monkeypatch.setattr(
        "telegram_kol_research.entry_assembly_wakeup_executions.fail_safe_wakeup",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("terminalization unavailable")
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.entry_assembly_wakeup_executions._capture_terminalization_failure",
        lambda *args, **kwargs: incidents.append(kwargs),
    )

    with pytest.raises(RuntimeError) as raised:
        run_claimed_entry_assembly_wakeup(
            session_factory,
            wake_claim=claim,
            auto_trade_executor=lambda _: pytest.fail("adapter must not run"),
            execution_registry=_RaceRegistry(),
        )

    assert raised.value is original
    assert incidents[0]["action"] == "drain_race_terminalize_failed"


def test_post_boundary_wakeup_failure_is_uncertain_and_parent_never_reopens(tmp_path):
    session_factory, parent_id, trigger_id = _prepared(tmp_path)
    claim = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=trigger_id,
        now=NOW,
        execution_owner=_owner(),
    )[0]
    calls = []

    def executor(raw_id):
        calls.append(raw_id)
        raise RuntimeError("lost after venue boundary")

    with pytest.raises(RuntimeError, match="lost after venue boundary"):
        run_claimed_entry_assembly_wakeup(
            session_factory,
            wake_claim=claim,
            auto_trade_executor=executor,
        )

    assert len(calls) == 1
    with session_factory() as session:
        parent = session.get(EntryAssemblyAttempt, parent_id)
        child = session.get(EntryAssemblyWakeupExecution, claim.child_execution_id)
        assert parent.status == "claimed"
        assert child.status == "uncertain"
    assert claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=99999,
        now=NOW + timedelta(minutes=6),
        execution_owner=_owner(),
    ) == ()


def test_recorded_wakeup_outcome_finalizes_parent_without_replay(tmp_path):
    session_factory, parent_id, trigger_id = _prepared(tmp_path)
    claim = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=trigger_id,
        now=NOW,
        execution_owner=_owner(),
    )[0]
    calls = []
    outcome = ExecutionBoundaryOutcome(
        status="completed",
        exchange_effect="not_started",
        raw_status="blocked",
        reason_code="policy",
        evidence_refs=(),
        public_result={"status": "blocked", "reason": "policy"},
    )
    run_claimed_entry_assembly_wakeup(
        session_factory,
        wake_claim=claim,
        auto_trade_executor=lambda raw_id: calls.append(raw_id) or outcome,
    )

    assert len(calls) == 1
    with session_factory() as session:
        parent = session.get(EntryAssemblyAttempt, parent_id)
        child = session.get(EntryAssemblyWakeupExecution, claim.child_execution_id)
        assert parent.status == "woken"
        assert child.status == "succeeded"


def test_wakeup_finalize_failure_records_incident_and_never_replays_adapter(
    tmp_path, monkeypatch
):
    session_factory, parent_id, trigger_id = _prepared(tmp_path)
    claim = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=trigger_id,
        now=NOW,
        execution_owner=_owner(),
    )[0]
    incidents = []
    calls = []
    monkeypatch.setattr(
        "telegram_kol_research.entry_assembly_wakeup_executions.finalize_recorded_wakeup",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("child finalize unavailable")
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.entry_assembly_wakeup_executions._capture_terminalization_failure",
        lambda *args, **kwargs: incidents.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="child finalize unavailable"):
        run_claimed_entry_assembly_wakeup(
            session_factory,
            wake_claim=claim,
            auto_trade_executor=lambda raw_id: calls.append(raw_id)
            or ExecutionBoundaryOutcome(
                status="completed",
                exchange_effect="not_started",
                raw_status="blocked",
                reason_code="policy",
                evidence_refs=(),
                public_result={"status": "blocked", "reason": "policy"},
            ),
        )

    assert calls == [claim.strategy_raw_message_id]
    assert incidents == [
        {
            "wake_claim": claim,
            "phase": "outcome_recorded",
            "action": "finalize_raised",
        }
    ]
    with session_factory() as session:
        assert session.get(EntryAssemblyAttempt, parent_id).status == "claimed"
        assert (
            session.get(EntryAssemblyWakeupExecution, claim.child_execution_id).status
            == "outcome_recorded"
        )

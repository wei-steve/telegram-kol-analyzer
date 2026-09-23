"""Phase 1+2 of the 2026-09-24 deferred-entry design.

Every acceptance here lands on the ``execution_events`` order ledger and on
``entry_assembly_wakeup_executions``, never on ``EntryAssemblyAttempt.status``.
The 2026-09-19 investigation was misled by that field: it read ``woken`` and
concluded the entry had been submitted, and the entry had never been submitted
at all.
"""

import json
from datetime import UTC, datetime, timedelta

from telegram_kol_research.authoritative_execution_attempts import (
    ExecutionOwnerIdentity,
)
from telegram_kol_research.authoritative_execution_schema import (
    apply_recognition_execution_schema,
    build_recognition_execution_schema_plan,
)
from telegram_kol_research.authoritative_recognition import (
    _run_entry_assembly_wakeups,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_admission_reconciler import (
    reconcile_due_entry_admissions,
)
from telegram_kol_research.entry_assembly_admission import (
    assess_entry_assembly_admission,
)
from telegram_kol_research.execution_boundary import ExecutionBoundaryOutcome
from telegram_kol_research.execution_events import (
    ExecutionEventRecord,
    record_execution_event,
)
from telegram_kol_research.models import (
    EntryAssemblyAttempt,
    EntryAssemblyWakeupExecution,
    ExecutionEvent,
    InstructionExecutionContract,
    MessageEvidenceExtractionClaim,
    MessageEvidenceVersion,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
)
from telegram_kol_research.recognition_execution_runtime import (
    RecognitionExecutionRegistry,
)


NOW = datetime(2026, 9, 24, 1, 17, tzinfo=UTC)

#: The order-ledger action a live limit entry writes
#: (``recovery_live_submit._record_entry_orders``).
ENTRY_ORDER_ACTION = "create_limit_entry"


def _owner():
    return ExecutionOwnerIdentity("worker", "instance", 4242, "boot", "9001")


def _session_factory(tmp_path, name):
    session_factory = create_session_factory(tmp_path / name)
    engine = session_factory.kw["bind"]
    plan = build_recognition_execution_schema_plan(engine)
    apply_recognition_execution_schema(engine, expected_plan_sha256=plan.plan_sha256)
    return session_factory


def _persist_deferred_entry(session_factory, *, seed=0, blockers=1):
    """One adjacent-context deferral, in the exact shape production leaves."""

    with session_factory() as session:
        strategy = RawMessage(
            chat_id=100 + seed,
            message_id=1000 + (seed * 100),
            posted_at=NOW,
            text="BTC 多 入场",
        )
        session.add(strategy)
        session.flush()
        blocker_ids = []
        for index in range(blockers):
            blocker = RawMessage(
                chat_id=100 + seed,
                message_id=1001 + (seed * 100) + index,
                posted_at=NOW + timedelta(seconds=1 + index),
                text="adjacent structured context",
            )
            session.add(blocker)
            session.flush()
            session.add(
                MessageEvidenceExtractionClaim(
                    raw_message_id=blocker.id,
                    input_fingerprint=f"blocker-input-{seed}-{index}",
                    claim_token=f"blocker-claim-{seed}-{index}",
                    claimed_at=NOW,
                    lease_expires_at=NOW + timedelta(minutes=5),
                )
            )
            blocker_ids.append(int(blocker.id))
        candidate = SignalCandidate(
            raw_message_id=strategy.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            recognition_generation=f"generation-{seed}",
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=strategy.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="entry",
            idempotency_key=f"{seed + 1:064x}",
            status="pending",
        )
        session.add(item)
        session.commit()
        ids = int(strategy.id), int(candidate.id), tuple(blocker_ids), int(item.id)

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=ids[0],
        signal_candidate_id=ids[1],
        mode="live",
        assessed_at=NOW + timedelta(seconds=2),
    )
    assert decision.status == "deferred"
    with session_factory() as session:
        item = session.get(MessageInstructionItem, ids[3])
        item.result_json = json.dumps(
            {"status": "deferred", "reason": "adjacent_entry_context_pending"}
        )
        item.visibility_next_attempt_at = NOW + timedelta(minutes=1)
        session.add(
            InstructionExecutionContract(
                message_instruction_item_id=item.id,
                raw_message_id=ids[0],
                signal_candidate_id=ids[1],
                intent_kind="entry",
                state="deferred",
                state_version=1,
                attempted_exchange_write=False,
                deadline_at=decision.deadline_at,
            )
        )
        session.commit()
    return ids


def _complete_blocker(session_factory, blocker_id):
    with session_factory() as session:
        claim = (
            session.query(MessageEvidenceExtractionClaim)
            .filter(
                MessageEvidenceExtractionClaim.raw_message_id == int(blocker_id)
            )
            .one()
        )
        input_fingerprint = claim.input_fingerprint
        session.delete(claim)
        session.add(
            MessageEvidenceVersion(
                raw_message_id=int(blocker_id),
                version=1,
                input_fingerprint=input_fingerprint,
                model="mimo",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=1,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json=(
                    '{"recognition_result":"非策略","strategy":null,'
                    '"lifecycle_event":{"event_type":"none"}}'
                ),
            )
        )
        session.commit()


def _order_placing_executor(session_factory, calls):
    """Stands in for the exchange writer and writes the same ledger row it does."""

    def executor(raw_message_id):
        calls.append(int(raw_message_id))
        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                action=ENTRY_ORDER_ACTION,
                status="submitted",
                venue="deepcoin",
                symbol="BTC",
                side="long",
                order_id=f"order-{raw_message_id}-{len(calls)}",
                source_message_id=int(raw_message_id),
                reason="live_signal_auto_trade",
            ),
        )
        return ExecutionBoundaryOutcome(
            status="completed",
            exchange_effect="confirmed_applied",
            raw_status="submitted",
            reason_code="entry_submitted",
            evidence_refs=({"kind": "deepcoin_write", "ordinal": 1},),
            public_result={"status": "submitted"},
        )

    return executor


def _entry_orders(session_factory, *, strategy_raw_message_id=None):
    with session_factory() as session:
        query = session.query(ExecutionEvent).filter(
            ExecutionEvent.action == ENTRY_ORDER_ACTION
        )
        if strategy_raw_message_id is not None:
            query = query.filter(
                ExecutionEvent.source_message_id == int(strategy_raw_message_id)
            )
        return query.all()


def _run_periodic_ready_claim(session_factory, executor):
    """Exactly what the worker's periodic cycle calls: no completed message."""

    _run_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=None,
        auto_trade_executor=executor,
        execution_owner=_owner(),
        execution_registry=RecognitionExecutionRegistry(),
    )


# --- design section 6, requirement 1 -----------------------------------------


def test_reconciled_entry_reaches_the_order_ledger_through_the_periodic_claim(
    tmp_path,
):
    session_factory = _session_factory(tmp_path, "ready-core.db")
    strategy_id, _, blocker_ids, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_ids[0])

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
    )
    assert result.released == 1
    with session_factory() as session:
        attempt = session.query(EntryAssemblyAttempt).one()
        assert attempt.status == "ready"
        assert json.loads(attempt.blocking_raw_message_ids_json) == []
        assert session.get(MessageInstructionItem, item_id).visibility_next_attempt_at is None
    # The reconciler alone must still not have placed anything: A-3d.
    assert _entry_orders(session_factory) == []

    calls = []
    _run_periodic_ready_claim(
        session_factory, _order_placing_executor(session_factory, calls)
    )

    orders = _entry_orders(session_factory, strategy_raw_message_id=strategy_id)
    assert len(orders) == 1
    assert orders[0].status == "submitted"
    assert calls == [strategy_id]
    with session_factory() as session:
        child = session.query(EntryAssemblyWakeupExecution).one()
        assert child.status == "succeeded"
        assert child.entry_assembly_attempt_id == (
            session.query(EntryAssemblyAttempt).one().id
        )


# --- design section 6, requirement 2 -----------------------------------------


def test_three_production_deferral_shapes_each_submit_exactly_once(tmp_path):
    """Replays the shape of attempts 29 (陈哥 10672), 25 (峰哥 9343) and 20.

    The production rows are not available offline, so what is replayed is their
    shape: one, two and three adjacent blockers respectively, all deferred, all
    released by the reconciler rather than by a wakeup.
    """

    session_factory = _session_factory(tmp_path, "ready-shapes.db")
    shapes = {29: 1, 25: 2, 20: 3}
    strategies = {}
    for seed, (attempt_label, blocker_count) in enumerate(shapes.items()):
        strategy_id, _, blocker_ids, _ = _persist_deferred_entry(
            session_factory, seed=seed, blockers=blocker_count
        )
        for blocker_id in blocker_ids:
            _complete_blocker(session_factory, blocker_id)
        strategies[attempt_label] = strategy_id

    released = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
    )
    assert released.released == 3

    calls = []
    executor = _order_placing_executor(session_factory, calls)
    for _ in range(len(shapes)):
        _run_periodic_ready_claim(session_factory, executor)

    for attempt_label, strategy_id in strategies.items():
        orders = _entry_orders(
            session_factory, strategy_raw_message_id=strategy_id
        )
        assert len(orders) == 1, attempt_label
    with session_factory() as session:
        assert (
            session.query(EntryAssemblyWakeupExecution)
            .filter(EntryAssemblyWakeupExecution.status == "succeeded")
            .count()
            == 3
        )
    assert sorted(calls) == sorted(strategies.values())


# --- design section 6, requirement 3 -----------------------------------------


def test_blocker_completion_still_wakes_and_submits_as_before(tmp_path):
    session_factory = _session_factory(tmp_path, "ready-legacy-trigger.db")
    strategy_id, _, blocker_ids, _ = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_ids[0])

    calls = []
    _run_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=blocker_ids[0],
        auto_trade_executor=_order_placing_executor(session_factory, calls),
        execution_owner=_owner(),
        execution_registry=RecognitionExecutionRegistry(),
    )

    assert calls == [strategy_id]
    orders = _entry_orders(session_factory, strategy_raw_message_id=strategy_id)
    assert len(orders) == 1
    with session_factory() as session:
        assert session.query(EntryAssemblyWakeupExecution).one().status == "succeeded"


# --- design section 6, requirement 4 -----------------------------------------


def test_reconciler_first_then_wakeup_submits_exactly_once(tmp_path):
    session_factory = _session_factory(tmp_path, "race-reconciler-first.db")
    strategy_id, _, blocker_ids, _ = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_ids[0])

    reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
    )

    calls = []
    executor = _order_placing_executor(session_factory, calls)
    # The periodic claim executes it, and then the blocker's own wakeup runs
    # against the very same attempt. Neither may submit a second order.
    _run_periodic_ready_claim(session_factory, executor)
    _run_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=blocker_ids[0],
        auto_trade_executor=executor,
        execution_owner=_owner(),
        execution_registry=RecognitionExecutionRegistry(),
    )

    assert calls == [strategy_id]
    assert len(_entry_orders(session_factory)) == 1
    with session_factory() as session:
        assert session.query(EntryAssemblyWakeupExecution).count() == 1


def test_wakeup_first_then_reconciler_submits_exactly_once(tmp_path):
    session_factory = _session_factory(tmp_path, "race-wakeup-first.db")
    strategy_id, _, blocker_ids, _ = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_ids[0])

    calls = []
    executor = _order_placing_executor(session_factory, calls)
    _run_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=blocker_ids[0],
        auto_trade_executor=executor,
        execution_owner=_owner(),
        execution_registry=RecognitionExecutionRegistry(),
    )
    # The reconciler arrives after the entry already executed. It must not
    # re-release it, and the later periodic claim must find nothing to do.
    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=20),
        execution_contract_mode="live",
    )
    _run_periodic_ready_claim(session_factory, executor)

    assert result.released == 0
    assert calls == [strategy_id]
    assert len(_entry_orders(session_factory)) == 1
    with session_factory() as session:
        assert session.query(EntryAssemblyWakeupExecution).count() == 1


def test_a_claimed_attempt_is_invisible_to_the_reconciler(tmp_path):
    """The mutual exclusion itself: one compare-and-set on the attempt row."""

    session_factory = _session_factory(tmp_path, "race-claimed.db")
    _persist_deferred_entry(session_factory)
    with session_factory() as session:
        attempt = session.query(EntryAssemblyAttempt).one()
        attempt.status = "claimed"
        attempt.wake_claim_token = "token"
        session.commit()

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
    )

    assert result == type(result)()
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).one().status == "claimed"


# --- design section 6, requirement 5 -----------------------------------------


def test_ready_attempt_expires_through_the_entry_channel_with_one_alert(tmp_path):
    session_factory = _session_factory(tmp_path, "ready-deadline.db")
    _, _, blocker_ids, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_ids[0])

    reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
    )
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).one().status == "ready"
        item = session.get(MessageInstructionItem, item_id)
        item.execution_deadline_at = NOW + timedelta(seconds=30)
        session.commit()

    incidents = []

    def incident_reporter(**kwargs):
        incidents.append(kwargs)
        return object()

    expired = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=60),
        execution_contract_mode="live",
        incident_reporter=incident_reporter,
    )
    repeated = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=90),
        execution_contract_mode="live",
        incident_reporter=incident_reporter,
    )

    assert expired.expired == 1
    assert expired.incidents == 1
    assert repeated.expired == 0
    assert len(incidents) == 1
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        assert item.status == "failed"
        assert (
            json.loads(item.error_json)["reason"]
            == "entry_admission_deadline_expired"
        )
        assert session.query(EntryAssemblyAttempt).one().status == "expired"
        assert (
            session.query(InstructionExecutionContract).one().reason_code
            == "entry_admission_deadline_expired"
        )

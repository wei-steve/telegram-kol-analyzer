import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import RequestAttemptFact
from telegram_kol_research.deepcoin_execution_operations import record_request_attempt
from telegram_kol_research.deepcoin_execution_operations import advance_account_write_generation
from telegram_kol_research.deepcoin_execution_operations import record_snapshot_evidence
from telegram_kol_research.deepcoin_execution_operations import reserve_execution_operation
from telegram_kol_research.deepcoin_request_policy import ErrorCategory
from telegram_kol_research.deepcoin_request_policy import OutcomeCertainty
from telegram_kol_research.deepcoin_request_policy import RequestPriority
from telegram_kol_research.models import StrategyLifecycle
from telegram_kol_research.models import DeepcoinExecutionOperation
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import ExecutionOrderLeg
from telegram_kol_research.models import PositionMutationIntent
from telegram_kol_research.models import TradeIdea
from telegram_kol_research.models import TradeSignal
from telegram_kol_research.models import TriggerProtectionIntent
from telegram_kol_research.trade_signals import enqueue_trade_signal
from telegram_kol_research.trade_signals import finalize_trade_signal_from_execution_operation
from telegram_kol_research.trade_signals import list_pending_trade_signals
from telegram_kol_research.trade_signals import load_trade_signal
from telegram_kol_research.trade_signals import mark_trade_signal_failed
from telegram_kol_research.trade_signals import mark_trade_signal_submitted
from telegram_kol_research.trade_signals import TradeSignalTransitionError


NOW = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)


def _processing_entry_with_lifecycle(session_factory):
    signal = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="alice",
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        action="open_position",
        payload={},
    )
    with session_factory() as session:
        idea = TradeIdea(
            chat_id=100,
            symbol="BTC",
            side="long",
            status="open",
            opened_at=NOW.replace(tzinfo=None),
        )
        session.add(idea)
        session.flush()
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=NOW.replace(tzinfo=None),
                entered_at=NOW.replace(tzinfo=None),
                trade_idea_id=idea.id,
            )
        )
        row = session.get(TradeSignal, signal.id)
        row.status = "processing"
        session.commit()
    return signal


def _parent_operation(
    session_factory,
    signal_id,
    *,
    state,
    phase,
    certainty,
    reason_code,
    expected_entry_leg_indices=(1,),
):
    is_no_exposure = state == "submission_failed_no_exposure"
    evidence = {
        "leg_index": expected_entry_leg_indices[0],
        "expected_entry_leg_indices": list(expected_entry_leg_indices),
        "uid_scope_hash": "9" * 64,
        "pre_submit_position_refs": [],
    }
    if state == "protected":
        evidence.update(
            {
                "required_protection_count": 1,
                "confirmed_protection_count": 1,
            }
        )
    return reserve_execution_operation(
        session_factory,
        operation_key=f"protected-entry:v1:signal:{signal_id}:leg:1:entry",
        trade_signal_id=signal_id,
        contract_version="1",
        phase=phase,
        state=state,
        outcome_certainty=certainty,
        reason_code=reason_code,
        request_fingerprint="a" * 64,
        economics_fingerprint="b" * 64,
        deadline_at=NOW + timedelta(seconds=10),
        writer_attempted_at=NOW,
        completed_at=(NOW + timedelta(milliseconds=3)) if is_no_exposure else None,
        evidence=evidence,
        created_at=NOW,
    )


def _assert_lifecycle_remains_open(session_factory):
    with session_factory() as session:
        lifecycle = session.query(StrategyLifecycle).one()
        idea = session.query(TradeIdea).one()
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None
    assert idea.status == "open"
    assert idea.closed_at is None


def _attach_child_authority(
    session_factory,
    *,
    signal,
    child_id,
    leg_index,
    protection,
):
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one_or_none()
        if binding is None:
            binding = ExecutionBinding(
                strategy_instance_id=signal.strategy_instance_id,
                kol_id="alice",
                chat_id=signal.chat_id,
                message_id=signal.message_id,
                symbol=signal.symbol,
                side=signal.side,
                venue="deepcoin",
                status="active",
            )
            session.add(binding)
            session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=signal.strategy_instance_id,
            leg_index=leg_index,
            purpose="entry",
            order_kind="market" if protection else "trigger_limit",
            venue="deepcoin",
            status="submitted",
        )
        session.add(leg)
        session.flush()
        child = session.get(DeepcoinExecutionOperation, child_id)
        child.execution_binding_id = binding.id
        child.execution_order_leg_id = leg.id
        evidence = json.loads(child.evidence_json)
        if protection:
            intent = PositionMutationIntent(
                idempotency_key=f"test-protection:{child.id}",
                venue="deepcoin",
                operation="set_position_sltp",
                strategy_instance_id=signal.strategy_instance_id,
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                pos_id="test-pos",
                order_id=f"test-protection-order-{child.id}",
                authority_fingerprint="8" * 64,
                request_fingerprint=child.request_fingerprint,
                status="confirmed",
                request_json="{}",
                response_json="{}",
                reserved_at=NOW.replace(tzinfo=None),
                submitted_at=NOW.replace(tzinfo=None),
                confirmed_at=NOW.replace(tzinfo=None),
            )
            session.add(intent)
            session.flush()
            evidence["position_mutation_intent_id"] = intent.id
        elif child.state == "completed":
            session.add(
                TriggerProtectionIntent(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    request_fingerprint=child.request_fingerprint,
                    pre_submit_tpsl_baseline_json="[]",
                    correlation_id=f"test-trigger:{child.id}",
                    parent_trigger_order_id=(
                        f"test-trigger-order-{child.id}"
                        if child.state == "completed"
                        else None
                    ),
                )
            )
        child.evidence_json = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        session.commit()


def test_enqueue_trade_signal_creates_stable_pending_signal(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    first = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="alice",
        chat_id=100,
        message_id=55,
        symbol="btc",
        side="LONG",
        action="open_position",
        payload={"hello": "world"},
    )
    second = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="alice",
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        action="open_position",
        payload={"hello": "again"},
    )

    assert first.id == second.id
    assert second.signal_uid == "deepcoin:recovery:100:55:BTC:long:open_position"
    assert second.strategy_instance_id == "deepcoin:100:55:BTC:long"
    assert second.status == "pending"
    assert second.payload == {"hello": "again"}
    assert [item.id for item in list_pending_trade_signals(session_factory)] == [first.id]


def test_trade_signal_status_transitions(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="alice",
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        action="open_position",
        payload={},
    )

    mark_trade_signal_failed(session_factory, signal_id=signal.id, error="boom")
    with session_factory() as session:
        row = session.query(TradeSignal).one()
    assert row.status == "failed"
    assert row.attempts == 1
    assert row.last_error == "boom"

    refreshed = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="alice",
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        action="open_position",
        payload={},
    )
    assert refreshed.status == "pending"

    mark_trade_signal_submitted(
        session_factory,
        signal_id=signal.id,
        result={"submitted": True},
    )
    with session_factory() as session:
        row = session.query(TradeSignal).one()
    assert row.status == "submitted"
    assert '"submitted": true' in row.result_json


@pytest.mark.parametrize(
    ("state", "phase", "certainty", "expected_projection"),
    [
        (
            "protection_unknown",
            "protection_readback",
            "unknown",
            "active_protection_pending",
        ),
        (
            "recovery_required",
            "protection_readback",
            "unknown",
            "recovery_required",
        ),
        (
            "entry_unknown",
            "entry_readback",
            "unknown",
            "recovery_required",
        ),
    ],
)
def test_live_exposure_and_unknown_writer_projection_preserves_lifecycle(
    tmp_path,
    state,
    phase,
    certainty,
    expected_projection,
):
    session_factory = create_session_factory(tmp_path / f"{state}.db")
    signal = _processing_entry_with_lifecycle(session_factory)
    _parent_operation(
        session_factory,
        signal.id,
        state=state,
        phase=phase,
        certainty=certainty,
        reason_code="protected_entry_execution_failed",
    )

    projection = finalize_trade_signal_from_execution_operation(
        session_factory,
        signal_id=signal.id,
        finalized_at=NOW + timedelta(seconds=1),
        expected_status="processing",
        safe_error_code="submission_failed_no_exposure",
    )

    assert projection == expected_projection
    assert load_trade_signal(session_factory, signal.id).status == expected_projection
    _assert_lifecycle_remains_open(session_factory)


def test_active_protected_deferred_projection_preserves_lifecycle(tmp_path):
    session_factory = create_session_factory(tmp_path / "deferred.db")
    signal = _processing_entry_with_lifecycle(session_factory)
    parent = _parent_operation(
        session_factory,
        signal.id,
        state="protected",
        phase="protection_readback",
        certainty="confirmed",
        reason_code="protection_fully_confirmed",
        expected_entry_leg_indices=(1, 2),
    )
    protection_child = reserve_execution_operation(
        session_factory,
        operation_key=f"protected-entry:v1:signal:{signal.id}:leg:1:protection:0",
        trade_signal_id=signal.id,
        parent_operation_id=parent.id,
        contract_version="1",
        phase="protection_readback",
        state="protected",
        outcome_certainty="confirmed",
        reason_code="protection_fully_confirmed",
        request_fingerprint="e" * 64,
        economics_fingerprint="f" * 64,
        deadline_at=NOW + timedelta(seconds=10),
        writer_attempted_at=NOW,
        completed_at=NOW + timedelta(milliseconds=1),
        evidence={
            "protection_index": 0,
            "position_mutation_intent_id": 1,
        },
        created_at=NOW,
    )
    deferred_child = reserve_execution_operation(
        session_factory,
        operation_key=f"protected-entry:v1:signal:{signal.id}:leg:2:entry",
        trade_signal_id=signal.id,
        parent_operation_id=parent.id,
        contract_version="1",
        phase="next_leg_preflight",
        state="pre_submit_deferred",
        outcome_certainty="not_sent",
        reason_code="next_leg_preflight_deferred",
        request_fingerprint="c" * 64,
        economics_fingerprint="d" * 64,
        deadline_at=NOW + timedelta(seconds=10),
        evidence={"leg_index": 2, "writer_attempted": False},
        created_at=NOW,
    )
    _attach_child_authority(
        session_factory,
        signal=signal,
        child_id=protection_child.id,
        leg_index=1,
        protection=True,
    )
    _attach_child_authority(
        session_factory,
        signal=signal,
        child_id=deferred_child.id,
        leg_index=2,
        protection=False,
    )

    projection = finalize_trade_signal_from_execution_operation(
        session_factory,
        signal_id=signal.id,
        finalized_at=NOW + timedelta(seconds=1),
        expected_status="processing",
        safe_error_code="protected_entry_pre_submit_deferred",
    )

    assert projection == "active_protected_deferred"
    assert load_trade_signal(session_factory, signal.id).status == (
        "active_protected_deferred"
    )
    _assert_lifecycle_remains_open(session_factory)


def test_execution_projection_never_persists_credential_shaped_error(tmp_path):
    session_factory = create_session_factory(tmp_path / "redacted-error.db")
    signal = _processing_entry_with_lifecycle(session_factory)
    _parent_operation(
        session_factory,
        signal.id,
        state="entry_unknown",
        phase="entry_readback",
        certainty="unknown",
        reason_code="entry_submission_unknown",
    )

    finalize_trade_signal_from_execution_operation(
        session_factory,
        signal_id=signal.id,
        finalized_at=NOW + timedelta(seconds=1),
        expected_status="processing",
        safe_error_code="dc_access_key:topsecret",
    )

    persisted = load_trade_signal(session_factory, signal.id)
    assert persisted.status == "recovery_required"
    assert persisted.last_error == "protected_entry_execution_failed"
    _assert_lifecycle_remains_open(session_factory)


def test_submitted_projection_redacts_credential_shaped_result(tmp_path):
    session_factory = create_session_factory(tmp_path / "redacted-result.db")
    signal = _processing_entry_with_lifecycle(session_factory)
    parent = _parent_operation(
        session_factory,
        signal.id,
        state="protected",
        phase="protection_readback",
        certainty="confirmed",
        reason_code="protection_fully_confirmed",
    )
    protection_child = reserve_execution_operation(
        session_factory,
        operation_key=f"protected-entry:v1:signal:{signal.id}:leg:1:protection:0",
        trade_signal_id=signal.id,
        parent_operation_id=parent.id,
        contract_version="1",
        phase="protection_readback",
        state="protected",
        outcome_certainty="confirmed",
        reason_code="protection_fully_confirmed",
        request_fingerprint="e" * 64,
        economics_fingerprint="f" * 64,
        deadline_at=NOW + timedelta(seconds=10),
        writer_attempted_at=NOW,
        completed_at=NOW + timedelta(milliseconds=1),
        evidence={
            "protection_index": 0,
            "position_mutation_intent_id": 1,
        },
        created_at=NOW,
    )
    _attach_child_authority(
        session_factory,
        signal=signal,
        child_id=protection_child.id,
        leg_index=1,
        protection=True,
    )

    projection = finalize_trade_signal_from_execution_operation(
        session_factory,
        signal_id=signal.id,
        result={"DC-ACCESS-KEY": "topsecret"},
        finalized_at=NOW + timedelta(seconds=1),
        expected_status="processing",
    )

    assert projection == "submitted"
    with session_factory() as session:
        result_json = session.get(TradeSignal, signal.id).result_json
    assert "topsecret" not in result_json
    assert "DC-ACCESS-KEY" not in result_json
    assert json.loads(result_json) == {
        "result_redacted": True,
        "status": "submitted",
    }


def test_concurrent_execution_projection_has_exactly_one_terminal_winner(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "concurrent-finalizer.db")
    signal = _processing_entry_with_lifecycle(session_factory)
    parent = _parent_operation(
        session_factory,
        signal.id,
        state="protected",
        phase="protection_readback",
        certainty="confirmed",
        reason_code="protection_fully_confirmed",
    )
    protection_child = reserve_execution_operation(
        session_factory,
        operation_key=(
            f"protected-entry:v1:signal:{signal.id}:leg:1:protection:0"
        ),
        trade_signal_id=signal.id,
        parent_operation_id=parent.id,
        contract_version="1",
        phase="protection_readback",
        state="protected",
        outcome_certainty="confirmed",
        reason_code="protection_fully_confirmed",
        request_fingerprint="e" * 64,
        economics_fingerprint="f" * 64,
        deadline_at=NOW + timedelta(seconds=10),
        writer_attempted_at=NOW,
        completed_at=NOW + timedelta(milliseconds=1),
        evidence={
            "protection_index": 0,
            "position_mutation_intent_id": 1,
        },
        created_at=NOW,
    )
    _attach_child_authority(
        session_factory,
        signal=signal,
        child_id=protection_child.id,
        leg_index=1,
        protection=True,
    )
    start = Barrier(2)

    def finalize(caller: int):
        start.wait()
        try:
            return (
                "submitted",
                finalize_trade_signal_from_execution_operation(
                    session_factory,
                    signal_id=signal.id,
                    result={"caller": caller},
                    finalized_at=NOW + timedelta(seconds=caller),
                    expected_status="processing",
                ),
            )
        except TradeSignalTransitionError as exc:
            return ("conflict", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(finalize, (1, 2)))

    assert sorted(outcome[0] for outcome in outcomes) == [
        "conflict",
        "submitted",
    ]
    assert [
        detail for status, detail in outcomes if status == "submitted"
    ] == ["submitted"]
    assert [
        detail for status, detail in outcomes if status == "conflict"
    ] == ["trade_signal_projection_transition_failed"]
    with session_factory() as session:
        persisted = session.get(TradeSignal, signal.id)
        result = json.loads(persisted.result_json)
    assert persisted.status == "submitted"
    assert persisted.attempts == 0
    assert result in ({"caller": 1}, {"caller": 2})
    _assert_lifecycle_remains_open(session_factory)


def test_submission_failed_no_exposure_requires_complete_zero_exposure_proof(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "no-exposure.db")
    signal = _processing_entry_with_lifecycle(session_factory)
    parent = _parent_operation(
        session_factory,
        signal.id,
        state="submission_failed_no_exposure",
        phase="completed",
        certainty="confirmed",
        reason_code="submission_failed_no_exposure_confirmed",
    )
    record_request_attempt(
        session_factory,
        operation_id=parent.id,
        expected_operation_key=parent.operation_key,
        expected_request_fingerprint=parent.request_fingerprint,
        uid_scope_hash="9" * 64,
        fact=RequestAttemptFact(
            ordinal=1,
            method="POST",
            normalized_path="/deepcoin/trade/order",
            phase="entry_submit",
            priority=RequestPriority.CRITICAL,
            correlation_id="task11-rejected-entry",
            outcome_certainty=OutcomeCertainty.REJECTED,
            error_category=ErrorCategory.BUSINESS_REJECTED,
            safe_code="entry_submission_rejected",
            http_status=200,
            business_code="rejected",
            governor_wait_ms=0,
            retry_delay_ms=0,
            latency_ms=1,
        ),
        started_at=NOW,
        completed_at=NOW + timedelta(microseconds=500),
    )
    advance_account_write_generation(
        session_factory,
        uid_scope_hash="9" * 64,
        updated_at=NOW + timedelta(microseconds=600),
    )
    advance_account_write_generation(
        session_factory,
        uid_scope_hash="9" * 64,
        updated_at=NOW + timedelta(microseconds=700),
    )
    empty_fingerprint = (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5"
        "ed12ab4d8e11ba873c2f11161202b945"
    )
    for ordinal, snapshot_kind in enumerate(
        ("positions", "open_orders", "trigger_orders_pending"),
        start=1,
    ):
        record_snapshot_evidence(
            session_factory,
            operation_id=parent.id,
            expected_operation_key=parent.operation_key,
            snapshot_kind=snapshot_kind,
            available=True,
            schema_valid=True,
            complete=True,
            row_count=0,
            page_count=1,
            collection_fingerprint=empty_fingerprint,
            start_write_generation=2,
            end_write_generation=2,
            capture_started_at=NOW + timedelta(milliseconds=1),
            capture_ended_at=NOW + timedelta(milliseconds=2),
            evidence={"target_identity": "hashed"},
        )

    projection = finalize_trade_signal_from_execution_operation(
        session_factory,
        signal_id=signal.id,
        finalized_at=NOW + timedelta(seconds=1),
        expected_status="processing",
        safe_error_code="entry_submission_rejected",
    )

    assert projection == "submission_failed_no_exposure"
    with session_factory() as session:
        persisted_signal = session.get(TradeSignal, signal.id)
        lifecycle = session.query(StrategyLifecycle).one()
        idea = session.query(TradeIdea).one()
    assert persisted_signal.status == "submission_failed_no_exposure"
    assert lifecycle.lifecycle_status == "invalidated"
    assert lifecycle.exit_reason == "auto_trade_failed"
    assert idea.status == "closed"


def test_strategy_revision_no_exposure_never_closes_colliding_lifecycle(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "revision-collision.db")
    signal = _processing_entry_with_lifecycle(session_factory)
    with session_factory() as session:
        session.get(TradeSignal, signal.id).source_type = "strategy_revision"
        session.commit()
    parent = _parent_operation(
        session_factory,
        signal.id,
        state="submission_failed_no_exposure",
        phase="completed",
        certainty="confirmed",
        reason_code="submission_failed_no_exposure_confirmed",
    )
    record_request_attempt(
        session_factory,
        operation_id=parent.id,
        expected_operation_key=parent.operation_key,
        expected_request_fingerprint=parent.request_fingerprint,
        uid_scope_hash="9" * 64,
        fact=RequestAttemptFact(
            ordinal=1,
            method="POST",
            normalized_path="/deepcoin/trade/order",
            phase="entry_submit",
            priority=RequestPriority.CRITICAL,
            correlation_id="revision-rejected-entry",
            outcome_certainty=OutcomeCertainty.REJECTED,
            error_category=ErrorCategory.BUSINESS_REJECTED,
            safe_code="entry_submission_rejected",
            http_status=200,
            business_code="rejected",
            governor_wait_ms=0,
            retry_delay_ms=0,
            latency_ms=1,
        ),
        started_at=NOW,
        completed_at=NOW + timedelta(microseconds=500),
    )
    advance_account_write_generation(
        session_factory,
        uid_scope_hash="9" * 64,
        updated_at=NOW + timedelta(microseconds=600),
    )
    advance_account_write_generation(
        session_factory,
        uid_scope_hash="9" * 64,
        updated_at=NOW + timedelta(microseconds=700),
    )
    empty_fingerprint = (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5"
        "ed12ab4d8e11ba873c2f11161202b945"
    )
    for snapshot_kind in (
        "positions",
        "open_orders",
        "trigger_orders_pending",
    ):
        record_snapshot_evidence(
            session_factory,
            operation_id=parent.id,
            expected_operation_key=parent.operation_key,
            snapshot_kind=snapshot_kind,
            available=True,
            schema_valid=True,
            complete=True,
            row_count=0,
            page_count=1,
            collection_fingerprint=empty_fingerprint,
            start_write_generation=2,
            end_write_generation=2,
            capture_started_at=NOW + timedelta(milliseconds=1),
            capture_ended_at=NOW + timedelta(milliseconds=2),
            evidence={"target_identity": "hashed"},
        )

    projection = finalize_trade_signal_from_execution_operation(
        session_factory,
        signal_id=signal.id,
        finalized_at=NOW + timedelta(seconds=1),
        expected_status="processing",
        safe_error_code="entry_submission_rejected",
    )

    assert projection == "submission_failed_no_exposure"
    _assert_lifecycle_remains_open(session_factory)


def test_incomplete_no_exposure_claim_projects_recovery_required(tmp_path):
    session_factory = create_session_factory(tmp_path / "incomplete-proof.db")
    signal = _processing_entry_with_lifecycle(session_factory)
    _parent_operation(
        session_factory,
        signal.id,
        state="submission_failed_no_exposure",
        phase="completed",
        certainty="confirmed",
        reason_code="submission_failed_no_exposure_confirmed",
    )

    projection = finalize_trade_signal_from_execution_operation(
        session_factory,
        signal_id=signal.id,
        finalized_at=NOW + timedelta(seconds=1),
        expected_status="processing",
        safe_error_code="entry_submission_rejected",
    )

    assert projection == "recovery_required"
    _assert_lifecycle_remains_open(session_factory)


def test_projection_statuses_never_reenter_pending_claim_queue(tmp_path):
    session_factory = create_session_factory(tmp_path / "frozen.db")
    signal = _processing_entry_with_lifecycle(session_factory)
    _parent_operation(
        session_factory,
        signal.id,
        state="entry_unknown",
        phase="entry_readback",
        certainty="unknown",
        reason_code="entry_submission_unknown",
    )
    finalize_trade_signal_from_execution_operation(
        session_factory,
        signal_id=signal.id,
        finalized_at=NOW + timedelta(seconds=1),
        expected_status="processing",
        safe_error_code="entry_submission_unknown",
    )

    reused = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="alice",
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        action="open_position",
        payload={"changed": True},
    )

    assert reused.status == "recovery_required"
    assert list_pending_trade_signals(session_factory) == []

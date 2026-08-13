import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_execution_operations import (
    advance_account_write_generation,
    record_request_attempt,
    reserve_execution_operation,
)
from telegram_kol_research.deepcoin_client import RequestAttemptFact
from telegram_kol_research.deepcoin_request_policy import ErrorCategory
from telegram_kol_research.deepcoin_request_policy import OutcomeCertainty
from telegram_kol_research.deepcoin_request_policy import RequestPriority
from telegram_kol_research.deepcoin_snapshot_authority import (
    AccountSnapshotEvidence,
    build_exchange_collection_evidence,
)
from telegram_kol_research.execution_bindings import (
    load_deepcoin_reconciliation_snapshot_for_instruments_read_only,
)
from telegram_kol_research.models import DeepcoinExecutionOperation
from telegram_kol_research.models import DeepcoinAccountWriteGeneration
from telegram_kol_research.models import DeepcoinRequestAttempt
from telegram_kol_research.models import DeepcoinSnapshotEvidence
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import ExecutionOrderLeg
from telegram_kol_research.models import PositionMutationIntent
from telegram_kol_research.models import PositionProtectionLedger
from telegram_kol_research.models import TradeSignal
from telegram_kol_research.position_mutation_intents import (
    bound_set_position_authority_fingerprint,
)
from telegram_kol_research.protected_entry_reconciliation import (
    reconcile_protected_entry_operations,
)
import telegram_kol_research.protected_entry_reconciliation as reconciliation_module
from telegram_kol_research.trade_signals import enqueue_trade_signal


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
UID_SCOPE_HASH = "9" * 64


def _market_request(*, client_order_id="client-entry-1"):
    return {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "cross",
        "side": "buy",
        "posSide": "long",
        "ordType": "market",
        "sz": "1",
        "clOrdId": client_order_id,
        "mrgPosition": "split",
    }


def _canonical_fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _signal(
    session_factory,
    *,
    status="recovery_required",
    message_id=55,
):
    signal = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="alice",
        chat_id=100,
        message_id=message_id,
        symbol="BTC",
        side="long",
        action="open_position",
        payload={
            "deepcoin_order_draft": {
                "instrument_id": "BTC-USDT-SWAP",
                "margin_mode": "cross",
                "position_mode": "split",
                "order_legs": [
                    {
                        "order_type": "market",
                        "side": "buy",
                        "position_side": "long",
                        "quantity": 1,
                        "client_order_id": "client-entry-1",
                    }
                ],
            }
        },
    )
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        row.status = status
        session.commit()
    return signal


def _entry_operation(
    session_factory,
    *,
    signal_id,
    state="entry_pending_readback",
    client_order_id="client-entry-1",
    record_post_attempt=True,
):
    operation = reserve_execution_operation(
        session_factory,
        operation_key=f"protected-entry:v1:signal:{signal_id}:leg:1:entry",
        trade_signal_id=signal_id,
        contract_version="1",
        phase="entry_readback",
        state=state,
        outcome_certainty=(
            "accepted" if state == "entry_pending_readback" else "unknown"
        ),
        reason_code=(
            "entry_submission_accepted"
            if state == "entry_pending_readback"
            else "entry_submission_unknown"
        ),
        request_fingerprint=_canonical_fingerprint(
            _market_request(client_order_id=client_order_id)
        ),
        economics_fingerprint=_canonical_fingerprint(
            {
                "client_order_id": client_order_id,
                "instrument_id": "BTC-USDT-SWAP",
                "leg_index": 1,
                "position_side": "long",
                "quantity": "1",
                "side": "buy",
            }
        ),
        deadline_at=NOW + timedelta(seconds=10),
        writer_attempted_at=NOW,
        evidence={
            "client_order_ref": hashlib.sha256(
                client_order_id.encode("utf-8")
            ).hexdigest(),
            "expected_entry_leg_indices": [1],
            "leg_index": 1,
            "pre_submit_position_refs": [],
            "uid_scope_hash": UID_SCOPE_HASH,
        },
        created_at=NOW,
    )
    if record_post_attempt:
        accepted = state == "entry_pending_readback"
        record_request_attempt(
            session_factory,
            operation_id=operation.id,
            expected_operation_key=operation.operation_key,
            expected_request_fingerprint=operation.request_fingerprint,
            uid_scope_hash=UID_SCOPE_HASH,
            fact=RequestAttemptFact(
                ordinal=1,
                method="POST",
                normalized_path="/deepcoin/trade/order",
                phase="entry_submit",
                priority=RequestPriority.CRITICAL,
                correlation_id="protected-entry-reconcile-test",
                outcome_certainty=(
                    OutcomeCertainty.ACCEPTED
                    if accepted
                    else OutcomeCertainty.UNKNOWN
                ),
                error_category=(
                    None if accepted else ErrorCategory.TRANSPORT_TIMEOUT
                ),
                safe_code=(
                    "entry_submission_accepted"
                    if accepted
                    else "entry_submission_unknown"
                ),
                http_status=200 if accepted else None,
                business_code="0" if accepted else None,
                governor_wait_ms=0,
                retry_delay_ms=0,
                latency_ms=1,
            ),
            started_at=NOW,
            completed_at=NOW + timedelta(milliseconds=1),
        )
    advance_account_write_generation(
        session_factory,
        uid_scope_hash=UID_SCOPE_HASH,
        updated_at=NOW,
    )
    advance_account_write_generation(
        session_factory,
        uid_scope_hash=UID_SCOPE_HASH,
        updated_at=NOW + timedelta(milliseconds=1),
    )
    return operation


def _snapshot(
    *,
    positions=(),
    position_history=(),
    open_orders=(),
    pending_trigger_orders=(),
    order_history=(),
    trade_fills=(),
    trigger_history=(),
    errors=None,
    uid_scope_hash=UID_SCOPE_HASH,
    start_generation=2,
    end_generation=2,
):
    safe_errors = dict(errors or {})
    complete = not safe_errors and start_generation == end_generation
    rows_by_kind = {
        "positions": [dict(row) for row in positions],
        "position_history": [dict(row) for row in position_history],
        "open_orders": [dict(row) for row in open_orders],
        "pending_trigger_orders": [dict(row) for row in pending_trigger_orders],
        "order_history": [dict(row) for row in order_history],
        "trade_fills": [dict(row) for row in trade_fills],
        "trigger_history": [dict(row) for row in trigger_history],
    }
    row_hashes = {
        kind: sorted(_canonical_fingerprint(row) for row in rows)
        for kind, rows in rows_by_kind.items()
    }
    composite_fingerprint = _canonical_fingerprint(row_hashes)
    composite_collection = build_exchange_collection_evidence(
        endpoint="account_composite",
        response={
            "data": [
                {
                    "collection_fingerprint": composite_fingerprint,
                    "collection_kinds": sorted(rows_by_kind),
                }
            ]
        },
    )
    return SimpleNamespace(
        positions=[dict(row) for row in positions],
        position_history=[dict(row) for row in position_history],
        open_orders=[dict(row) for row in open_orders],
        pending_trigger_orders=[dict(row) for row in pending_trigger_orders],
        order_history=[dict(row) for row in order_history],
        trade_fills=[dict(row) for row in trade_fills],
        trigger_history=[dict(row) for row in trigger_history],
        errors=safe_errors,
        account_authority=AccountSnapshotEvidence(
            uid_scope_hash=uid_scope_hash,
            start_write_generation=start_generation,
            end_write_generation=end_generation,
            collections=(composite_collection,),
            complete=complete,
            reason_code=None if complete else "snapshot_incomplete",
        ),
        capture_started_at=NOW + timedelta(milliseconds=4),
        capture_ended_at=NOW + timedelta(milliseconds=5),
    )


def _exact_entry_snapshot(*, client_order_id="client-entry-1"):
    return _snapshot(
        positions=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "position-1",
                "posSide": "long",
                "pos": "1",
            }
        ],
        order_history=[
            {
                "clOrdId": client_order_id,
                "instId": "BTC-USDT-SWAP",
                "ordId": "order-1",
                "posId": "position-1",
                "posSide": "long",
                "side": "buy",
                "state": "filled",
                "sz": "1",
            }
        ],
    )


def test_entry_pending_readback_confirms_from_exact_shared_snapshot(tmp_path):
    session_factory = create_session_factory(tmp_path / "entry-confirm.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_entry_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.checked == 1
    assert result.confirmed == 1
    assert result.conflicts == 0
    assert result.unchanged == 0
    with session_factory() as session:
        persisted = session.get(DeepcoinExecutionOperation, operation.id)
        evidence = json.loads(persisted.evidence_json)
        snapshots = session.query(DeepcoinSnapshotEvidence).all()
    assert persisted.state == "entry_confirmed"
    assert persisted.outcome_certainty == "confirmed"
    assert evidence["order_ref"] == hashlib.sha256(b"order:order-1").hexdigest()
    assert evidence["position_ref"] == hashlib.sha256(
        b"position:position-1"
    ).hexdigest()
    assert "order-1" not in persisted.evidence_json
    assert "position-1" not in persisted.evidence_json
    assert len(snapshots) == 1
    assert snapshots[0].snapshot_kind == "account_composite"
    assert snapshots[0].complete is True

    repeated = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_entry_snapshot(),
        reconciled_at=NOW + timedelta(seconds=2),
    )
    assert repeated.checked == 0
    assert repeated.confirmed == 0
    with session_factory() as session:
        assert session.query(DeepcoinSnapshotEvidence).count() == 1


def test_incomplete_snapshot_appends_safe_evidence_and_preserves_unknown_state(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "entry-incomplete.db")
    signal = _signal(session_factory)
    operation = _entry_operation(
        session_factory,
        signal_id=signal.id,
        state="entry_unknown",
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_snapshot(
            errors={
                "order_history:BTC-USDT-SWAP": (
                    "Authorization: Bearer TOPSECRET"
                )
            }
        ),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.checked == 1
    assert result.unchanged == 1
    with session_factory() as session:
        persisted = session.get(DeepcoinExecutionOperation, operation.id)
        snapshot_evidence = session.query(DeepcoinSnapshotEvidence).one()
    assert persisted.state == "entry_unknown"
    assert snapshot_evidence.complete is False
    assert snapshot_evidence.error_code == "protected_entry_snapshot_incomplete"
    assert "TOPSECRET" not in repr(snapshot_evidence.__dict__)
    assert "Authorization" not in snapshot_evidence.evidence_json


def test_complete_absence_never_authorizes_resend_or_terminal_absence(tmp_path):
    session_factory = create_session_factory(tmp_path / "entry-absent.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.checked == 1
    assert result.unchanged == 1
    assert result.confirmed == 0
    assert result.conflicts == 0
    with session_factory() as session:
        persisted = session.get(DeepcoinExecutionOperation, operation.id)
    assert persisted.state == "entry_pending_readback"
    assert persisted.writer_attempted_at == NOW.replace(tzinfo=None)

    repeated = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_snapshot(),
        reconciled_at=NOW + timedelta(seconds=2),
    )
    assert repeated.checked == 1
    assert repeated.unchanged == 1
    with session_factory() as session:
        assert session.query(DeepcoinSnapshotEvidence).count() == 1


def test_entry_without_durable_sent_attempt_fails_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "entry-no-attempt.db")
    signal = _signal(session_factory)
    operation = _entry_operation(
        session_factory,
        signal_id=signal.id,
        record_post_attempt=False,
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_entry_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.conflicts == 1
    with session_factory() as session:
        persisted = session.get(DeepcoinExecutionOperation, operation.id)
    assert persisted.state == "recovery_required"
    assert persisted.reason_code == (
        "protected_entry_reconciliation_identity_conflict"
    )


def test_entry_readback_state_phase_and_certainty_must_be_canonical(tmp_path):
    session_factory = create_session_factory(tmp_path / "entry-state-pair.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    with session_factory() as session:
        row = session.get(DeepcoinExecutionOperation, operation.id)
        row.phase = "protection_readback"
        row.outcome_certainty = "not_sent"
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_entry_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "recovery_required"
        )


def test_entry_pending_state_must_match_accepted_post_fact(tmp_path):
    session_factory = create_session_factory(tmp_path / "entry-attempt-pair.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    with session_factory() as session:
        attempt = session.query(DeepcoinRequestAttempt).one()
        attempt.outcome_certainty = "unknown"
        attempt.error_category = "transport_timeout"
        attempt.http_status = None
        attempt.business_code = None
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_entry_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "recovery_required"
        )


def test_projection_cas_loss_does_not_hide_confirmed_entry(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "projection-cas.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    monkeypatch.setattr(
        reconciliation_module,
        "_project_trade_signal",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            reconciliation_module.TradeSignalTransitionError("cas_lost")
        ),
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_entry_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 1
    assert result.unchanged == 0
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "entry_confirmed"
        )


def test_projection_cas_loss_does_not_hide_identity_conflict(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "conflict-cas.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    exact = _exact_entry_snapshot()
    conflicting_order = dict(exact.order_history[0])
    conflicting_order["posSide"] = "short"
    snapshot = _snapshot(
        positions=exact.positions,
        order_history=[conflicting_order],
    )
    monkeypatch.setattr(
        reconciliation_module,
        "_project_trade_signal",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            reconciliation_module.TradeSignalTransitionError("cas_lost")
        ),
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.conflicts == 1
    assert result.unchanged == 0
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "recovery_required"
        )


def test_snapshot_captured_before_entry_post_cannot_confirm(tmp_path):
    session_factory = create_session_factory(tmp_path / "entry-stale-snapshot.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    snapshot = _exact_entry_snapshot()
    snapshot.capture_started_at = NOW - timedelta(days=1)
    snapshot.capture_ended_at = NOW - timedelta(days=1) + timedelta(seconds=1)

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "recovery_required"
        )


def test_future_snapshot_capture_window_never_confirms_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "future-snapshot.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    snapshot = _exact_entry_snapshot()
    snapshot.capture_started_at = datetime(2099, 1, 1, tzinfo=UTC)
    snapshot.capture_ended_at = datetime(2099, 1, 1, 0, 0, 1, tzinfo=UTC)

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.unchanged == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "entry_pending_readback"
        )


def test_fresh_snapshot_never_reuses_pre_writer_durable_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "stale-dedup.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    fresh = _exact_entry_snapshot()
    proof = reconciliation_module._snapshot_proof(
        fresh,
        captured_at=NOW + timedelta(seconds=1),
    )
    reconciliation_module.record_snapshot_evidence(
        session_factory,
        operation_id=operation.id,
        expected_operation_key=operation.operation_key,
        snapshot_kind="account_composite",
        available=True,
        schema_valid=True,
        complete=True,
        row_count=proof.row_count,
        page_count=1,
        collection_fingerprint=proof.fingerprint,
        start_write_generation=proof.start_generation,
        end_write_generation=proof.end_generation,
        capture_started_at=NOW - timedelta(days=1),
        capture_ended_at=NOW - timedelta(days=1) + timedelta(seconds=1),
        evidence={"source": "shared_account_reconciliation"},
        error_category=None,
        error_code=None,
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=fresh,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 1
    with session_factory() as session:
        persisted = session.get(DeepcoinExecutionOperation, operation.id)
        snapshots = (
            session.query(DeepcoinSnapshotEvidence)
            .order_by(DeepcoinSnapshotEvidence.ordinal)
            .all()
        )
    assert len(snapshots) == 2
    evidence = json.loads(persisted.evidence_json)
    assert evidence["reconciliation_snapshot_id"] == snapshots[1].id
    assert snapshots[1].capture_started_at == fresh.capture_started_at.replace(
        tzinfo=None
    )


def test_client_identity_collision_moves_entry_to_recovery_required(tmp_path):
    session_factory = create_session_factory(tmp_path / "entry-conflict.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    exact = _exact_entry_snapshot()
    collision = _snapshot(
        positions=exact.positions,
        order_history=[
            *exact.order_history,
            {
                "clOrdId": "client-entry-1",
                "instId": "BTC-USDT-SWAP",
                "ordId": "different-order",
                "posId": "position-1",
                "posSide": "long",
                "state": "filled",
            },
        ],
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=collision,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.conflicts == 1
    with session_factory() as session:
        persisted = session.get(DeepcoinExecutionOperation, operation.id)
    assert persisted.state == "recovery_required"
    assert persisted.error_category == "state_conflict"
    assert persisted.reason_code == "protected_entry_reconciliation_identity_conflict"
    assert "different-order" not in persisted.evidence_json


def test_pre_submit_deferred_is_outside_reconciliation_authority(tmp_path):
    session_factory = create_session_factory(tmp_path / "deferred.db")
    signal = _signal(session_factory, status="active_protected_deferred")
    operation = reserve_execution_operation(
        session_factory,
        operation_key=f"protected-entry:v1:signal:{signal.id}:leg:2:entry",
        trade_signal_id=signal.id,
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

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_entry_snapshot(client_order_id="irrelevant"),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.checked == 0
    with session_factory() as session:
        persisted = session.get(DeepcoinExecutionOperation, operation.id)
        assert session.query(DeepcoinSnapshotEvidence).count() == 0
    assert persisted.state == "pre_submit_deferred"
    assert persisted.writer_attempted_at is None


def _protection_operation_bundle(session_factory):
    signal = _signal(session_factory)
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id=signal.strategy_instance_id,
            kol_id="alice",
            chat_id=signal.chat_id,
            message_id=signal.message_id,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="active",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=signal.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="entry-order",
            client_order_id="client-entry-1",
            pos_id="position-1",
            venue="deepcoin",
            status="active",
        )
        session.add(leg)
        session.flush()
        binding_id = binding.id
        leg_id = leg.id
        session.commit()
    parent = reserve_execution_operation(
        session_factory,
        operation_key=f"protected-entry:v1:signal:{signal.id}:leg:1:entry",
        trade_signal_id=signal.id,
        contract_version="1",
        phase="protection_readback",
        state="recovery_required",
        outcome_certainty="unknown",
        reason_code="protection_incomplete",
        request_fingerprint=_canonical_fingerprint(_market_request()),
        economics_fingerprint=_canonical_fingerprint(
            {
                "client_order_id": "client-entry-1",
                "instrument_id": "BTC-USDT-SWAP",
                "leg_index": 1,
                "position_side": "long",
                "quantity": "1",
                "side": "buy",
            }
        ),
        deadline_at=NOW + timedelta(seconds=10),
        writer_attempted_at=NOW,
        evidence={
            "client_order_ref": hashlib.sha256(b"client-entry-1").hexdigest(),
            "confirmed_protection_count": 0,
            "expected_entry_leg_indices": [1],
            "leg_index": 1,
            "pre_submit_position_refs": [],
            "required_protection_count": 1,
            "uid_scope_hash": UID_SCOPE_HASH,
        },
        created_at=NOW,
    )
    record_request_attempt(
        session_factory,
        operation_id=parent.id,
        expected_operation_key=parent.operation_key,
        expected_request_fingerprint=parent.request_fingerprint,
        uid_scope_hash=UID_SCOPE_HASH,
        fact=RequestAttemptFact(
            ordinal=1,
            method="POST",
            normalized_path="/deepcoin/trade/order",
            phase="entry_submit",
            priority=RequestPriority.CRITICAL,
            correlation_id="protected-entry-parent",
            outcome_certainty=OutcomeCertainty.ACCEPTED,
            error_category=None,
            safe_code="entry_submission_accepted",
            http_status=200,
            business_code="0",
            governor_wait_ms=0,
            retry_delay_ms=0,
            latency_ms=1,
        ),
        started_at=NOW,
        completed_at=NOW + timedelta(milliseconds=1),
    )
    request_payload = {
        "instId": "BTC-USDT-SWAP",
        "posId": "position-1",
        "posSide": "long",
        "slTriggerPx": "60000",
        "sz": "1",
    }
    request_fingerprint = _canonical_fingerprint(request_payload)
    base_authority = "7" * 64
    authority_fingerprint = bound_set_position_authority_fingerprint(
        base_authority_fingerprint=base_authority,
        ledger_purpose="stop_loss",
        pre_submit_order_refs=[],
    )
    request = {
        **request_payload,
        "_base_authority_fingerprint": base_authority,
        "_ledger_purpose": "stop_loss",
        "_pre_submit_order_refs": [],
    }
    with session_factory() as session:
        intent = PositionMutationIntent(
            idempotency_key=f"protected-entry:{signal.id}:1:set:0",
            venue="deepcoin",
            operation="set_position_sltp",
            strategy_instance_id=signal.strategy_instance_id,
            execution_binding_id=binding_id,
            execution_order_leg_id=leg_id,
            pos_id="position-1",
            order_id="protection-order-1",
            authority_fingerprint=authority_fingerprint,
            request_fingerprint=request_fingerprint,
            status="confirmed",
            request_json=json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            response_json=(
                '{"code":"0","data":{"ordId":"protection-order-1"}}'
            ),
            reserved_at=NOW.replace(tzinfo=None),
            submitted_at=NOW.replace(tzinfo=None),
            confirmed_at=NOW.replace(tzinfo=None),
        )
        session.add(intent)
        session.flush()
        child = DeepcoinExecutionOperation(
            operation_key=(
                f"protected-entry:v1:signal:{signal.id}:leg:1:protection:0"
            ),
            trade_signal_id=signal.id,
            parent_operation_id=parent.id,
            execution_binding_id=binding_id,
            execution_order_leg_id=leg_id,
            contract_version="1",
            phase="protection_readback",
            state="protection_pending_readback",
            outcome_certainty="accepted",
            reason_code="protection_readback_pending",
            request_fingerprint=request_fingerprint,
            economics_fingerprint=_canonical_fingerprint(
                {
                    "pos_id": "position-1",
                    "purpose": "stop_loss",
                    "size": "1",
                    "trigger_price": "60000",
                }
            ),
            deadline_at=(NOW + timedelta(seconds=10)).replace(tzinfo=None),
            writer_attempted_at=NOW.replace(tzinfo=None),
            attempt_count=0,
            state_version=0,
            evidence_json=json.dumps(
                {
                    "position_mutation_intent_id": intent.id,
                    "protection_index": 0,
                    "required_protection_count": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        session.add(child)
        session.flush()
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=leg_id,
                strategy_instance_id=signal.strategy_instance_id,
                pos_id="position-1",
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="protection-order-1",
                purpose="stop_loss",
                trigger_price="60000",
                size_text="1",
                status="verified",
                evidence_source="position_mutation_intent_readback",
                evidence_json='{"intent_id":1}',
            )
        )
        session.commit()
        child_id = child.id
    record_request_attempt(
        session_factory,
        operation_id=child_id,
        expected_operation_key=(
            f"protected-entry:v1:signal:{signal.id}:leg:1:protection:0"
        ),
        expected_request_fingerprint=request_fingerprint,
        uid_scope_hash=UID_SCOPE_HASH,
        fact=RequestAttemptFact(
            ordinal=1,
            method="POST",
            normalized_path="/deepcoin/trade/set-position-sltp",
            phase="protection_submit",
            priority=RequestPriority.CRITICAL,
            correlation_id="protected-entry-protection",
            outcome_certainty=OutcomeCertainty.ACCEPTED,
            error_category=None,
            safe_code="position_sltp_submission_accepted",
            http_status=200,
            business_code="0",
            governor_wait_ms=0,
            retry_delay_ms=0,
            latency_ms=1,
        ),
        started_at=NOW + timedelta(milliseconds=2),
        completed_at=NOW + timedelta(milliseconds=3),
    )
    for offset in range(4):
        advance_account_write_generation(
            session_factory,
            uid_scope_hash=UID_SCOPE_HASH,
            updated_at=NOW + timedelta(milliseconds=offset),
        )
    return signal, parent, child_id


def test_protection_pending_confirms_exact_intent_and_parent_idempotently(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "protection-confirm.db")
    signal, parent, child_id = _protection_operation_bundle(session_factory)
    snapshot = _snapshot(
        start_generation=4,
        end_generation=4,
        positions=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "position-1",
                "posSide": "long",
                "pos": "1",
            }
        ],
        pending_trigger_orders=[
            {
                "instId": "BTC-USDT-SWAP",
                "ordId": "protection-order-1",
                "posId": "position-1",
                "posSide": "long",
                "slTriggerPx": "60000",
                "sz": "1",
            }
        ],
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.checked == 1
    assert result.confirmed == 1
    assert result.conflicts == 0
    with session_factory() as session:
        child = session.get(DeepcoinExecutionOperation, child_id)
        persisted_parent = session.get(DeepcoinExecutionOperation, parent.id)
        persisted_signal = session.get(TradeSignal, signal.id)
    assert child.state == "protected"
    assert child.completed_at == (NOW + timedelta(seconds=1)).replace(tzinfo=None)
    assert persisted_parent.state == "protected"
    assert persisted_signal.status == "submitted"

    repeated = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=2),
    )
    assert repeated.checked == 0
    assert repeated.confirmed == 0


def test_incomplete_composite_collection_never_authorizes_confirmation(tmp_path):
    session_factory = create_session_factory(tmp_path / "incomplete-authority.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    snapshot = _exact_entry_snapshot()
    collection = snapshot.account_authority.collections[0]
    snapshot.account_authority = replace(
        snapshot.account_authority,
        collections=(
            replace(
                collection,
                complete=False,
                reason_code="snapshot_pagination_incomplete",
            ),
        ),
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.unchanged == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "entry_pending_readback"
        )


def test_protection_requires_exact_entry_leg_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-leg.db")
    _signal_row, parent, child_id = _protection_operation_bundle(session_factory)
    with session_factory() as session:
        child = session.get(DeepcoinExecutionOperation, child_id)
        leg = session.get(ExecutionOrderLeg, child.execution_order_leg_id)
        leg.purpose = "take_profit"
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_protection_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, child_id).state == (
            "recovery_required"
        )
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "recovery_required"
        )


def test_protection_cannot_confirm_without_exact_live_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-no-position.db")
    _signal_row, parent, child_id = _protection_operation_bundle(session_factory)
    snapshot = _exact_protection_snapshot()
    snapshot = _snapshot(
        start_generation=4,
        end_generation=4,
        pending_trigger_orders=snapshot.pending_trigger_orders,
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.unchanged == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, child_id).state == (
            "protection_pending_readback"
        )
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "recovery_required"
        )


def test_protection_requires_live_position_size_to_match_request(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-size.db")
    _signal_row, parent, child_id = _protection_operation_bundle(session_factory)
    snapshot = _exact_protection_snapshot()
    snapshot = _snapshot(
        start_generation=4,
        end_generation=4,
        positions=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "position-1",
                "posSide": "long",
                "pos": "0.1",
            }
        ],
        pending_trigger_orders=snapshot.pending_trigger_orders,
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, child_id).state == (
            "recovery_required"
        )
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "recovery_required"
        )


def test_protection_response_must_have_one_unique_order_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "response-identity.db")
    _signal_row, parent, child_id = _protection_operation_bundle(session_factory)
    with session_factory() as session:
        child = session.get(DeepcoinExecutionOperation, child_id)
        evidence = json.loads(child.evidence_json)
        intent = session.get(
            PositionMutationIntent,
            evidence["position_mutation_intent_id"],
        )
        intent.response_json = json.dumps(
            {
                "code": "0",
                "data": [
                    {"ordId": "protection-order-1"},
                    {"ordId": "attacker-order"},
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_protection_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, child_id).state == (
            "recovery_required"
        )
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "recovery_required"
        )


def test_protection_readback_state_phase_and_certainty_must_be_canonical(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "protection-state-pair.db")
    _signal_row, parent, child_id = _protection_operation_bundle(session_factory)
    with session_factory() as session:
        child = session.get(DeepcoinExecutionOperation, child_id)
        child.phase = "entry_readback"
        child.outcome_certainty = "not_sent"
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_protection_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, child_id).state == (
            "recovery_required"
        )
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "recovery_required"
        )


def test_protection_readback_certainty_must_match_pending_state(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-certainty.db")
    _signal_row, parent, child_id = _protection_operation_bundle(session_factory)
    with session_factory() as session:
        child = session.get(DeepcoinExecutionOperation, child_id)
        child.outcome_certainty = "unknown"
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_protection_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, child_id).state == (
            "recovery_required"
        )
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "recovery_required"
        )


def test_snapshot_captured_before_protection_post_cannot_confirm(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-stale.db")
    _signal_row, parent, child_id = _protection_operation_bundle(session_factory)
    snapshot = _exact_protection_snapshot()
    snapshot.capture_started_at = NOW + timedelta(milliseconds=1)
    snapshot.capture_ended_at = NOW + timedelta(milliseconds=2)

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, child_id).state == (
            "recovery_required"
        )
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "recovery_required"
        )


def test_rejected_parent_cannot_authorize_protection_confirmation(tmp_path):
    session_factory = create_session_factory(tmp_path / "rejected-parent.db")
    signal, parent, child_id = _protection_operation_bundle(session_factory)
    with session_factory() as session:
        parent_row = session.get(DeepcoinExecutionOperation, parent.id)
        parent_row.phase = "entry_readback"
        parent_row.state = "entry_rejected"
        parent_row.outcome_certainty = "rejected"
        parent_row.reason_code = "entry_submission_rejected"
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_protection_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, child_id).state == (
            "recovery_required"
        )
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "recovery_required"
        )
        assert session.get(TradeSignal, signal.id).status == "recovery_required"


def test_parent_never_trusts_forged_protected_sibling(tmp_path):
    session_factory = create_session_factory(tmp_path / "forged-sibling.db")
    signal, parent, _child_id = _protection_operation_bundle(session_factory)
    with session_factory() as session:
        parent_row = session.get(DeepcoinExecutionOperation, parent.id)
        parent_evidence = json.loads(parent_row.evidence_json)
        parent_evidence["required_protection_count"] = 2
        parent_row.evidence_json = json.dumps(
            parent_evidence,
            sort_keys=True,
            separators=(",", ":"),
        )
        legitimate = (
            session.query(DeepcoinExecutionOperation)
            .filter(
                DeepcoinExecutionOperation.parent_operation_id == parent.id
            )
            .one()
        )
        session.add(
            DeepcoinExecutionOperation(
                operation_key=(
                    f"protected-entry:v1:signal:{signal.id}:leg:1:protection:1"
                ),
                trade_signal_id=signal.id,
                parent_operation_id=parent.id,
                execution_binding_id=legitimate.execution_binding_id,
                execution_order_leg_id=legitimate.execution_order_leg_id,
                contract_version="1",
                phase="protection_readback",
                state="protected",
                outcome_certainty="confirmed",
                reason_code="protection_fully_confirmed",
                request_fingerprint="f" * 64,
                economics_fingerprint="d" * 64,
                deadline_at=(NOW + timedelta(seconds=10)).replace(tzinfo=None),
                writer_attempted_at=NOW.replace(tzinfo=None),
                completed_at=NOW.replace(tzinfo=None),
                attempt_count=1,
                state_version=1,
                evidence_json=(
                    '{"position_mutation_intent_id":999999,'
                    '"protection_index":1,"required_protection_count":2}'
                ),
                created_at=NOW.replace(tzinfo=None),
                updated_at=NOW.replace(tzinfo=None),
            )
        )
        session.commit()
    snapshot = _snapshot(
        start_generation=4,
        end_generation=4,
        positions=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "position-1",
                "posSide": "long",
                "pos": "1",
            }
        ],
        pending_trigger_orders=[
            {
                "instId": "BTC-USDT-SWAP",
                "ordId": "protection-order-1",
                "posId": "position-1",
                "posSide": "long",
                "slTriggerPx": "60000",
                "sz": "1",
            }
        ],
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1
    with session_factory() as session:
        persisted_parent = session.get(DeepcoinExecutionOperation, parent.id)
        persisted_signal = session.get(TradeSignal, signal.id)
    assert persisted_parent.state == "recovery_required"
    assert persisted_signal.status == "recovery_required"


def test_protection_child_key_must_match_parent_and_intent_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-key.db")
    _signal_row, _parent, child_id = _protection_operation_bundle(session_factory)
    with session_factory() as session:
        child = session.get(DeepcoinExecutionOperation, child_id)
        child.operation_key = "protected-entry:v1:signal:999:leg:9:protection:0"
        session.commit()
    snapshot = _snapshot(
        start_generation=4,
        end_generation=4,
        positions=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "position-1",
                "posSide": "long",
                "pos": "1",
            }
        ],
        pending_trigger_orders=[
            {
                "instId": "BTC-USDT-SWAP",
                "ordId": "protection-order-1",
                "posId": "position-1",
                "posSide": "long",
                "slTriggerPx": "60000",
                "sz": "1",
            }
        ],
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.conflicts == 1
    with session_factory() as session:
        persisted = session.get(DeepcoinExecutionOperation, child_id)
    assert persisted.state == "recovery_required"


def test_wrong_account_scope_freezes_operation_without_using_snapshot(tmp_path):
    session_factory = create_session_factory(tmp_path / "wrong-scope.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_snapshot(uid_scope_hash="8" * 64),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.conflicts == 1
    with session_factory() as session:
        persisted = session.get(DeepcoinExecutionOperation, operation.id)
        snapshot_evidence = session.query(DeepcoinSnapshotEvidence).one()
    assert persisted.state == "recovery_required"
    assert persisted.reason_code == "protected_entry_reconciliation_scope_conflict"
    assert snapshot_evidence.complete is False
    assert snapshot_evidence.error_code == (
        "protected_entry_snapshot_scope_conflict"
    )


def test_malformed_operation_evidence_freezes_instead_of_silent_skip(tmp_path):
    session_factory = create_session_factory(tmp_path / "malformed-operation.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    with session_factory() as session:
        session.get(DeepcoinExecutionOperation, operation.id).evidence_json = "{"
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_entry_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.conflicts == 1
    with session_factory() as session:
        persisted = session.get(DeepcoinExecutionOperation, operation.id)
    assert persisted.state == "recovery_required"
    assert persisted.reason_code == "protected_entry_reconciliation_evidence_invalid"
    evidence = json.loads(persisted.evidence_json)
    assert evidence == {
        "next_action": "supervision_only",
        "reconciliation_error": "operation_evidence_invalid",
        "reconciliation_snapshot_id": 1,
    }


def test_credential_shaped_operation_evidence_is_replaced_by_safe_conflict(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "secret-operation.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    with session_factory() as session:
        row = session.get(DeepcoinExecutionOperation, operation.id)
        evidence = json.loads(row.evidence_json)
        evidence["note"] = "Authorization: Bearer TOPSECRET"
        row.evidence_json = json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
        )
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_entry_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.conflicts == 1
    with session_factory() as session:
        persisted = session.get(DeepcoinExecutionOperation, operation.id)
    assert persisted.state == "recovery_required"
    assert "TOPSECRET" not in persisted.evidence_json
    assert "Authorization" not in persisted.evidence_json


def test_tampered_client_reference_cannot_bypass_economics_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "client-ref-tamper.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    with session_factory() as session:
        row = session.get(DeepcoinExecutionOperation, operation.id)
        evidence = json.loads(row.evidence_json)
        evidence["client_order_ref"] = hashlib.sha256(
            b"attacker-client"
        ).hexdigest()
        row.evidence_json = json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
        )
        session.commit()
    snapshot = _exact_entry_snapshot(client_order_id="attacker-client")

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.conflicts == 1
    with session_factory() as session:
        persisted = session.get(DeepcoinExecutionOperation, operation.id)
    assert persisted.state == "recovery_required"
    assert persisted.reason_code == (
        "protected_entry_reconciliation_identity_conflict"
    )


def test_shared_snapshot_capture_and_reconcile_never_exposes_writer_api(tmp_path):
    session_factory = create_session_factory(tmp_path / "read-only-client.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)

    class ReadOnlyGuardClient:
        uid_scope_hash = UID_SCOPE_HASH

        def __init__(self):
            self.reads = []

        def read_positions(self):
            self.reads.append("positions")
            return {
                "data": [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "posId": "position-1",
                        "posSide": "long",
                        "pos": "1",
                    }
                ]
            }

        def read_open_orders(self):
            self.reads.append("open_orders")
            return {"data": []}

        def read_position_history(self, *, inst_id):
            self.reads.append("position_history")
            return {"data": []}

        def read_trigger_orders_pending(self, *, inst_id):
            self.reads.append("pending_trigger_orders")
            return {"data": []}

        def read_order_history(self, *, inst_id):
            self.reads.append("order_history")
            return {
                "data": [
                    {
                        "clOrdId": "client-entry-1",
                        "instId": "BTC-USDT-SWAP",
                        "ordId": "order-1",
                        "posId": "position-1",
                        "posSide": "long",
                        "side": "buy",
                        "state": "filled",
                        "sz": "1",
                    }
                ]
            }

        def read_trade_fills(self, *, inst_id):
            self.reads.append("trade_fills")
            return {"data": []}

        def read_trigger_order_history(self, *, inst_id):
            self.reads.append("trigger_history")
            return {"data": []}

        def place_order(self, payload):
            raise AssertionError("writer must not be called")

        def trigger_order(self, payload):
            raise AssertionError("writer must not be called")

        def set_position_sltp(self, payload):
            raise AssertionError("writer must not be called")

    client = ReadOnlyGuardClient()
    snapshot = load_deepcoin_reconciliation_snapshot_for_instruments_read_only(
        session_factory,
        client=client,
        instruments={"BTC-USDT-SWAP"},
    )
    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 1
    assert set(client.reads) == {
        "positions",
        "open_orders",
        "position_history",
        "pending_trigger_orders",
        "order_history",
        "trade_fills",
        "trigger_history",
    }
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "entry_confirmed"
        )


def test_generation_drift_after_capture_never_confirms_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "generation-drift.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    for offset in range(4):
        advance_account_write_generation(
            session_factory,
            uid_scope_hash=UID_SCOPE_HASH,
            updated_at=NOW + timedelta(milliseconds=offset),
        )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_entry_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.unchanged == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "entry_pending_readback"
        )


def test_missing_generation_authority_never_confirms_sent_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "generation-missing.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    with session_factory() as session:
        session.query(DeepcoinAccountWriteGeneration).delete()
        session.commit()

    exact = _exact_entry_snapshot()
    zero_generation = _snapshot(
        positions=exact.positions,
        order_history=exact.order_history,
        start_generation=0,
        end_generation=0,
    )
    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=zero_generation,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.unchanged == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "entry_pending_readback"
        )


def test_partial_pending_entry_row_stays_pending_instead_of_conflict(tmp_path):
    session_factory = create_session_factory(tmp_path / "partial-pending.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    snapshot = _snapshot(
        open_orders=[
            {
                "clOrdId": "client-entry-1",
                "instId": "BTC-USDT-SWAP",
                "ordId": "order-1",
                "posSide": "long",
                "side": "buy",
                "state": "live",
                "sz": "1",
            }
        ]
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.unchanged == 1
    assert result.conflicts == 0
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "entry_pending_readback"
        )


def test_partial_position_identity_stays_pending_instead_of_conflict(tmp_path):
    session_factory = create_session_factory(tmp_path / "partial-position.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    exact = _exact_entry_snapshot()
    snapshot = _snapshot(
        positions=[{"posId": "position-1"}],
        order_history=exact.order_history,
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.unchanged == 1
    assert result.conflicts == 0
    assert result.confirmed == 0
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "entry_pending_readback"
        )


def test_split_fills_are_aggregated_before_confirming_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "split-fills.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_snapshot(
            positions=[
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "position-1",
                    "posSide": "long",
                    "pos": "1",
                }
            ],
            trade_fills=[
                {
                    "clOrdId": "client-entry-1",
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-1",
                    "posId": "position-1",
                    "posSide": "long",
                    "side": "buy",
                    "fillSz": "0.5",
                },
                {
                    "clOrdId": "client-entry-1",
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-1",
                    "posId": "position-1",
                    "posSide": "long",
                    "side": "buy",
                    "fillSz": "0.5",
                },
            ],
        ),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 1
    assert result.conflicts == 0
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "entry_confirmed"
        )


def test_live_partial_fill_never_confirms_complete_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "partial-fill.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    snapshot = _snapshot(
        positions=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "position-1",
                "posSide": "long",
                "pos": "0.1",
            }
        ],
        open_orders=[
            {
                "clOrdId": "client-entry-1",
                "instId": "BTC-USDT-SWAP",
                "ordId": "order-1",
                "posId": "position-1",
                "posSide": "long",
                "side": "buy",
                "state": "live",
                "sz": "1",
            }
        ],
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.unchanged == 1
    assert result.confirmed == 0
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "entry_pending_readback"
        )


def test_entry_attempt_wrong_writer_endpoint_fails_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "wrong-endpoint.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    with session_factory() as session:
        session.query(DeepcoinRequestAttempt).filter_by(
            deepcoin_execution_operation_id=operation.id
        ).one().normalized_path = "/totally-wrong-writer"
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_entry_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "recovery_required"
        )


def test_entry_identity_is_rebuilt_from_queued_request_not_mutable_columns(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "entry-authority.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    attacker_client = "attacker-client"
    with session_factory() as session:
        row = session.get(DeepcoinExecutionOperation, operation.id)
        evidence = json.loads(row.evidence_json)
        evidence["client_order_ref"] = hashlib.sha256(
            attacker_client.encode("utf-8")
        ).hexdigest()
        row.evidence_json = json.dumps(
            evidence, sort_keys=True, separators=(",", ":")
        )
        row.economics_fingerprint = _canonical_fingerprint(
            {
                "client_order_id": attacker_client,
                "instrument_id": "BTC-USDT-SWAP",
                "leg_index": 1,
                "position_side": "long",
                "quantity": "1",
                "side": "buy",
            }
        )
        session.commit()
    attacker_snapshot = _snapshot(
        positions=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "attacker-position",
                "posSide": "long",
                "pos": "1",
            }
        ],
        order_history=[
            {
                "clOrdId": attacker_client,
                "instId": "BTC-USDT-SWAP",
                "ordId": "attacker-order",
                "posId": "attacker-position",
                "posSide": "long",
                "side": "buy",
                "state": "filled",
                "sz": "1",
            }
        ],
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=attacker_snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "recovery_required"
        )


def test_snapshot_rows_must_match_account_composite_authority(tmp_path):
    session_factory = create_session_factory(tmp_path / "snapshot-binding.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    snapshot = _exact_entry_snapshot()
    snapshot.order_history[0]["ordId"] = "row-mutated-after-authority"

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=snapshot,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.unchanged == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "entry_pending_readback"
        )


def test_identity_conflict_projects_compatibility_signal_to_recovery(tmp_path):
    session_factory = create_session_factory(tmp_path / "conflict-projection.db")
    signal = _signal(session_factory, status="active_protection_pending")
    operation = _entry_operation(session_factory, signal_id=signal.id)
    conflict = _snapshot(
        positions=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "position-1",
                "posSide": "long",
                "pos": "1",
            }
        ],
        order_history=[
            {
                "clOrdId": "client-entry-1",
                "instId": "BTC-USDT-SWAP",
                "ordId": "order-1",
                "posId": "position-1",
                "posSide": "short",
                "side": "buy",
                "state": "filled",
                "sz": "1",
            }
        ],
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=conflict,
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "recovery_required"
        )
        assert session.get(TradeSignal, signal.id).status == "recovery_required"


def test_terminal_signal_projection_never_regresses(tmp_path):
    session_factory = create_session_factory(tmp_path / "terminal-signal.db")
    signal = _signal(session_factory, status="submitted")
    _entry_operation(session_factory, signal_id=signal.id)

    reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_entry_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    with session_factory() as session:
        assert session.get(TradeSignal, signal.id).status == "submitted"


def test_terminal_operation_projection_replays_after_crash(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "projection-replay.db")
    signal = _signal(session_factory)
    operation = _entry_operation(session_factory, signal_id=signal.id)
    original = reconciliation_module._project_trade_signal
    monkeypatch.setattr(
        reconciliation_module,
        "_project_trade_signal",
        lambda *args, **kwargs: (_ for _ in ()).throw(BaseException("crash")),
    )
    with pytest.raises(BaseException, match="crash"):
        reconcile_protected_entry_operations(
            session_factory,
            snapshot=_exact_entry_snapshot(),
            reconciled_at=NOW + timedelta(seconds=1),
        )
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, operation.id).state == (
            "entry_confirmed"
        )
        assert session.get(TradeSignal, signal.id).status == "recovery_required"

    monkeypatch.setattr(reconciliation_module, "_project_trade_signal", original)
    reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_entry_snapshot(),
        reconciled_at=NOW + timedelta(seconds=2),
    )

    with session_factory() as session:
        assert session.get(TradeSignal, signal.id).status == (
            "active_protection_pending"
        )


def test_protected_child_replays_parent_aggregate_after_crash(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "parent-replay.db")
    signal, parent, child_id = _protection_operation_bundle(session_factory)
    original = reconciliation_module._confirm_parent_protection_aggregate
    monkeypatch.setattr(
        reconciliation_module,
        "_confirm_parent_protection_aggregate",
        lambda *args, **kwargs: (_ for _ in ()).throw(BaseException("crash")),
    )
    with pytest.raises(BaseException, match="crash"):
        reconcile_protected_entry_operations(
            session_factory,
            snapshot=_exact_protection_snapshot(),
            reconciled_at=NOW + timedelta(seconds=1),
        )
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, child_id).state == "protected"
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "recovery_required"
        )

    monkeypatch.setattr(
        reconciliation_module,
        "_confirm_parent_protection_aggregate",
        original,
    )
    reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_protection_snapshot(),
        reconciled_at=NOW + timedelta(seconds=2),
    )

    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, parent.id).state == "protected"
        assert session.get(TradeSignal, signal.id).status == "submitted"


def test_protected_child_replay_rejects_cross_uid_snapshot(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "parent-cross-uid.db")
    signal, parent, child_id = _protection_operation_bundle(session_factory)
    original = reconciliation_module._confirm_parent_protection_aggregate
    monkeypatch.setattr(
        reconciliation_module,
        "_confirm_parent_protection_aggregate",
        lambda *args, **kwargs: (_ for _ in ()).throw(BaseException("crash")),
    )
    with pytest.raises(BaseException, match="crash"):
        reconcile_protected_entry_operations(
            session_factory,
            snapshot=_exact_protection_snapshot(),
            reconciled_at=NOW + timedelta(seconds=1),
        )
    other_uid = "8" * 64
    for offset in range(4):
        advance_account_write_generation(
            session_factory,
            uid_scope_hash=other_uid,
            updated_at=NOW + timedelta(milliseconds=offset),
        )
    monkeypatch.setattr(
        reconciliation_module,
        "_confirm_parent_protection_aggregate",
        original,
    )
    cross_uid_snapshot = _exact_protection_snapshot()
    cross_uid_snapshot.account_authority = AccountSnapshotEvidence(
        uid_scope_hash=other_uid,
        start_write_generation=4,
        end_write_generation=4,
        collections=cross_uid_snapshot.account_authority.collections,
        complete=True,
        reason_code=None,
    )

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=cross_uid_snapshot,
        reconciled_at=NOW + timedelta(seconds=2),
    )

    assert result.confirmed == 0
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, child_id).state == "protected"
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "recovery_required"
        )
        assert session.get(TradeSignal, signal.id).status == "recovery_required"


def test_completed_protection_history_is_not_scanned_for_parent_replay(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "bounded-parent-replay.db")
    _signal_row, _parent, _child_id = _protection_operation_bundle(session_factory)
    reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_protection_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )
    calls = []
    monkeypatch.setattr(
        reconciliation_module,
        "_confirm_parent_protection_aggregate",
        lambda *args, **kwargs: calls.append(kwargs["parent_operation_id"]),
    )

    repeated = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_protection_snapshot(),
        reconciled_at=NOW + timedelta(seconds=2),
    )

    assert repeated.checked == 0
    assert calls == []


def test_invalid_projection_rows_cannot_starve_later_valid_signal(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "projection-fairness.db")
    valid_signal_id = None
    for index in range(3):
        signal = _signal(session_factory, message_id=55 + index)
        operation = _entry_operation(session_factory, signal_id=signal.id)
        with session_factory() as session:
            row = session.get(DeepcoinExecutionOperation, operation.id)
            row.state = "entry_confirmed"
            row.outcome_certainty = "confirmed"
            row.reason_code = "entry_readback_confirmed"
            session.commit()
        if index < 2:
            with session_factory() as session:
                row = session.get(DeepcoinExecutionOperation, operation.id)
                row.operation_key = f"corrupt-projection-key-{index}"
                session.commit()
        else:
            valid_signal_id = signal.id
    monkeypatch.setattr(reconciliation_module, "_MAX_OPERATIONS_PER_CYCLE", 2)

    for offset in range(3):
            reconcile_protected_entry_operations(
                session_factory,
                snapshot=_snapshot(errors={"positions": "unavailable"}),
                reconciled_at=(
                    NOW + timedelta(days=1, seconds=offset + 1)
                ),
            )

    with session_factory() as session:
        assert session.get(TradeSignal, valid_signal_id).status == (
            "active_protection_pending"
        )


def test_invalid_parent_aggregates_cannot_starve_later_replay(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "aggregate-fairness.db")
    parent_ids = []
    for index in range(3):
        signal = _signal(session_factory, message_id=80 + index)
        parent = reserve_execution_operation(
            session_factory,
            operation_key=(
                f"protected-entry:v1:signal:{signal.id}:leg:1:entry"
            ),
            trade_signal_id=signal.id,
            contract_version="1",
            phase="protection_readback",
            state="recovery_required",
            outcome_certainty="unknown",
            reason_code="protection_incomplete",
            request_fingerprint="a" * 64,
            economics_fingerprint="b" * 64,
            deadline_at=NOW + timedelta(seconds=10),
            writer_attempted_at=NOW,
            evidence={
                "required_protection_count": 1,
                "uid_scope_hash": UID_SCOPE_HASH,
            },
            created_at=NOW,
        )
        with session_factory() as session:
            session.add(
                DeepcoinExecutionOperation(
                    operation_key=(
                        f"protected-entry:v1:signal:{signal.id}:"
                        "leg:1:protection:0"
                    ),
                    trade_signal_id=signal.id,
                    parent_operation_id=parent.id,
                    contract_version="1",
                    phase="protection_readback",
                    state="protected",
                    outcome_certainty="confirmed",
                    reason_code="protection_fully_confirmed",
                    request_fingerprint="c" * 64,
                    economics_fingerprint="d" * 64,
                    deadline_at=(NOW + timedelta(seconds=10)).replace(tzinfo=None),
                    writer_attempted_at=NOW.replace(tzinfo=None),
                    completed_at=NOW.replace(tzinfo=None),
                    attempt_count=1,
                    state_version=1,
                    evidence_json=(
                        '{"position_mutation_intent_id":1,'
                        '"protection_index":0,'
                        '"required_protection_count":1}'
                    ),
                    created_at=NOW.replace(tzinfo=None),
                    updated_at=NOW.replace(tzinfo=None),
                )
            )
            session.commit()
        parent_ids.append(parent.id)
    calls = []

    def fake_confirm(*args, **kwargs):
        parent_id = kwargs["parent_operation_id"]
        calls.append(parent_id)
        if parent_id != parent_ids[-1]:
            raise reconciliation_module.DeepcoinOperationConflict("invalid")

    monkeypatch.setattr(reconciliation_module, "_MAX_OPERATIONS_PER_CYCLE", 2)
    monkeypatch.setattr(
        reconciliation_module,
        "_confirm_parent_protection_aggregate",
        fake_confirm,
    )
    for offset in range(2):
        reconcile_protected_entry_operations(
            session_factory,
            snapshot=_exact_protection_snapshot(),
            reconciled_at=NOW + timedelta(days=1, seconds=offset),
        )

    assert parent_ids[-1] in calls


def _exact_protection_snapshot():
    return _snapshot(
        start_generation=4,
        end_generation=4,
        positions=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "position-1",
                "posSide": "long",
                "pos": "1",
            }
        ],
        pending_trigger_orders=[
            {
                "instId": "BTC-USDT-SWAP",
                "ordId": "protection-order-1",
                "posId": "position-1",
                "posSide": "long",
                "slTriggerPx": "60000",
                "sz": "1",
            }
        ],
    )


def test_tampered_parent_identity_freezes_parent_and_child(tmp_path):
    session_factory = create_session_factory(tmp_path / "parent-identity.db")
    signal, parent, child_id = _protection_operation_bundle(session_factory)
    with session_factory() as session:
        session.get(DeepcoinExecutionOperation, parent.id).operation_key = (
            "tampered-parent"
        )
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_protection_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "recovery_required"
        )
        assert session.get(DeepcoinExecutionOperation, child_id).state == (
            "recovery_required"
        )
        assert session.get(TradeSignal, signal.id).status == "recovery_required"


def test_tampered_protection_economics_fails_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-economics.db")
    _signal_row, _parent, child_id = _protection_operation_bundle(session_factory)
    with session_factory() as session:
        session.get(DeepcoinExecutionOperation, child_id).economics_fingerprint = (
            "f" * 64
        )
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_protection_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.conflicts == 1
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, child_id).state == (
            "recovery_required"
        )


def test_protection_conflict_crash_replays_parent_freeze(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "conflict-replay.db")
    signal, parent, child_id = _protection_operation_bundle(session_factory)
    with session_factory() as session:
        parent_row = session.get(DeepcoinExecutionOperation, parent.id)
        parent_row.phase = "protection_submit"
        parent_row.state = "protection_prepared"
        parent_row.outcome_certainty = "confirmed"
        parent_row.reason_code = "protection_intents_prepared"
        child = session.get(DeepcoinExecutionOperation, child_id)
        evidence = json.loads(child.evidence_json)
        evidence["required_protection_count"] = 999
        child.evidence_json = json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
        )
        session.commit()
    original = reconciliation_module._freeze_parent_conflict
    monkeypatch.setattr(
        reconciliation_module,
        "_freeze_parent_conflict",
        lambda *args, **kwargs: (_ for _ in ()).throw(BaseException("crash")),
    )
    with pytest.raises(BaseException, match="crash"):
        reconcile_protected_entry_operations(
            session_factory,
            snapshot=_exact_protection_snapshot(),
            reconciled_at=NOW + timedelta(seconds=1),
        )
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, child_id).state == (
            "recovery_required"
        )
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "protection_prepared"
        )

    monkeypatch.setattr(
        reconciliation_module,
        "_freeze_parent_conflict",
        original,
    )
    repeated = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_protection_snapshot(),
        reconciled_at=NOW + timedelta(seconds=2),
    )

    assert repeated.checked == 0
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "recovery_required"
        )
        assert session.get(TradeSignal, signal.id).status == "recovery_required"


def test_pre_submit_protection_order_is_never_reowned(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-baseline.db")
    _signal_row, _parent, child_id = _protection_operation_bundle(session_factory)
    baseline_ref = hashlib.sha256(
        b"protection_order:protection-order-1"
    ).hexdigest()
    with session_factory() as session:
        child = session.get(DeepcoinExecutionOperation, child_id)
        intent = session.get(
            PositionMutationIntent,
            json.loads(child.evidence_json)["position_mutation_intent_id"],
        )
        request = json.loads(intent.request_json)
        request["_pre_submit_order_refs"] = [baseline_ref]
        intent.request_json = json.dumps(
            request, sort_keys=True, separators=(",", ":")
        )
        intent.authority_fingerprint = bound_set_position_authority_fingerprint(
            base_authority_fingerprint=request["_base_authority_fingerprint"],
            ledger_purpose=request["_ledger_purpose"],
            pre_submit_order_refs=[baseline_ref],
        )
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_exact_protection_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.confirmed == 0
    assert result.conflicts == 1


def test_malformed_parent_evidence_is_recorded_and_frozen(tmp_path):
    session_factory = create_session_factory(tmp_path / "parent-evidence.db")
    _signal_row, parent, child_id = _protection_operation_bundle(session_factory)
    with session_factory() as session:
        session.get(DeepcoinExecutionOperation, parent.id).evidence_json = "{"
        session.commit()

    result = reconcile_protected_entry_operations(
        session_factory,
        snapshot=_snapshot(),
        reconciled_at=NOW + timedelta(seconds=1),
    )

    assert result.conflicts == 1
    assert result.unchanged == 0
    with session_factory() as session:
        assert session.get(DeepcoinExecutionOperation, parent.id).state == (
            "recovery_required"
        )
        assert session.get(DeepcoinExecutionOperation, child_id).state == (
            "recovery_required"
        )
        assert session.query(DeepcoinSnapshotEvidence).count() >= 1


def test_bounded_cycle_eventually_visits_item_129(tmp_path):
    session_factory = create_session_factory(tmp_path / "fair-cycle.db")
    signal = _signal(session_factory)
    for leg_index in range(1, 130):
        reserve_execution_operation(
            session_factory,
            operation_key=(
                f"protected-entry:v1:signal:{signal.id}:leg:{leg_index}:entry"
            ),
            trade_signal_id=signal.id,
            contract_version="1",
            phase="entry_readback",
            state="entry_unknown",
            outcome_certainty="unknown",
            request_fingerprint=_canonical_fingerprint(_market_request()),
            economics_fingerprint="b" * 64,
            deadline_at=NOW + timedelta(seconds=10),
            writer_attempted_at=NOW,
            evidence={
                "client_order_ref": hashlib.sha256(
                    b"client-entry-1"
                ).hexdigest(),
                "expected_entry_leg_indices": [leg_index],
                "leg_index": leg_index,
                "pre_submit_position_refs": [],
                "uid_scope_hash": UID_SCOPE_HASH,
            },
            created_at=NOW,
        )
    incomplete = _snapshot(errors={"positions": "snapshot_read_unavailable"})

    first = reconcile_protected_entry_operations(
        session_factory,
        snapshot=incomplete,
        reconciled_at=NOW + timedelta(seconds=1),
    )
    second = reconcile_protected_entry_operations(
        session_factory,
        snapshot=incomplete,
        reconciled_at=NOW + timedelta(seconds=2),
    )

    assert first.checked == 128
    assert second.checked >= 1
    with session_factory() as session:
        visited = {
            row.deepcoin_execution_operation_id
            for row in session.query(DeepcoinSnapshotEvidence).all()
        }
        final_id = (
            session.query(DeepcoinExecutionOperation.id)
            .order_by(DeepcoinExecutionOperation.id.desc())
            .first()[0]
        )
    assert final_id in visited

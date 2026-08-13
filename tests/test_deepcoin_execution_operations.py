from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import RequestAttemptFact
from telegram_kol_research.deepcoin_execution_operations import (
    DeepcoinEvidenceValidationError,
    DeepcoinOperationConflict,
    advance_account_write_generation,
    load_operation_bundle,
    record_request_attempt,
    record_snapshot_evidence,
    reserve_execution_operation,
    transition_execution_operation,
)
from telegram_kol_research.deepcoin_request_policy import (
    ErrorCategory,
    OutcomeCertainty,
    RequestPriority,
)
from telegram_kol_research.models import (
    DeepcoinExecutionOperation,
    DeepcoinRequestAttempt,
    DeepcoinSnapshotEvidence,
    TradeSignal,
)


NOW = datetime(2026, 8, 12, 12, 0, 0)
REQUEST_FP = "a" * 64
ECONOMICS_FP = "b" * 64
UID_FP = "c" * 64


@pytest.fixture
def operation_store(tmp_path):
    session_factory = create_session_factory(tmp_path / "operations.db")
    with session_factory() as session:
        first = TradeSignal(
            signal_uid="protected-entry-1",
            strategy_instance_id="strategy-1",
            source_type="message_instruction",
            venue="deepcoin",
            kol_id="source",
            chat_id=10,
            message_id=20,
            symbol="BTC",
            side="long",
            action="open_position",
            status="processing",
            payload_json="{}",
            created_at=NOW,
            updated_at=NOW,
        )
        second = TradeSignal(
            signal_uid="protected-entry-2",
            strategy_instance_id="strategy-2",
            source_type="message_instruction",
            venue="deepcoin",
            kol_id="source",
            chat_id=10,
            message_id=21,
            symbol="ETH",
            side="long",
            action="open_position",
            status="processing",
            payload_json="{}",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add_all([first, second])
        session.commit()
        return session_factory, int(first.id), int(second.id)


def _reservation(signal_id: int, **overrides):
    payload = {
        "operation_key": "protected-entry:signal-1:leg-1",
        "trade_signal_id": signal_id,
        "contract_version": "1",
        "phase": "entry_preflight",
        "state": "planned",
        "outcome_certainty": "not_sent",
        "request_fingerprint": REQUEST_FP,
        "economics_fingerprint": ECONOMICS_FP,
        "deadline_at": NOW + timedelta(seconds=10),
        "evidence": {"b": 2, "a": 1},
        "created_at": NOW,
    }
    payload.update(overrides)
    return payload


def test_reservation_locks_before_read_and_is_idempotent(operation_store):
    session_factory, signal_id, _ = operation_store
    statements: list[str] = []
    engine = session_factory.kw["bind"]

    def capture_statement(_conn, _cursor, statement, _params, _context, _many):
        statements.append(" ".join(statement.strip().split()))

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        first = reserve_execution_operation(
            session_factory, **_reservation(signal_id)
        )
        first_statements = list(statements)
        statements.clear()
        second = reserve_execution_operation(
            session_factory, **_reservation(signal_id)
        )
        second_statements = list(statements)
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert first.id == second.id
    assert first.evidence_json == '{"a":1,"b":2}'
    assert first_statements[0] == "BEGIN IMMEDIATE"
    assert second_statements[0] == "BEGIN IMMEDIATE"
    with pytest.raises(FrozenInstanceError):
        first.state = "completed"
    with session_factory() as session:
        assert session.query(DeepcoinExecutionOperation).count() == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trade_signal_id", "second_signal"),
        ("economics_fingerprint", "d" * 64),
        ("request_fingerprint", "e" * 64),
        ("contract_version", "2"),
        ("deadline_at", NOW + timedelta(seconds=11)),
    ],
)
def test_reservation_conflicts_when_immutable_identity_changes(
    operation_store, field, value
):
    session_factory, signal_id, second_signal_id = operation_store
    reserve_execution_operation(session_factory, **_reservation(signal_id))
    changed = second_signal_id if value == "second_signal" else value

    with pytest.raises(DeepcoinOperationConflict, match="operation_identity_conflict"):
        reserve_execution_operation(
            session_factory,
            **_reservation(signal_id, **{field: changed}),
        )

    with session_factory() as session:
        assert session.query(DeepcoinExecutionOperation).count() == 1


def test_transition_requires_exact_state_and_state_version(operation_store):
    session_factory, signal_id, _ = operation_store
    operation = reserve_execution_operation(
        session_factory, **_reservation(signal_id)
    )

    transitioned = transition_execution_operation(
        session_factory,
        operation_id=operation.id,
        expected_operation_key=operation.operation_key,
        expected_state="planned",
        expected_state_version=0,
        phase="entry_submit",
        state="entry_prepared",
        outcome_certainty="not_sent",
        reason_code="entry_request_prepared",
        evidence={"prepared": True},
        updated_at=NOW + timedelta(milliseconds=1),
    )

    assert transitioned.state == "entry_prepared"
    assert transitioned.state_version == 1
    assert transitioned.evidence_json == '{"prepared":true}'
    for expected_state, expected_version in (("planned", 1), ("entry_prepared", 0)):
        with pytest.raises(DeepcoinOperationConflict, match="operation_state_conflict"):
            transition_execution_operation(
                session_factory,
                operation_id=operation.id,
                expected_operation_key=operation.operation_key,
                expected_state=expected_state,
                expected_state_version=expected_version,
                phase="entry_submit",
                state="entry_submitting",
                outcome_certainty="not_sent",
                evidence={},
                updated_at=NOW + timedelta(milliseconds=2),
            )


def test_json_is_canonical_bounded_finite_depth_limited_and_secret_free(
    operation_store,
):
    session_factory, signal_id, _ = operation_store

    invalid_evidence = [
        {"value": float("nan")},
        {"value": "x" * 5000},
        {"level": {"level": {"level": {"level": {"level": {"level": {"level": {"level": {"level": 1}}}}}}}}},
        {"DC-ACCESS-KEY": "secret"},
        {"note": "Authorization: Bearer top-secret"},
    ]
    for index, evidence in enumerate(invalid_evidence):
        with pytest.raises(DeepcoinEvidenceValidationError):
            reserve_execution_operation(
                session_factory,
                **_reservation(
                    signal_id,
                    operation_key=f"invalid-evidence-{index}",
                    evidence=evidence,
                ),
            )

    with pytest.raises(DeepcoinEvidenceValidationError):
        reserve_execution_operation(
            session_factory,
            **_reservation(signal_id, operation_key="bad-code", evidence={}),
            reason_code="raw exchange error: credential=secret",
        )
    with session_factory() as session:
        assert session.query(DeepcoinExecutionOperation).count() == 0


def _attempt_fact(*, ordinal: int = 1) -> RequestAttemptFact:
    return RequestAttemptFact(
        ordinal=ordinal,
        method="GET",
        normalized_path="/deepcoin/account/positions",
        phase="entry_readback",
        priority=RequestPriority.CRITICAL,
        correlation_id="correlation-safe",
        outcome_certainty=OutcomeCertainty.UNKNOWN,
        error_category=ErrorCategory.HTTP_RETRYABLE,
        safe_code="http_503",
        http_status=503,
        business_code=None,
        governor_wait_ms=2,
        retry_delay_ms=500,
        latency_ms=9,
    )


def test_attempts_snapshots_and_generation_are_append_only(operation_store):
    session_factory, signal_id, _ = operation_store
    operation = reserve_execution_operation(
        session_factory, **_reservation(signal_id)
    )

    first_attempt = record_request_attempt(
        session_factory,
        operation_id=operation.id,
        expected_operation_key=operation.operation_key,
        expected_request_fingerprint=REQUEST_FP,
        uid_scope_hash=UID_FP,
        fact=_attempt_fact(),
        started_at=NOW,
        completed_at=NOW + timedelta(milliseconds=9),
    )
    second_attempt = record_request_attempt(
        session_factory,
        operation_id=operation.id,
        expected_operation_key=operation.operation_key,
        expected_request_fingerprint=REQUEST_FP,
        uid_scope_hash=UID_FP,
        fact=_attempt_fact(ordinal=1),
        started_at=NOW + timedelta(seconds=1),
        completed_at=NOW + timedelta(seconds=1, milliseconds=9),
    )
    snapshot = record_snapshot_evidence(
        session_factory,
        operation_id=operation.id,
        expected_operation_key=operation.operation_key,
        snapshot_kind="positions",
        available=True,
        schema_valid=True,
        complete=True,
        row_count=1,
        page_count=1,
        collection_fingerprint="d" * 64,
        start_write_generation=2,
        end_write_generation=2,
        capture_started_at=NOW,
        capture_ended_at=NOW + timedelta(milliseconds=20),
        evidence={"owned_position_count": 1},
    )
    before_writer = advance_account_write_generation(
        session_factory, uid_scope_hash=UID_FP, updated_at=NOW
    )
    after_writer = advance_account_write_generation(
        session_factory,
        uid_scope_hash=UID_FP,
        updated_at=NOW + timedelta(milliseconds=1),
    )
    bundle = load_operation_bundle(session_factory, operation_id=operation.id)

    assert (first_attempt.ordinal, second_attempt.ordinal) == (1, 2)
    assert snapshot.ordinal == 1
    with pytest.raises(FrozenInstanceError):
        snapshot.complete = False
    assert (before_writer.generation, after_writer.generation) == (1, 2)
    assert [item.ordinal for item in bundle.attempts] == [1, 2]
    assert [item.ordinal for item in bundle.snapshots] == [1]
    assert bundle.operation.attempt_count == 2
    with session_factory() as session:
        stored_attempt = session.get(DeepcoinRequestAttempt, first_attempt.id)
        stored_snapshot = session.get(DeepcoinSnapshotEvidence, snapshot.id)
        assert stored_attempt is not None
        assert stored_snapshot is not None
        assert not hasattr(stored_attempt, "request_body")
        assert not hasattr(stored_snapshot, "raw_rows")


def test_concurrent_reservation_and_attempt_ordinals_have_one_owner(operation_store):
    session_factory, signal_id, _ = operation_store

    def reserve_once():
        return reserve_execution_operation(
            session_factory, **_reservation(signal_id)
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        operations = list(pool.map(lambda _index: reserve_once(), range(8)))
    assert len({item.id for item in operations}) == 1
    operation = operations[0]

    def record_once(index: int):
        return record_request_attempt(
            session_factory,
            operation_id=operation.id,
            expected_operation_key=operation.operation_key,
            expected_request_fingerprint=REQUEST_FP,
            uid_scope_hash=UID_FP,
            fact=_attempt_fact(ordinal=index + 1),
            started_at=NOW + timedelta(milliseconds=index),
            completed_at=NOW + timedelta(milliseconds=index + 1),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(record_once, range(8)))

    assert sorted(item.ordinal for item in attempts) == list(range(1, 9))
    with session_factory() as session:
        assert session.query(DeepcoinExecutionOperation).count() == 1
        assert session.query(DeepcoinRequestAttempt).count() == 8


def test_attempt_recorder_failure_never_looks_like_exchange_success(operation_store):
    session_factory, signal_id, _ = operation_store
    operation = reserve_execution_operation(
        session_factory, **_reservation(signal_id)
    )

    with pytest.raises(DeepcoinOperationConflict, match="operation_identity_conflict"):
        record_request_attempt(
            session_factory,
            operation_id=operation.id,
            expected_operation_key="another-operation",
            expected_request_fingerprint=REQUEST_FP,
            uid_scope_hash=UID_FP,
            fact=_attempt_fact(),
            started_at=NOW,
            completed_at=NOW,
        )
    with session_factory() as session:
        assert session.query(DeepcoinRequestAttempt).count() == 0

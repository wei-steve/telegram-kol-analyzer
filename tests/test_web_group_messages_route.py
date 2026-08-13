from datetime import UTC, datetime, timedelta
import json
import re

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, text

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    DeepcoinExecutionOperation,
    DeepcoinRequestAttempt,
    DeepcoinSnapshotEvidence,
    ExecutionBinding,
    MediaAsset,
    MessageEvidenceVersion,
    MessageRecognition,
    MimoRecognitionAttempt,
    MimoRecognitionRun,
    RawMessage,
    RecognitionDecision,
    RecognitionExperiment,
    SignalCandidate,
    StrategyLifecycle,
    TradeSignal,
)
from telegram_kol_research.web_app import create_web_app
from telegram_kol_research.web_queries import load_group_messages
from telegram_kol_research.web_queries import _safe_deepcoin_display_code


@pytest.mark.parametrize(
    "safe_code",
    ("connect_timeout", "write_timeout", "pool_timeout"),
)
def test_group_messages_projection_preserves_known_transport_safe_codes(
    safe_code,
):
    assert _safe_deepcoin_display_code(safe_code) == safe_code


def test_group_messages_route_returns_partial_for_selected_group(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=77,
                    message_id=1,
                    posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                    text="group 77",
                ),
                RawMessage(
                    chat_id=88,
                    message_id=1,
                    posted_at=datetime(2026, 4, 3, tzinfo=UTC),
                    text="group 88",
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert "group 88" in response.text
    assert "group 77" not in response.text


def test_group_messages_route_projects_canonical_deepcoin_execution_operation(
    tmp_path,
):
    database_path = tmp_path / "canonical-execution.db"
    session_factory = create_session_factory(database_path)
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    states = (
        ("protected", "completed", "confirmed", "保护已确认"),
        ("pre_submit_deferred", "next_leg_preflight", "not_sent", "已延期"),
        (
            "entry_pending_readback",
            "entry_readback",
            "accepted",
            "等待入场读回",
        ),
        ("entry_unknown", "entry_readback", "unknown", "入场结果未知"),
        ("entry_rejected", "entry_readback", "rejected", "入场已拒绝"),
        (
            "submission_failed_no_exposure",
            "completed",
            "confirmed",
            "确认未产生敞口",
        ),
    )
    with session_factory() as session:
        for ordinal, (state, phase, certainty, _label) in enumerate(states, start=1):
            session.add(
                RawMessage(
                    chat_id=88,
                    message_id=ordinal,
                    posted_at=now + timedelta(minutes=ordinal),
                    text=f"canonical execution {state}",
                )
            )
            signal = TradeSignal(
                signal_uid=f"canonical-signal-{ordinal}",
                strategy_instance_id=f"strategy-{ordinal}",
                source_type=(
                    "message_instruction" if ordinal == 3 else "recovery"
                ),
                venue="deepcoin",
                kol_id="alice",
                chat_id=88,
                message_id=ordinal,
                symbol="BTC",
                side="long",
                action="open_position",
                status="recovery_required",
                payload_json=(
                    '{"request_body":"DC-ACCESS-KEY=RAW-KEY",'
                    '"passphrase":"RAW-PASSPHRASE"}'
                ),
                result_json='{"response_body":"RAW-RESPONSE"}',
                last_error="Authorization: Bearer HOSTILE-ERROR",
            )
            session.add(signal)
            session.flush()
            root_operation = DeepcoinExecutionOperation(
                operation_key=f"protected-entry:v1:signal:{signal.id}:leg:1:entry",
                trade_signal_id=signal.id,
                contract_version="1",
                phase="completed" if ordinal == 2 else phase,
                state="protected" if ordinal == 2 else state,
                outcome_certainty="confirmed" if ordinal == 2 else certainty,
                error_category=(
                    "business_rejected"
                    if state == "entry_rejected" and ordinal != 2
                    else None
                ),
                reason_code=(
                    "dc-access-key:raw-reason"
                    if ordinal == 1
                    else "primary_leg_protected"
                    if ordinal == 2
                    else f"safe_{state}"
                ),
                request_fingerprint="a" * 64,
                economics_fingerprint="b" * 64,
                deadline_at=now + timedelta(seconds=10 + ordinal),
                writer_attempted_at=(
                    None if state == "pre_submit_deferred" and ordinal != 2 else now
                ),
                completed_at=(
                    now + timedelta(seconds=2)
                    if ordinal == 2
                    or state in {"protected", "submission_failed_no_exposure"}
                    else None
                ),
                attempt_count=0 if ordinal == 2 else 1,
                state_version=ordinal,
                evidence_json=(
                    '{"order_id":"RAW-ORDER-ID",'
                    '"pos_id":"RAW-POS-ID",'
                    '"private_key":"RAW-PRIVATE-KEY"}'
                ),
                created_at=now,
                updated_at=now + timedelta(seconds=ordinal),
            )
            session.add(root_operation)
            session.flush()
            operation = root_operation
            if ordinal == 2:
                operation = DeepcoinExecutionOperation(
                    operation_key=(
                        f"protected-entry:v1:signal:{signal.id}:leg:2:entry"
                    ),
                    trade_signal_id=signal.id,
                    parent_operation_id=root_operation.id,
                    contract_version="1",
                    phase=phase,
                    state=state,
                    outcome_certainty=certainty,
                    reason_code=f"safe_{state}",
                    request_fingerprint="a" * 64,
                    economics_fingerprint="b" * 64,
                    deadline_at=now + timedelta(seconds=10 + ordinal),
                    writer_attempted_at=None,
                    attempt_count=1,
                    state_version=ordinal,
                    evidence_json="{}",
                    created_at=now,
                    updated_at=now + timedelta(seconds=ordinal + 1),
                )
                session.add(operation)
                session.flush()
            session.add(
                DeepcoinRequestAttempt(
                    deepcoin_execution_operation_id=operation.id,
                    ordinal=1,
                    method="GET" if "readback" in phase else "POST",
                    normalized_path="/deepcoin/trade/order",
                    priority="critical",
                    phase=phase,
                    outcome_certainty=certainty,
                    error_category=(
                        "business_rejected"
                        if state == "entry_rejected"
                        else None
                    ),
                    safe_code=(
                        "dc_access_key:raw-attempt"
                        if ordinal == 1
                        else f"safe_attempt_{ordinal}"
                    ),
                    http_status=400 if state == "entry_rejected" else 200,
                    business_code="1001" if state == "entry_rejected" else "0",
                    governor_wait_ms=ordinal * 10,
                    retry_delay_ms=ordinal * 20,
                    latency_ms=ordinal * 30,
                    uid_scope_hash="c" * 64,
                    request_fingerprint="a" * 64,
                    started_at=now,
                    completed_at=now + timedelta(milliseconds=ordinal * 30),
                )
            )
            session.add(
                DeepcoinSnapshotEvidence(
                    deepcoin_execution_operation_id=operation.id,
                    ordinal=1,
                    snapshot_kind="account_composite",
                    available=True,
                    schema_valid=True,
                    complete=True,
                    row_count=ordinal,
                    page_count=1,
                    collection_fingerprint="d" * 64,
                    start_write_generation=2,
                    end_write_generation=2,
                    capture_started_at=now + timedelta(seconds=3),
                    capture_ended_at=now + timedelta(seconds=4),
                    evidence_json='{"response_body":"RAW-SNAPSHOT"}',
                )
            )
        revision_signal = TradeSignal(
            signal_uid="unrelated-strategy-revision",
            strategy_instance_id="unrelated-strategy",
            source_type="strategy_revision",
            venue="deepcoin",
            kol_id="system",
            chat_id=88,
            message_id=1,
            symbol="BTC",
            side="long",
            action="open_position",
            status="recovery_required",
            payload_json="{}",
        )
        session.add(revision_signal)
        session.flush()
        session.add(
            DeepcoinExecutionOperation(
                operation_key=(
                    f"protected-entry:v1:signal:{revision_signal.id}:leg:1:entry"
                ),
                trade_signal_id=revision_signal.id,
                contract_version="1",
                phase="entry_readback",
                state="entry_rejected",
                outcome_certainty="rejected",
                error_category="business_rejected",
                reason_code="strategy_revision_collision",
                request_fingerprint="e" * 64,
                economics_fingerprint="f" * 64,
                deadline_at=now + timedelta(seconds=30),
                writer_attempted_at=now,
                attempt_count=1,
                evidence_json="{}",
                created_at=now,
                updated_at=now,
            )
        )
        no_exposure_collision_signal = TradeSignal(
            signal_uid="newer-no-exposure-signal",
            strategy_instance_id="newer-no-exposure-strategy",
            source_type="recovery",
            venue="deepcoin",
            kol_id="alice",
            chat_id=88,
            message_id=4,
            symbol="ETH",
            side="long",
            action="open_position",
            status="submitted",
            payload_json="{}",
        )
        session.add(no_exposure_collision_signal)
        session.flush()
        session.add(
            DeepcoinExecutionOperation(
                operation_key=(
                    "protected-entry:v1:signal:"
                    f"{no_exposure_collision_signal.id}:leg:1:entry"
                ),
                trade_signal_id=no_exposure_collision_signal.id,
                contract_version="1",
                phase="completed",
                state="submission_failed_no_exposure",
                outcome_certainty="confirmed",
                reason_code="newer_no_exposure_collision",
                request_fingerprint="1" * 64,
                economics_fingerprint="2" * 64,
                deadline_at=now + timedelta(seconds=30),
                writer_attempted_at=now,
                completed_at=now + timedelta(seconds=1),
                attempt_count=1,
                evidence_json="{}",
                created_at=now,
                updated_at=now + timedelta(minutes=1),
            )
        )
        session.add(
            RawMessage(
                chat_id=88,
                message_id=7,
                posted_at=now + timedelta(minutes=7),
                text="duplicate canonical roots",
            )
        )
        duplicate_root_signal = TradeSignal(
            signal_uid="duplicate-root-signal",
            strategy_instance_id="duplicate-root-strategy",
            source_type="message_instruction",
            venue="deepcoin",
            kol_id="alice",
            chat_id=88,
            message_id=7,
            symbol="BTC",
            side="long",
            action="open_position",
            status="recovery_required",
            payload_json="{}",
        )
        session.add(duplicate_root_signal)
        session.flush()
        for leg_index, root_state in ((1, "completed"), (2, "protected")):
            session.add(
                DeepcoinExecutionOperation(
                    operation_key=(
                        "protected-entry:v1:signal:"
                        f"{duplicate_root_signal.id}:leg:{leg_index}:entry"
                    ),
                    trade_signal_id=duplicate_root_signal.id,
                    contract_version="1",
                    phase="completed",
                    state=root_state,
                    outcome_certainty="confirmed",
                    reason_code=f"duplicate_root_{root_state}",
                    request_fingerprint=str(leg_index) * 64,
                    economics_fingerprint=str(leg_index + 2) * 64,
                    deadline_at=now + timedelta(seconds=40),
                    completed_at=now + timedelta(seconds=3),
                    attempt_count=0,
                    evidence_json="{}",
                    created_at=now,
                    updated_at=now + timedelta(seconds=leg_index),
                )
            )
        session.add(
            RawMessage(
                chat_id=88,
                message_id=8,
                posted_at=now + timedelta(minutes=8),
                text="malformed durable metrics",
            )
        )
        malformed_signal = TradeSignal(
            signal_uid="malformed-metrics-signal",
            strategy_instance_id="malformed-metrics-strategy",
            source_type="recovery",
            venue="deepcoin",
            kol_id="alice",
            chat_id=88,
            message_id=8,
            symbol="BTC",
            side="long",
            action="open_position",
            status="recovery_required",
            payload_json="{}",
        )
        session.add(malformed_signal)
        session.flush()
        malformed_operation = DeepcoinExecutionOperation(
            operation_key=(
                f"protected-entry:v1:signal:{malformed_signal.id}:leg:1:entry"
            ),
            trade_signal_id=malformed_signal.id,
            contract_version="1",
            phase="completed",
            state="completed",
            outcome_certainty="confirmed",
            reason_code="malformed_metric_fixture",
            request_fingerprint="7" * 64,
            economics_fingerprint="8" * 64,
            deadline_at=now + timedelta(seconds=50),
            completed_at=now + timedelta(seconds=4),
            attempt_count=1,
            evidence_json="{}",
            created_at=now,
            updated_at=now,
        )
        session.add(malformed_operation)
        session.flush()
        malformed_attempt = DeepcoinRequestAttempt(
            deepcoin_execution_operation_id=malformed_operation.id,
            ordinal=1,
            method="GET",
            normalized_path="/deepcoin/account/positions",
            priority="critical",
            phase="completed",
            outcome_certainty="confirmed",
            safe_code="complete",
            http_status=200,
            business_code="0",
            governor_wait_ms=1,
            retry_delay_ms=0,
            latency_ms=2,
            uid_scope_hash="9" * 64,
            request_fingerprint="7" * 64,
            started_at=now,
            completed_at=now + timedelta(milliseconds=2),
        )
        session.add(malformed_attempt)
        session.flush()
        malformed_snapshot = DeepcoinSnapshotEvidence(
            deepcoin_execution_operation_id=malformed_operation.id,
            ordinal=1,
            snapshot_kind="account_composite",
            available=True,
            schema_valid=True,
            complete=True,
            row_count=0,
            page_count=1,
            collection_fingerprint="0" * 64,
            start_write_generation=2,
            end_write_generation=2,
            capture_started_at=now,
            capture_ended_at=now + timedelta(seconds=1),
            evidence_json="{}",
        )
        session.add(malformed_snapshot)
        session.flush()
        malformed_operation_id = int(malformed_operation.id)
        malformed_attempt_id = int(malformed_attempt.id)
        malformed_snapshot_id = int(malformed_snapshot.id)
        session.add(
            RawMessage(
                chat_id=88,
                message_id=9,
                posted_at=now + timedelta(minutes=9),
                text="incoherent protected certainty",
            )
        )
        incoherent_signal = TradeSignal(
            signal_uid="incoherent-operation-signal",
            strategy_instance_id="incoherent-operation-strategy",
            source_type="recovery",
            venue="deepcoin",
            kol_id="alice",
            chat_id=88,
            message_id=9,
            symbol="BTC",
            side="long",
            action="open_position",
            status="recovery_required",
            payload_json="{}",
        )
        session.add(incoherent_signal)
        session.flush()
        session.add(
            DeepcoinExecutionOperation(
                operation_key=(
                    f"protected-entry:v1:signal:{incoherent_signal.id}:leg:1:entry"
                ),
                trade_signal_id=incoherent_signal.id,
                contract_version="1",
                phase="completed",
                state="protected",
                outcome_certainty="unknown",
                reason_code="unsafe_protected_unknown",
                request_fingerprint="3" * 64,
                economics_fingerprint="4" * 64,
                deadline_at=now + timedelta(seconds=60),
                writer_attempted_at=now,
                attempt_count=1,
                evidence_json="{}",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            RawMessage(
                chat_id=88,
                message_id=10,
                posted_at=now + timedelta(minutes=10),
                text="entry confirmed protection prepared",
            )
        )
        prepared_signal = TradeSignal(
            signal_uid="protection-prepared-signal",
            strategy_instance_id="protection-prepared-strategy",
            source_type="message_instruction",
            venue="deepcoin",
            kol_id="alice",
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="long",
            action="open_position",
            status="active_protection_pending",
            payload_json="{}",
        )
        session.add(prepared_signal)
        session.flush()
        prepared_parent = DeepcoinExecutionOperation(
            operation_key=(
                f"protected-entry:v1:signal:{prepared_signal.id}:leg:1:entry"
            ),
            trade_signal_id=prepared_signal.id,
            contract_version="1",
            phase="protection_submit",
            state="protection_prepared",
            outcome_certainty="confirmed",
            reason_code="protection_intents_prepared",
            request_fingerprint="5" * 64,
            economics_fingerprint="6" * 64,
            deadline_at=now + timedelta(seconds=70),
            writer_attempted_at=now,
            attempt_count=1,
            evidence_json="{}",
            created_at=now,
            updated_at=now,
        )
        session.add(prepared_parent)
        session.flush()
        session.add(
            DeepcoinExecutionOperation(
                operation_key=(
                    "protected-entry:v1:signal:"
                    f"{prepared_signal.id}:leg:1:protection:0"
                ),
                trade_signal_id=prepared_signal.id,
                parent_operation_id=prepared_parent.id,
                contract_version="1",
                phase="protection_submit",
                state="protection_prepared",
                outcome_certainty="not_sent",
                reason_code="protection_submit_authorized",
                request_fingerprint="5" * 64,
                economics_fingerprint="6" * 64,
                deadline_at=now + timedelta(seconds=70),
                attempt_count=0,
                evidence_json="{}",
                created_at=now,
                updated_at=now + timedelta(seconds=1),
            )
        )
        session.commit()

    engine = session_factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE deepcoin_execution_operations "
                "SET attempt_count = 'oops', deadline_at = 'oops' "
                "WHERE id = :operation_id"
            ),
            {"operation_id": malformed_operation_id},
        )
        connection.execute(
            text(
                "UPDATE deepcoin_request_attempts "
                "SET governor_wait_ms = 'oops', started_at = 'oops' "
                "WHERE id = :attempt_id"
            ),
            {"attempt_id": malformed_attempt_id},
        )
        connection.execute(
            text(
                "UPDATE deepcoin_snapshot_evidence "
                "SET capture_started_at = 'oops', capture_ended_at = 'oops' "
                "WHERE id = :snapshot_id"
            ),
            {"snapshot_id": malformed_snapshot_id},
        )

    statements: list[str] = []

    def track_execution_queries(
        _conn, _cursor, statement, _parameters, _context, _many
    ):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", track_execution_queries)
    try:
        projected_rows = load_group_messages(
            session_factory, chat_id=88, limit=20
        )
    finally:
        event.remove(engine, "before_cursor_execute", track_execution_queries)

    assert len(projected_rows) == 10
    incoherent_projection = next(
        row["deepcoin_execution"]
        for row in projected_rows
        if row["message_id"] == 9
    )
    assert incoherent_projection["state"] == "evidence_conflict"
    assert incoherent_projection["next_action"] == "需要人工核实"
    malformed_projection = next(
        row["deepcoin_execution"]
        for row in projected_rows
        if row["message_id"] == 8
    )
    assert malformed_projection["state"] == "evidence_conflict"
    assert malformed_projection["next_action"] == "需要人工核实"
    assert malformed_projection["deadline_at"] is None
    assert malformed_projection["latest_complete_snapshot_at"] is None
    prepared_projection = next(
        row["deepcoin_execution"]
        for row in projected_rows
        if row["message_id"] == 10
    )
    assert prepared_projection["state"] == "protection_prepared"
    assert prepared_projection["state_label"] == "保护单已准备"
    for table_name in (
        "trade_signals",
        "deepcoin_execution_operations",
        "deepcoin_request_attempts",
        "deepcoin_snapshot_evidence",
    ):
        assert sum(table_name in statement for statement in statements) == 1

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    )

    assert response.status_code == 200
    assert response.text.count('data-deepcoin-execution="canonical"') == 10
    for _state, _phase, _certainty, label in states:
        assert label in response.text
    assert "入场读回核实" in response.text
    assert "结果未知" in response.text
    assert "尝试 1 次" in response.text
    assert "截止 2026-08-13" in response.text
    assert "最近完整快照 2026-08-13" in response.text
    assert "限流等待 60 ms" in response.text
    assert "请求耗时 180 ms" in response.text
    assert "读回耗时 150 ms" in response.text
    assert "自动读取核实" in response.text
    assert "已延期，禁止自动追单" in response.text
    assert "需要人工核实" in response.text
    assert "操作身份冲突" in response.text
    assert "状态证据异常" in response.text
    assert "safe_entry_rejected" in response.text
    assert "business_rejected" in response.text
    assert "RAW-KEY" not in response.text
    assert "RAW-PASSPHRASE" not in response.text
    assert "RAW-RESPONSE" not in response.text
    assert "RAW-ORDER-ID" not in response.text
    assert "RAW-POS-ID" not in response.text
    assert "HOSTILE-ERROR" not in response.text
    assert "RAW-SNAPSHOT" not in response.text
    assert "raw-reason" not in response.text
    assert "raw-attempt" not in response.text
    assert "strategy_revision_collision" not in response.text
    assert "newer_no_exposure_collision" not in response.text


def _persist_bounded_deepcoin_projection_fixture(
    session_factory,
    *,
    operation_key: str | None = None,
    contract_version: str = "1",
    attempt_count: int = 1,
):
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=188,
                message_id=1,
                posted_at=now,
                text="bounded canonical execution",
            )
        )
        signal = TradeSignal(
            signal_uid="bounded-canonical-signal",
            strategy_instance_id="bounded-canonical-strategy",
            source_type="message_instruction",
            venue="deepcoin",
            kol_id="alice",
            chat_id=188,
            message_id=1,
            symbol="BTC",
            side="long",
            action="open_position",
            status="submitted",
            payload_json="{}",
        )
        session.add(signal)
        session.flush()
        root = DeepcoinExecutionOperation(
            operation_key=(
                operation_key
                or f"protected-entry:v1:signal:{signal.id}:leg:1:entry"
            ),
            trade_signal_id=signal.id,
            contract_version=contract_version,
            phase="completed",
            state="completed",
            outcome_certainty="confirmed",
            reason_code="canonical_complete",
            request_fingerprint="a" * 64,
            economics_fingerprint="b" * 64,
            deadline_at=now + timedelta(seconds=10),
            writer_attempted_at=now,
            completed_at=now + timedelta(seconds=1),
            attempt_count=attempt_count,
            state_version=1,
            evidence_json="{}",
            created_at=now,
            updated_at=now,
        )
        session.add(root)
        session.flush()
        session.commit()
        return int(root.id), int(signal.id)


def test_group_messages_projection_bounds_attempt_rows_and_fails_closed(tmp_path):
    database_path = tmp_path / "bounded-execution-attempts.db"
    session_factory = create_session_factory(database_path)
    operation_id, _signal_id = _persist_bounded_deepcoin_projection_fixture(
        session_factory,
        attempt_count=65,
    )
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add_all(
            [
                DeepcoinRequestAttempt(
                    deepcoin_execution_operation_id=operation_id,
                    ordinal=ordinal,
                    method="GET",
                    normalized_path="/deepcoin/account/positions",
                    priority="critical",
                    phase="completed",
                    outcome_certainty="confirmed",
                    safe_code="complete",
                    http_status=200,
                    business_code="0",
                    governor_wait_ms=0,
                    retry_delay_ms=0,
                    latency_ms=1,
                    uid_scope_hash="c" * 64,
                    request_fingerprint="a" * 64,
                    started_at=now + timedelta(milliseconds=ordinal),
                    completed_at=now + timedelta(milliseconds=ordinal + 1),
                )
                for ordinal in range(1, 66)
            ]
        )
        session.commit()

    projection = load_group_messages(
        session_factory,
        chat_id=188,
        limit=1,
    )[0]["deepcoin_execution"]

    assert projection["state"] == "evidence_conflict"
    assert projection["next_action"] == "需要人工核实"
    assert len(projection["attempts"]) <= 64


def test_group_messages_projection_rejects_unpinned_operation_version_and_key(
    tmp_path,
):
    database_path = tmp_path / "unpinned-execution-operation.db"
    session_factory = create_session_factory(database_path)
    _persist_bounded_deepcoin_projection_fixture(
        session_factory,
        operation_key="unrelated:v2:operation",
        contract_version="2",
    )

    projection = load_group_messages(
        session_factory,
        chat_id=188,
        limit=1,
    )[0]["deepcoin_execution"]

    assert projection["state"] == "identity_conflict"
    assert projection["next_action"] == "需要人工核实"


def test_group_messages_projection_normalizes_timezone_aware_sqlite_times(
    tmp_path,
):
    database_path = tmp_path / "aware-execution-time.db"
    session_factory = create_session_factory(database_path)
    root_id, signal_id = _persist_bounded_deepcoin_projection_fixture(
        session_factory,
    )
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        child = DeepcoinExecutionOperation(
            operation_key=(
                f"protected-entry:v1:signal:{signal_id}:leg:1:protection:0"
            ),
            trade_signal_id=signal_id,
            parent_operation_id=root_id,
            contract_version="1",
            phase="completed",
            state="completed",
            outcome_certainty="confirmed",
            reason_code="canonical_child_complete",
            request_fingerprint="d" * 64,
            economics_fingerprint="e" * 64,
            deadline_at=now + timedelta(seconds=10),
            writer_attempted_at=now,
            completed_at=now + timedelta(seconds=1),
            attempt_count=0,
            state_version=1,
            evidence_json="{}",
            created_at=now,
            updated_at=now,
        )
        session.add(child)
        session.flush()
        child_id = int(child.id)
        session.commit()
    engine = session_factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE deepcoin_execution_operations "
                "SET updated_at = '2026-08-13T12:00:01+00:00' "
                "WHERE id = :operation_id"
            ),
            {"operation_id": child_id},
        )

    projection = load_group_messages(
        session_factory,
        chat_id=188,
        limit=1,
    )[0]["deepcoin_execution"]

    assert projection["state"] == "completed"
    assert projection["next_action"] == "已完成，无需操作"


def test_group_messages_signal_overflow_fails_closed_for_every_page_message(
    tmp_path,
):
    database_path = tmp_path / "overflowing-execution-signals.db"
    session_factory = create_session_factory(database_path)
    root_id, signal_id = _persist_bounded_deepcoin_projection_fixture(
        session_factory,
    )
    assert root_id > 0
    assert signal_id > 0
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=188,
                message_id=2,
                posted_at=now + timedelta(minutes=1),
                text="overflowing related signals",
            )
        )
        session.add_all(
            [
                TradeSignal(
                    signal_uid=f"overflow-signal-{index}",
                    strategy_instance_id=f"overflow-strategy-{index}",
                    source_type="message_instruction",
                    venue="deepcoin",
                    kol_id="alice",
                    chat_id=188,
                    message_id=2,
                    symbol=f"X{index}",
                    side="long",
                    action="open_position",
                    status="recovery_required",
                    payload_json="{}",
                )
                for index in range(256)
            ]
        )
        session.commit()

    rows = load_group_messages(session_factory, chat_id=188, limit=2)

    assert {row["message_id"] for row in rows} == {1, 2}
    assert all(
        row["deepcoin_execution"]["state"] == "evidence_conflict"
        for row in rows
    )


@pytest.mark.parametrize(
    "exchange_identity",
    (
        "123456789012345678",
        "550e8400-e29b-41d4-a716-446655440000",
        "a" * 64,
        "order_123456789012345678",
        "pos_123456789012345678",
        "clord_abcdef0123456789",
    ),
)
def test_group_messages_projection_never_renders_exchange_identity(
    tmp_path,
    exchange_identity,
):
    database_path = tmp_path / "numeric-exchange-identity.db"
    session_factory = create_session_factory(database_path)
    operation_id, _signal_id = _persist_bounded_deepcoin_projection_fixture(
        session_factory,
    )
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        operation = session.get(DeepcoinExecutionOperation, operation_id)
        operation.reason_code = exchange_identity
        session.add(
            DeepcoinRequestAttempt(
                deepcoin_execution_operation_id=operation_id,
                ordinal=1,
                method="GET",
                normalized_path="/deepcoin/account/positions",
                priority="critical",
                phase="completed",
                outcome_certainty="confirmed",
                safe_code=exchange_identity,
                http_status=200,
                business_code="0",
                governor_wait_ms=0,
                retry_delay_ms=0,
                latency_ms=1,
                uid_scope_hash="c" * 64,
                request_fingerprint="a" * 64,
                started_at=now,
                completed_at=now + timedelta(milliseconds=1),
            )
        )
        session.commit()

    projection = load_group_messages(
        session_factory,
        chat_id=188,
        limit=1,
    )[0]["deepcoin_execution"]

    assert projection["reason_code"] is None
    assert projection["attempts"][0]["safe_code"] is None
    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/188/messages"
    )
    assert response.status_code == 200
    assert exchange_identity not in response.text


def test_group_messages_route_renders_decision_card_before_model_analysis(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=7,
            text="调整一下止损防止插针。",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="非策略",
                authoritative_payload_json=(
                    '{"reason":"识别到调整止损意图，未提供新的止损价格。",'
                    '"lifecycle_event":{"event_type":"position_update",'
                    '"management_action":"move_stop_to_protect",'
                    '"symbol":"BTC","side":"short","stop_loss":null}}'
                ),
                agreement_status="agreed",
                differences_json="[]",
                comparison_status="completed",
                disagreement_severity="none",
                comparison_model="deepseek-v4-flash",
                comparison_payload_json=(
                    '{"reason":"同意不可自动执行，建议补充价格后再处理。",'
                    '"conflict_types":[]}'
                ),
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert 'class="message-decision-card is-manual-review"' in response.text
    assert "需人工确认" in response.text
    assert "建议动作：<strong>不执行</strong>" in response.text
    assert "未提供新的止损价格" in response.text
    assert "本消息新增：" in response.text
    assert "自动执行记录：" in response.text
    assert "主分析 · MiMo" in response.text
    assert "辅助复核 · DeepSeek" in response.text
    assert "历史 AI 细节（调试）" in response.text
    assert "is-decision-card-history" in response.text
    assert 'data-message-ai-insights\n            open' not in response.text
    assert "结论一致 · 不自动执行" in response.text
    assert "未发送交易所请求" in response.text
    assert response.text.index("需人工确认") < response.text.index("主分析 · MiMo")


def test_groups_route_returns_latest_activity_sorted_partial(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=77,
                    message_id=1,
                    posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                    sender_name="older group",
                    text="older",
                ),
                RawMessage(
                    chat_id=88,
                    message_id=1,
                    posted_at=datetime(2026, 4, 3, tzinfo=UTC),
                    sender_name="newer group",
                    text="newer",
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups?selected_chat_id=77")

    assert response.status_code == 200
    assert 'kol-strategy-list' in response.text
    assert response.text.index("newer group") < response.text.index("older group")
    assert 'data-chat-id="77"' in response.text
    assert response.text.count("is-active") >= 1


def test_groups_route_uses_lifecycle_counts_for_sidebar_badges(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=77,
                message_id=1,
                posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                sender_name="strategy group",
                text="group",
            )
        )
        binding = ExecutionBinding(
            kol_id="group:77",
            chat_id=77,
            message_id=10,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="active",
            pos_id="pos-live",
        )
        session.add(binding)
        session.flush()
        session.add_all(
            [
                StrategyLifecycle(
                    chat_id=77,
                    message_id=10,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="entered",
                    signal_at=datetime(2026, 4, 2, tzinfo=UTC),
                    execution_binding_id=binding.id,
                ),
                StrategyLifecycle(
                    chat_id=77,
                    message_id=11,
                    symbol="ETH",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 4, 2, tzinfo=UTC),
                    entry_range_low=3200,
                    entry_range_high=3220,
                ),
                StrategyLifecycle(
                    chat_id=77,
                    message_id=12,
                    symbol="SOL",
                    side="short",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 4, 2, tzinfo=UTC),
                    entry_range_low=180,
                    entry_range_high=181,
                ),
                StrategyLifecycle(
                    chat_id=77,
                    message_id=13,
                    symbol="QQ",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 4, 2, tzinfo=UTC),
                ),
                StrategyLifecycle(
                    chat_id=77,
                    message_id=14,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 4, 2, tzinfo=UTC),
                    entry_range_low=6.22,
                    entry_range_high=6.27,
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups?selected_chat_id=77")

    assert response.status_code == 200
    assert re.search(
        r'class="kol-status-badge kol-status-holding"[^>]*>\s*[^<]*1\s*</span>',
        response.text,
    )
    assert re.search(
        r'class="kol-status-badge kol-status-pending"[^>]*>\s*[^<]*2\s*</span>',
        response.text,
    )
    assert re.search(r'3\s*[^<]*</span>', response.text)


def test_group_messages_route_supports_search_and_sender_filters(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=88, message_id=1, sender_name="Alice", text="BTC long"
                ),
                RawMessage(
                    chat_id=88, message_id=2, sender_name="Bob", text="BTC short"
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages?search_text=BTC&sender_name=Alice")

    assert response.status_code == 200
    assert "BTC long" in response.text
    assert "BTC short" not in response.text


def test_group_messages_route_renders_filter_state_without_final_page_footer(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(chat_id=88, message_id=1, sender_name="Alice", text="first"),
                RawMessage(
                    chat_id=88, message_id=2, sender_name="Alice", text="second"
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages?sender_name=Ali")

    assert response.status_code == 200
    assert 'value=""' in response.text
    assert 'value="Ali"' in response.text
    assert "data-load-more" not in response.text
    assert 'data-latest-message-id="2"' in response.text
    assert response.text.index('data-message-list') < response.text.index('message-list-footer')
    assert response.text.index("second") < response.text.index("first")
    assert "data-message-select" not in response.text


def test_group_message_routes_render_twenty_messages_and_more_footer(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=88,
                    message_id=message_id,
                    text=f"message-{message_id:02d}",
                )
                for message_id in range(1, 22)
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    for path in (
        "/groups/88/messages",
        "/groups/88/detail",
        "/groups/88/detail/tab/messages",
    ):
        response = client.get(path)

        assert response.status_code == 200
        assert response.text.count("\n        data-message-card\n") == 20
        assert "message-21" in response.text
        assert "message-02" in response.text
        assert "message-01" not in response.text
        assert 'data-before-message-id="2"' in response.text
        assert "data-load-more" in response.text
        assert 'data-message-page-size="20"' in response.text


def test_group_messages_route_omits_more_footer_for_exact_final_page(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=88,
                    message_id=message_id,
                    text=f"message-{message_id:02d}",
                )
                for message_id in range(1, 21)
            ]
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    )

    assert response.status_code == 200
    assert response.text.count("\n        data-message-card\n") == 20
    assert "data-load-more" not in response.text


def test_group_messages_route_renders_messages_newest_first(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=88,
                    message_id=1,
                    sender_name="Alice",
                    posted_at=datetime(2026, 4, 1, tzinfo=UTC),
                    text="older",
                ),
                RawMessage(
                    chat_id=88,
                    message_id=2,
                    sender_name="Alice",
                    posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                    text="newer",
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert response.text.index('class="message-text">newer</p>') < response.text.index(
        'class="message-text">older</p>'
    )


def test_group_messages_route_renders_posted_at_timestamp_for_each_message(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=88,
                message_id=7,
                sender_name="Alice",
                posted_at=datetime(2026, 4, 19, 9, 30, tzinfo=UTC),
                text="timed message",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert "timed message" in response.text
    assert "2026-04-19 17:30" in response.text


def test_group_messages_route_shows_ai_strategy_detection_results(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        strategy_message = RawMessage(
            chat_id=88,
            message_id=3,
            sender_name="Alice",
            posted_at=datetime(2026, 4, 3, tzinfo=UTC),
            text="BTC long 68000-68200 SL 67500 TP 69000/70000",
        )
        text_message = RawMessage(
            chat_id=88,
            message_id=2,
            sender_name="Alice",
            posted_at=datetime(2026, 4, 2, tzinfo=UTC),
            text="普通聊天",
        )
        video_message = RawMessage(
            chat_id=88,
            message_id=1,
            sender_name="Alice",
            posted_at=datetime(2026, 4, 1, tzinfo=UTC),
            text="视频复盘",
        )
        session.add_all([strategy_message, text_message, video_message])
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=strategy_message.id,
                source_id=None,
                symbol="BTC",
                side="long",
                entry_text="68000-68200",
                stop_loss_text="67500",
                take_profit_text="69000/70000",
                leverage_text="20x",
                event_type="entry_signal",
                parse_source="text",
                confidence=0.91,
            )
        )
        session.add(
            MediaAsset(
                raw_message_id=video_message.id,
                kind="messagemediadocument",
                mime_type="video/mp4",
                local_path="data/media/88/1.mp4",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert response.text.count('class="message-ai-summary-status"') == 3
    assert response.text.count('data-message-collapsed-ai-summary') == 3
    assert "AI识别结果：是策略" in response.text
    assert "策略内容：" in response.text
    assert "BTC long" in response.text
    assert "Entry 68000-68200" in response.text
    assert "SL 67500" in response.text
    assert "TP 69000/70000" in response.text
    assert "20x" in response.text
    assert "AI识别结果：待识别" in response.text
    assert "AI识别结果：非策略" in response.text
    assert "视频消息默认跳过" in response.text
    assert response.text.count('data-message-ai-insights') == 3
    assert response.text.count('data-message-ai-insights\n            open') == 1
    assert response.text.count('class="message-ai-toggle"') == 3
    assert 'data-message-list-expand-all' in response.text
    assert 'data-message-list-default' in response.text
    assert 'data-message-list-collapse-all' in response.text
    assert response.text.count('data-message-default-expanded="true"') == 2
    assert response.text.count('data-message-default-expanded="false"') == 1


def test_group_messages_route_labels_submitted_execution_as_unconfirmed(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        message = RawMessage(
            chat_id=88,
            message_id=9525,
            sender_name="陈哥",
            text="先出来，保留40%",
        )
        session.add(message)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=message.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="是策略",
                authoritative_payload_json="{}",
                agreement_status="authoritative_only",
                differences_json="[]",
                automation_status="executed",
                automation_reason="close_submitted",
            )
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    )

    assert response.status_code == 200
    assert "实盘执行结果：" in response.text
    assert "已提交，等待交易所确认" in response.text
    assert "交易所已确认执行" not in response.text


def test_group_messages_route_labels_lifecycle_event_detection(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=3869,
            sender_name="大镖客 11分组",
            posted_at=datetime(2026, 6, 27, 13, 5, 27, tzinfo=UTC),
            text="周末震荡，时间太久注意保护成本，只浮盈100点",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MessageRecognition(
                raw_message_id=raw_message.id,
                status="非策略",
                reason="当前消息建议保护成本，属于移动止损到保护位置。",
                engine="deepseek-v4-flash",
            )
        )
        session.add(
            SignalCandidate(
                raw_message_id=raw_message.id,
                source_id=None,
                symbol="BTC",
                side="short",
                stop_loss_text="60410.9",
                take_profit_text="59800/59100/58400",
                event_type="position_update",
                parse_source="lifecycle_ai",
                confidence=0.9,
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert "AI识别结果：仓位管理" in response.text
    assert "主结果：仓位管理" in response.text
    assert "当前消息建议保护成本" in response.text
    assert "AI识别结果：非策略" not in response.text


def test_group_messages_route_shows_low_confidence_exit_targets(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=4070,
            sender_name="大镖客·Andy",
            text="空单解套的人就可以先平加仓或者平仓等新机会",
        )
        session.add(raw_message)
        session.flush()
        session.add_all(
            [
                SignalCandidate(
                    raw_message_id=raw_message.id,
                    symbol="BTC",
                    side="short",
                    event_type="position_update",
                    target_lifecycle_id=101,
                    management_action="partial_take_profit",
                    management_fraction=0.5,
                    parse_source="low_confidence_group_exit",
                    confidence=0.85,
                ),
                SignalCandidate(
                    raw_message_id=raw_message.id,
                    symbol="ETH",
                    side="short",
                    event_type="position_update",
                    target_lifecycle_id=102,
                    management_action="partial_take_profit",
                    management_fraction=0.5,
                    parse_source="low_confidence_group_exit",
                    confidence=0.85,
                ),
            ]
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    )

    assert response.status_code == 200
    assert "低信心离场：每条腿平 50%" in response.text
    assert "BTC 空 · 策略 #101" in response.text
    assert "ETH 空 · 策略 #102" in response.text


def test_group_messages_route_shows_authoritative_model_summary(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=4,
            sender_name="Alice",
            posted_at=datetime(2026, 4, 4, tzinfo=UTC),
            text="BTC long 68000 SL 67000 TP 70000",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MessageRecognition(
                raw_message_id=raw_message.id,
                status="非策略",
                reason="MiMo identified an exit event.",
                summary="BTC long Entry 68000 SL 67000 TP 70000",
                engine="mimo-v2.5",
            )
        )
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="非策略",
                authoritative_payload_json=(
                    '{"reason":"MiMo identified an exit event.",'
                    '"lifecycle_event":{"event_type":"exit_position",'
                    '"symbol":"ETH","side":"long"}}'
                ),
                auxiliary_model="deepseek-v4-flash",
                auxiliary_status="非策略",
                auxiliary_payload_json='{"reason":"DeepSeek agrees with the exit."}',
                agreement_status="agreed",
                differences_json="[]",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert "权威模型结论" in response.text
    assert "MiMo 主分析" in response.text
    assert "DeepSeek 辅助复核" in response.text
    assert "deepseek-v4-flash" in response.text
    assert "mimo-v2.5" in response.text
    assert "MiMo identified an exit event." in response.text
    assert "DeepSeek agrees with the exit." in response.text
    assert "DeepSeek text" not in response.text
    assert "GLM-OCR image" not in response.text
    assert "MiMo text" not in response.text
    assert "MiMo image" not in response.text
    collapsed_summary = re.search(
        r'<div class="message-collapsed-ai-summary"[^>]*>(.*?)</div>',
        response.text,
        re.S,
    )
    assert collapsed_summary
    assert "MiMo：BTC long Entry 68000 SL 67000 TP 70000" in collapsed_summary.group(1)
    assert "BTC long Entry 68000 SL 67000 TP 70000" in collapsed_summary.group(1)
    assert "MiMo detected strategy" not in collapsed_summary.group(1)


def test_group_messages_route_omits_retired_mimo_experiment(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    empty_strategy_json = (
        '{"entry": null, "side": null, "stop_loss": null, '
        '"symbol": null, "take_profit": null}'
    )
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=5,
            sender_name="Alice",
            posted_at=datetime(2026, 4, 4, tzinfo=UTC),
            text="Join the VIP channel.",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            RecognitionExperiment(
                raw_message_id=raw_message.id,
                experiment_name="mimo_direct_v1",
                model="mimo-v2.5",
                prompt_version="mimo_direct_v1",
                input_kind="text",
                status="\u975e\u7b56\u7565",
                reason="Advertisement.",
                observed_text="Join the VIP channel.",
                strategy_json=empty_strategy_json,
                confidence=0.1,
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert "MiMo text" not in response.text
    assert empty_strategy_json not in response.text


def test_group_messages_route_renders_immediate_recognition_button(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=88,
                message_id=1,
                sender_name="Alice",
                text="BTC long 68000-68200",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert "立即识别" in response.text
    assert "data-recognize-message" in response.text
    assert 'data-raw-message-id="1"' in response.text


def test_group_messages_route_renders_mimo_first_one_level_truth_order(tmp_path):
    database_path = tmp_path / "mimo-first.db"
    session_factory = create_session_factory(database_path)
    now = datetime(2026, 8, 11, 20, 0)
    with session_factory() as session:
        raw = RawMessage(
            chat_id=88,
            message_id=50,
            sender_name="Trader",
            text="移动止损到1940",
        )
        session.add(raw)
        session.flush()
        media = MediaAsset(
            raw_message_id=raw.id,
            kind="photo",
            mime_type="image/jpeg",
            local_path="data/media/88/50.jpg",
        )
        session.add(media)
        session.flush()
        authoritative = MimoRecognitionRun(
            raw_message_id=raw.id,
            run_kind="v2_authoritative",
            contract_version="mimo-authoritative-v2",
            model="mimo-v2.5",
            input_kind="text+image",
            input_fingerprint="sha256:authority",
            prompt_versions_json="{}",
            status="completed",
            attempt_count=1,
            selected_attempt_ordinal=1,
            became_authoritative=True,
            started_at=now,
            completed_at=now,
        )
        session.add(authoritative)
        session.flush()
        session.add(
            MimoRecognitionAttempt(
                run_id=authoritative.id,
                ordinal=1,
                status="completed",
                duration_ms=125,
                response_fingerprint="a" * 64,
                started_at=now,
                completed_at=now,
            )
        )
        payload = {
            "contract_version": "mimo-authoritative-v2",
            "summary": "管理已有 ETH 空单并移动止损",
            "confidence": 0.94,
            "intents": [
                {
                    "intent_type": "position_management",
                    "action": {
                        "kind": "move_stop_to_protect",
                        "target": {"lifecycle_id": 790, "thread_id": 52},
                        "strategy": None,
                        "parameters": {"stop_loss": "1940"},
                    },
                    "reason": "消息明确要求移动止损到1940",
                    "confidence": 0.95,
                    "evidence_refs": ["text:stop_loss", f"image:{media.id}:symbol"],
                }
            ],
            "evidence": {
                "text": {
                    "observed_text": "移动止损到1940",
                    "fields": {
                        "stop_loss": {
                            "value": "1940",
                            "source": "text",
                            "confidence": 0.99,
                        }
                    },
                },
                "images": [
                    {
                        "asset_id": media.id,
                        "image_type": "position_screenshot",
                        "quality": "clear",
                        "observed_text": "ETHUSDT 永续，空，止损1940",
                        "summary": "ETHUSDT空仓持仓截图",
                        "fields": {
                            "symbol": {
                                "value": "ETH",
                                "source": "image",
                                "confidence": 0.99,
                            }
                        },
                        "confidence": 0.97,
                    }
                ],
                "conflicts": [],
            },
        }
        session.add(
            MessageEvidenceVersion(
                raw_message_id=raw.id,
                mimo_recognition_run_id=authoritative.id,
                version=1,
                input_fingerprint="sha256:authority",
                model="mimo-v2.5",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=0.94,
                text_evidence_json=json.dumps(payload["evidence"]["text"]),
                image_evidence_json=json.dumps(
                    {
                        "images": payload["evidence"]["images"],
                        "conflicts": [],
                    }
                ),
                normalized_evidence_json=json.dumps(
                    {
                        key: payload[key]
                        for key in ("contract_version", "summary", "confidence", "intents")
                    }
                ),
            )
        )
        session.add(
            ContextResolutionAttempt(
                raw_message_id=raw.id,
                context_fingerprint="sha256:context",
                model="deepseek-v4-flash",
                prompt_versions_json="{}",
                request_summary_json="{}",
                decision_json=json.dumps(
                    {
                        "decision": "linked",
                        "confidence": 0.91,
                        "supporting_message_ids": [],
                        "opposing_message_ids": [],
                    }
                ),
                status="completed",
            )
        )
        session.add(
            RecognitionDecision(
                raw_message_id=raw.id,
                input_kind="text+image",
                authoritative_model="mimo-v2.5",
                authoritative_status="非策略",
                authoritative_payload_json="{}",
                agreement_status="agreed",
                differences_json="[]",
                comparison_status="completed",
                disagreement_severity="none",
                comparison_model="deepseek-v4-flash",
                comparison_payload_json=json.dumps(
                    {"reason": "DeepSeek 同意该仓位管理判断。", "conflict_types": []}
                ),
                automation_status="failed",
                automation_reason="target_unresolved",
            )
        )
        session.add(
            MessageRecognition(
                raw_message_id=raw.id,
                status="非策略",
                reason="legacy compatibility status",
                engine="mimo-v2.5",
            )
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    )

    assert response.status_code == 200
    order = [
        'class="message-text"',
        'class="media-list"',
        'data-mimo-current-authority',
        'data-mimo-image-evidence',
        'aria-label="上下文二次判断"',
        'data-system-acceptance',
        'data-automatic-trading-result',
        'data-auxiliary-review',
        'data-legacy-debug',
    ]
    positions = [response.text.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert "当前权威 MiMo 分析" in response.text
    assert "仓位管理" in response.text
    assert "移动止损保护" in response.text
    assert "ETHUSDT空仓持仓截图" in response.text
    assert "系统未安全接纳" in response.text
    assert "未执行" in response.text
    assert 'data-message-default-expanded="true"' in response.text
    assert '<details class="mimo-runtime-details"' in response.text
    assert '<details class="mimo-raw-evidence"' in response.text
    assert "data-mimo-toggle" not in response.text


def test_group_messages_route_keeps_v1_authority_and_shows_latest_failed_call(
    tmp_path,
):
    database_path = tmp_path / "latest-failed.db"
    session_factory = create_session_factory(database_path)
    now = datetime(2026, 8, 11, 20, 0)
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=51, text="BTC long")
        session.add(raw)
        session.flush()
        authority = MimoRecognitionRun(
            raw_message_id=raw.id,
            run_kind="v1_authoritative",
            contract_version="v1",
            model="mimo-v2.5",
            input_kind="text",
            input_fingerprint="sha256:v1",
            prompt_versions_json="{}",
            status="completed",
            attempt_count=1,
            selected_attempt_ordinal=1,
            became_authoritative=True,
            started_at=now,
            completed_at=now,
        )
        session.add(authority)
        session.flush()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=raw.id,
                mimo_recognition_run_id=authority.id,
                version=1,
                input_fingerprint="sha256:v1",
                model="mimo-v2.5",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=0.8,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json=json.dumps(
                    {"summary": "当前仍采用 v1 权威结果", "confidence": 0.8}
                ),
            )
        )
        failed = MimoRecognitionRun(
            raw_message_id=raw.id,
            run_kind="v2_authoritative",
            contract_version="mimo-authoritative-v2",
            model="mimo-v2.5",
            input_kind="text",
            input_fingerprint="sha256:v2",
            prompt_versions_json="{}",
            status="failed",
            attempt_count=2,
            became_authoritative=False,
            final_error_code="provider_timeout",
            final_error_message="provider request timed out",
            started_at=now,
            completed_at=now,
        )
        session.add(failed)
        session.flush()
        session.add_all(
            [
                MimoRecognitionAttempt(
                    run_id=failed.id,
                    ordinal=ordinal,
                    status="timeout",
                    duration_ms=100,
                    error_code="provider_timeout",
                    error_message="provider request timed out",
                    started_at=now,
                    completed_at=now,
                )
                for ordinal in (1, 2)
            ]
        )
        session.add(
            RecognitionDecision(
                raw_message_id=raw.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="是策略",
                authoritative_payload_json='{"reason":"当前仍采用 v1 权威结果"}',
                agreement_status="authoritative_only",
                differences_json="[]",
            )
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    )

    assert response.status_code == 200
    assert "MiMo v1结果" in response.text
    assert "当前仍采用 v1 权威结果" in response.text
    assert "最新 MiMo 调用失败" in response.text
    assert "provider_timeout" in response.text
    assert "provider request timed out" in response.text
    assert response.text.index("当前权威 MiMo 分析") < response.text.index(
        "最新 MiMo 调用失败"
    )


def test_group_messages_route_labels_historical_v1_and_escapes_mimo_content(
    tmp_path,
):
    database_path = tmp_path / "historical-v1.db"
    session_factory = create_session_factory(database_path)
    unsafe_summary = "<script>alert('mimo')</script>"
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=52, text="historical")
        session.add(raw)
        session.flush()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=raw.id,
                version=1,
                input_fingerprint="sha256:historical",
                model="mimo-v2.5",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=0.0,
                text_evidence_json=json.dumps({"observed_text": unsafe_summary}),
                image_evidence_json="{}",
                normalized_evidence_json=json.dumps(
                    {"summary": unsafe_summary, "confidence": 0.0}
                ),
            )
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    )

    assert response.status_code == 200
    assert "MiMo 历史结果 · v1格式" in response.text
    assert "置信度 0.0" in response.text
    assert "历史记录未保存 MiMo 调用尝试明细" in response.text
    assert "历史记录未保存逐图 MiMo 证据" in response.text
    assert unsafe_summary not in response.text
    assert "&lt;script&gt;alert" in response.text

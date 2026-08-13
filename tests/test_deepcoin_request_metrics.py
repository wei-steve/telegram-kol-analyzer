from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_request_metrics import (
    DeepcoinRequestMetricsIncomplete,
    project_deepcoin_request_health,
)
from telegram_kol_research.models import (
    DeepcoinExecutionOperation,
    DeepcoinRequestAttempt,
    TradeSignal,
)


def _signal(session, *, message_id: int) -> TradeSignal:
    row = TradeSignal(
        signal_uid=f"health-signal-{message_id}",
        strategy_instance_id=f"health-strategy-{message_id}",
        source_type="message_instruction",
        venue="deepcoin",
        kol_id="alice",
        chat_id=88,
        message_id=message_id,
        symbol="BTC",
        side="long",
        action="open_position",
        status="recovery_required",
        payload_json="{}",
    )
    session.add(row)
    session.flush()
    return row


def _operation(
    session,
    *,
    signal: TradeSignal,
    state: str,
    certainty: str,
    created_at: datetime,
    ordinal: int,
) -> DeepcoinExecutionOperation:
    phase = {
        "pre_submit_deferred": "next_leg_preflight",
        "entry_unknown": "entry_readback",
        "protection_unknown": "protection_readback",
        "recovery_required": "reconciliation",
    }[state]
    row = DeepcoinExecutionOperation(
        operation_key=(
            f"protected-entry:v1:signal:{signal.id}:leg:{ordinal}:entry"
        ),
        trade_signal_id=signal.id,
        contract_version="1",
        phase=phase,
        state=state,
        outcome_certainty=certainty,
        error_category=(
            "transport_timeout" if certainty == "unknown" else None
        ),
        reason_code=f"health_{state}",
        request_fingerprint=str(ordinal) * 64,
        economics_fingerprint=str(ordinal + 3) * 64,
        deadline_at=created_at + timedelta(seconds=10),
        writer_attempted_at=(
            created_at if certainty == "unknown" else None
        ),
        attempt_count=0,
        evidence_json="{}",
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(row)
    session.flush()
    return row


def _attempt(
    session,
    *,
    operation: DeepcoinExecutionOperation,
    ordinal: int,
    method: str,
    path: str,
    phase: str,
    certainty: str,
    started_at: datetime,
    wait_ms: int = 0,
    retry_ms: int = 0,
    latency_ms: int = 0,
    error_category: str | None = None,
    safe_code: str = "ok",
    http_status: int | None = 200,
) -> None:
    session.add(
        DeepcoinRequestAttempt(
            deepcoin_execution_operation_id=operation.id,
            ordinal=ordinal,
            method=method,
            normalized_path=path,
            priority="background",
            phase=phase,
            outcome_certainty=certainty,
            error_category=error_category,
            safe_code=safe_code,
            http_status=http_status,
            business_code=None,
            governor_wait_ms=wait_ms,
            retry_delay_ms=retry_ms,
            latency_ms=latency_ms,
            uid_scope_hash="a" * 64,
            request_fingerprint=operation.request_fingerprint,
            started_at=started_at,
            completed_at=started_at + timedelta(milliseconds=latency_ms),
            created_at=started_at,
        )
    )


def test_deepcoin_request_health_projects_bounded_redacted_metrics(tmp_path):
    session_factory = create_session_factory(tmp_path / "health.db")
    since = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    until = since + timedelta(minutes=15)
    with session_factory() as session:
        deferred = _operation(
            session,
            signal=_signal(session, message_id=1),
            state="pre_submit_deferred",
            certainty="not_sent",
            created_at=since + timedelta(seconds=10),
            ordinal=1,
        )
        unknown_entry = _operation(
            session,
            signal=_signal(session, message_id=2),
            state="entry_unknown",
            certainty="unknown",
            created_at=since + timedelta(seconds=20),
            ordinal=2,
        )
        unprotected = _operation(
            session,
            signal=_signal(session, message_id=3),
            state="protection_unknown",
            certainty="unknown",
            created_at=since + timedelta(minutes=5),
            ordinal=3,
        )
        _operation(
            session,
            signal=_signal(session, message_id=4),
            state="protection_unknown",
            certainty="unknown",
            created_at=since - timedelta(minutes=5),
            ordinal=4,
        )
        _operation(
            session,
            signal=_signal(session, message_id=5),
            state="pre_submit_deferred",
            certainty="not_sent",
            created_at=since - timedelta(minutes=5),
            ordinal=5,
        )
        _operation(
            session,
            signal=_signal(session, message_id=6),
            state="recovery_required",
            certainty="unknown",
            created_at=since - timedelta(minutes=10),
            ordinal=6,
        )
        _attempt(
            session,
            operation=deferred,
            ordinal=1,
            method="GET",
            path="/deepcoin/account/positions",
            phase="entry_preflight",
            certainty="confirmed",
            started_at=since + timedelta(seconds=30),
            wait_ms=0,
            latency_ms=100,
        )
        _attempt(
            session,
            operation=deferred,
            ordinal=2,
            method="GET",
            path="/deepcoin/account/positions",
            phase="entry_preflight",
            certainty="unknown",
            started_at=since + timedelta(seconds=31),
            wait_ms=200,
            retry_ms=125,
            latency_ms=50,
            error_category="rate_limited",
            safe_code="http_429",
            http_status=429,
        )
        _attempt(
            session,
            operation=deferred,
            ordinal=3,
            method="GET",
            path="/deepcoin/trade/order/123456789",
            phase="reconciliation",
            certainty="confirmed",
            started_at=since + timedelta(seconds=32),
            wait_ms=300,
            latency_ms=0,
        )
        _attempt(
            session,
            operation=unknown_entry,
            ordinal=1,
            method="POST",
            path="/deepcoin/trade/order",
            phase="entry_submit",
            certainty="unknown",
            started_at=since + timedelta(minutes=1),
            wait_ms=100,
            latency_ms=80,
            error_category="transport_timeout",
            safe_code="transport_timeout",
            http_status=None,
        )
        _attempt(
            session,
            operation=unprotected,
            ordinal=1,
            method="GET",
            path="/deepcoin/trade/trigger-orders-pending",
            phase="protection_readback",
            certainty="unknown",
            started_at=since + timedelta(minutes=6),
            wait_ms=300,
            latency_ms=150,
            error_category="schema_invalid",
            safe_code="schema_invalid",
            http_status=200,
        )
        _attempt(
            session,
            operation=unprotected,
            ordinal=2,
            method="GET",
            path="/deepcoin/trade/trigger-orders-pending",
            phase="protection_readback",
            certainty="unknown",
            started_at=since + timedelta(minutes=6, seconds=1),
            wait_ms=400,
            latency_ms=200,
            error_category="schema_incompatible",
            safe_code="schema_incompatible",
            http_status=200,
        )
        for attempt_ordinal, offset in ((3, 2), (4, 7)):
            _attempt(
                session,
                operation=unprotected,
                ordinal=attempt_ordinal,
                method="GET",
                path="/deepcoin/trade/orders-pending",
                phase="reconciliation",
                certainty="not_sent",
                started_at=since + timedelta(minutes=6, seconds=offset),
                wait_ms=500 + attempt_ordinal,
                latency_ms=0,
                error_category="state_conflict",
                safe_code="circuit_open",
                http_status=None,
            )
        session.commit()

    result = project_deepcoin_request_health(
        session_factory,
        since=since,
        until=until,
        max_attempts=100,
        max_operations=100,
    )

    assert result["complete"] is True
    assert result["request_count"] == 8
    assert result["request_count_by_endpoint_phase"] == [
        {
            "endpoint": "/deepcoin/account/positions",
            "phase": "entry_preflight",
            "count": 2,
        },
        {
            "endpoint": "/deepcoin/trade/order",
            "phase": "entry_submit",
            "count": 1,
        },
        {
            "endpoint": "/deepcoin/trade/orders-pending",
            "phase": "reconciliation",
            "count": 2,
        },
        {
            "endpoint": "/deepcoin/trade/trigger-orders-pending",
            "phase": "protection_readback",
            "count": 2,
        },
        {
            "endpoint": "/deepcoin/unknown",
            "phase": "reconciliation",
            "count": 1,
        },
    ]
    assert result["limiter_wait_ms"] == {"p50": 300, "p95": 504, "max": 504}
    assert result["retry_count"] == 1
    assert result["http_429_count"] == 1
    assert result["unavailable_count"] == 2
    assert result["schema_invalid_count"] == 1
    assert result["schema_incompatible_count"] == 1
    assert result["readback_latency_ms"] == {"p50": 100, "p95": 200, "max": 200}
    assert result["open_circuit"] == {
        "available": True,
        "count": 2,
        "duration_ms": 0,
    }
    assert result["unknown_writer_count"] == 1
    assert result["pre_submit_deferred_count"] == 1
    assert result["live_unprotected"] == {
        "count": 3,
        "max_duration_seconds": 1500,
        "total_duration_seconds": 3300,
    }
    assert "signal_uid" not in repr(result)
    assert "strategy_instance_id" not in repr(result)
    assert "operation_id" not in repr(result)


def test_deepcoin_request_health_rejects_truncated_attempt_or_operation_scan(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "truncated.db")
    since = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        signal = _signal(session, message_id=1)
        operation = _operation(
            session,
            signal=signal,
            state="entry_unknown",
            certainty="unknown",
            created_at=since,
            ordinal=1,
        )
        for ordinal in range(1, 4):
            _attempt(
                session,
                operation=operation,
                ordinal=ordinal,
                method="GET",
                path="/deepcoin/account/positions",
                phase="entry_readback",
                certainty="unknown",
                started_at=since + timedelta(seconds=ordinal),
            )
        session.commit()

    with pytest.raises(
        DeepcoinRequestMetricsIncomplete,
        match="metric_attempt_scan_truncated",
    ):
        project_deepcoin_request_health(
            session_factory,
            since=since,
            until=since + timedelta(minutes=15),
            max_attempts=2,
            max_operations=100,
        )
    with pytest.raises(
        DeepcoinRequestMetricsIncomplete,
        match="metric_operation_scan_truncated",
    ):
        project_deepcoin_request_health(
            session_factory,
            since=since,
            until=since + timedelta(minutes=15),
            max_attempts=100,
            max_operations=0,
        )


def test_deepcoin_request_health_rejects_malformed_timestamp_excluded_by_window(
    tmp_path,
):
    database_path = tmp_path / "malformed-time.db"
    session_factory = create_session_factory(database_path)
    since = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        operation = _operation(
            session,
            signal=_signal(session, message_id=1),
            state="entry_unknown",
            certainty="unknown",
            created_at=since,
            ordinal=1,
        )
        _attempt(
            session,
            operation=operation,
            ordinal=1,
            method="GET",
            path="/deepcoin/account/positions",
            phase="entry_readback",
            certainty="unknown",
            started_at=since + timedelta(seconds=1),
        )
        session.commit()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE deepcoin_request_attempts SET started_at = 'oops'"
        )
        connection.commit()

    with pytest.raises(
        DeepcoinRequestMetricsIncomplete,
        match="metric_datetime_evidence_invalid",
    ):
        project_deepcoin_request_health(
            session_factory,
            since=since,
            until=since + timedelta(minutes=15),
            max_attempts=100,
            max_operations=100,
        )


@pytest.mark.parametrize(
    ("table_name", "assignment"),
    [
        (
            "deepcoin_request_attempts",
            "completed_at = '2026-08-13 11:59:00'",
        ),
        (
            "deepcoin_execution_operations",
            "updated_at = '2026-08-13 11:59:00'",
        ),
        (
            "deepcoin_execution_operations",
            "writer_attempted_at = '2026-08-13 12:16:00'",
        ),
    ],
)
def test_deepcoin_request_health_rejects_illegal_time_relationships(
    tmp_path,
    table_name,
    assignment,
):
    database_path = tmp_path / f"illegal-{table_name}.db"
    session_factory = create_session_factory(database_path)
    since = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        operation = _operation(
            session,
            signal=_signal(session, message_id=1),
            state="entry_unknown",
            certainty="unknown",
            created_at=since,
            ordinal=1,
        )
        _attempt(
            session,
            operation=operation,
            ordinal=1,
            method="GET",
            path="/deepcoin/account/positions",
            phase="entry_readback",
            certainty="unknown",
            started_at=since + timedelta(seconds=1),
        )
        session.commit()

    with sqlite3.connect(database_path) as connection:
        connection.execute(f"UPDATE {table_name} SET {assignment}")
        connection.commit()

    with pytest.raises(
        DeepcoinRequestMetricsIncomplete,
        match="metric_.*_evidence_invalid",
    ):
        project_deepcoin_request_health(
            session_factory,
            since=since,
            until=since + timedelta(minutes=15),
            max_attempts=100,
            max_operations=100,
        )


@pytest.mark.parametrize(
    "assignment",
    [
        "outcome_certainty = 'confirmed'",
        "phase = 'not-a-closed-phase'",
    ],
)
def test_deepcoin_request_health_rejects_operation_tuple_conflict(
    tmp_path,
    assignment,
):
    database_path = tmp_path / "operation-tuple.db"
    session_factory = create_session_factory(database_path)
    since = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        _operation(
            session,
            signal=_signal(session, message_id=1),
            state="protection_unknown",
            certainty="unknown",
            created_at=since,
            ordinal=1,
        )
        session.commit()

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE deepcoin_execution_operations SET {assignment}"
        )
        connection.commit()

    with pytest.raises(
        DeepcoinRequestMetricsIncomplete,
        match="metric_operation_evidence_invalid",
    ):
        project_deepcoin_request_health(
            session_factory,
            since=since,
            until=since + timedelta(minutes=15),
            max_attempts=100,
            max_operations=100,
        )

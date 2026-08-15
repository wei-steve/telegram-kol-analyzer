from __future__ import annotations

import sqlite3
import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_kol_research.production_monitor_facts import (
    FACT_STATUS_COMPLETE,
    FACT_STATUS_UNKNOWN,
    MonitorReadinessEvidence,
    build_coverage_candidates,
    build_readiness_candidates,
    build_settings_candidates,
    parse_monitor_readiness_projection,
    read_journal_candidates,
    read_local_monitor_facts,
    select_usable_snapshot_generations,
)
from telegram_kol_research.production_monitor_snapshot import (
    SnapshotCollectionEvidence,
    SnapshotGeneration,
    SnapshotManifest,
)
from telegram_kol_research.production_monitor_sentinel import (
    SentinelObservation,
    evaluate_sentinel_observation,
)
from telegram_kol_research.production_monitor_state import ProductionMonitorState


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _create_fact_database(path, *, wal: bool = False) -> None:
    with sqlite3.connect(path) as connection:
        if wal:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.executescript(
            """
            CREATE TABLE strategy_management_batches (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                reason_code TEXT,
                last_progress_at TEXT,
                execution_deadline_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE strategy_management_components (
                id INTEGER PRIMARY KEY,
                management_batch_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                last_progress_at TEXT,
                execution_deadline_at TEXT,
                updated_at TEXT NOT NULL,
                strategy_management_leg_id INTEGER,
                component_kind TEXT NOT NULL,
                desired_json TEXT NOT NULL
            );
            CREATE TABLE strategy_management_legs (
                id INTEGER PRIMARY KEY,
                management_batch_id INTEGER NOT NULL,
                pos_id TEXT NOT NULL
            );
            CREATE TABLE instruction_execution_contracts (
                id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                last_progress_at TEXT,
                deadline_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE deepcoin_execution_operations (
                id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                updated_at TEXT,
                deadline_at TEXT
            );
            CREATE TABLE entry_preambles (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def _snapshot(
    *,
    generation: int = 7,
    started_at=NOW - timedelta(seconds=2),
    positions=(),
):
    collections = tuple(
        SnapshotCollectionEvidence(
            name=name,
            available=True,
            schema_valid=True,
            complete=True,
            page_count=1,
            row_count=len(positions) if name == "positions" else 0,
            rows=tuple(positions) if name == "positions" else (),
        )
        for name in ("positions", "open_orders", "pending_trigger_orders")
    )
    return SnapshotGeneration(
        generation=generation,
        outcome="SUCCESS",
        request_started_at=started_at,
        request_completed_at=started_at + timedelta(seconds=1),
        uid_scope_hash="a" * 64,
        collections=collections,
    )


def _insert_active_component(
    path,
    *,
    component_id: int = 1,
    status: str = "awaiting_exchange",
    progress=NOW - timedelta(minutes=2),
    deadline=NOW - timedelta(minutes=1),
    component_kind: str = "consume_take_profit_stage",
    desired: dict | None = None,
    pos_id: str = "pos-1",
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO strategy_management_batches "
            "(id, status, reason_code, last_progress_at, execution_deadline_at, updated_at) "
            "VALUES (1, 'executing', NULL, ?, ?, ?)",
            (
                progress.isoformat() if isinstance(progress, datetime) else progress,
                deadline.isoformat() if isinstance(deadline, datetime) else deadline,
                NOW.isoformat(),
            ),
        )
        connection.execute(
            "INSERT OR IGNORE INTO strategy_management_legs "
            "(id, management_batch_id, pos_id) VALUES (1, 1, ?)",
            (pos_id,),
        )
        connection.execute(
            "INSERT INTO strategy_management_components "
            "(id, management_batch_id, status, last_progress_at, execution_deadline_at, "
            "updated_at, strategy_management_leg_id, component_kind, desired_json) "
            "VALUES (?, 1, ?, ?, ?, ?, 1, ?, ?)",
            (
                component_id,
                status,
                progress.isoformat() if isinstance(progress, datetime) else progress,
                deadline.isoformat() if isinstance(deadline, datetime) else deadline,
                NOW.isoformat(),
                component_kind,
                json.dumps(desired or {}),
            ),
        )


def test_local_facts_use_uri_read_only_query_only_and_explicit_transaction(tmp_path):
    database = tmp_path / "research.db"
    _create_fact_database(database)
    calls = []

    class RecordingConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, parameters=()):
            calls.append(" ".join(sql.split()))
            return self._connection.execute(sql, parameters)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._connection.close()

    def connect(path, *, uri):
        calls.append((path, uri))
        return RecordingConnection(sqlite3.connect(path, uri=uri))

    result = read_local_monitor_facts(
        database,
        snapshot_generations=(),
        now=NOW,
        connect=connect,
    )

    assert result.status == FACT_STATUS_COMPLETE
    assert calls[0] == (f"file:{database}?mode=ro", True)
    assert "PRAGMA query_only=ON" in calls
    assert "BEGIN" in calls
    assert all(
        " LIMIT " in f" {statement.upper()} "
        for statement in calls
        if isinstance(statement, str) and statement.lstrip().upper().startswith("SELECT")
    )


def test_wal_writer_between_queries_cannot_mix_local_generations(tmp_path):
    database = tmp_path / "research.db"
    _create_fact_database(database, wal=True)
    _insert_active_component(database)
    hook_calls = []

    def writer_hook():
        hook_calls.append(True)
        with sqlite3.connect(database) as writer:
            writer.execute(
                "UPDATE strategy_management_batches SET status='succeeded' WHERE id=1"
            )
            writer.execute(
                "UPDATE strategy_management_components SET status='confirmed' WHERE id=1"
            )

    result = read_local_monitor_facts(
        database,
        snapshot_generations=(
            _snapshot(started_at=NOW - timedelta(seconds=2)),
        ),
        now=NOW,
        between_queries_hook=writer_hook,
    )

    assert hook_calls == [True]
    assert result.status == FACT_STATUS_COMPLETE
    assert len([item for item in result.candidates if item.anomaly_present]) == 2
    assert all(
        item.evidence_complete is False
        for item in result.candidates
        if item.anomaly_present
    )


@pytest.mark.parametrize(
    ("mutation", "unknown_code"),
    [
        ("DROP TABLE strategy_management_components", "database_schema_missing"),
        (
            "ALTER TABLE strategy_management_components RENAME COLUMN status TO other",
            "database_schema_invalid",
        ),
    ],
)
def test_missing_table_or_column_is_typed_unknown(tmp_path, mutation, unknown_code):
    database = tmp_path / "research.db"
    _create_fact_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(mutation)

    result = read_local_monitor_facts(
        database, snapshot_generations=(), now=NOW
    )

    assert result.status == FACT_STATUS_UNKNOWN
    assert result.unknown_code == unknown_code
    assert result.candidates == ()


def test_overflow_bad_time_and_unknown_status_are_typed_unknown(tmp_path):
    cases = []
    for name in ("overflow", "bad-time", "unknown-status"):
        database = tmp_path / f"{name}.db"
        _create_fact_database(database)
        if name == "overflow":
            _insert_active_component(database, component_id=1)
            _insert_active_component(database, component_id=2)
            kwargs = {"row_limit": 1}
            expected = "database_row_limit_exceeded"
        elif name == "bad-time":
            _insert_active_component(database, progress="not-a-time")
            kwargs = {}
            expected = "database_timestamp_invalid"
        else:
            _insert_active_component(database, status="future_status")
            kwargs = {}
            expected = "database_status_invalid"
        result = read_local_monitor_facts(
            database, snapshot_generations=(), now=NOW, **kwargs
        )
        cases.append((result.status, result.unknown_code, result.candidates))
        assert result.unknown_code == expected

    assert cases == [
        (FACT_STATUS_UNKNOWN, "database_row_limit_exceeded", ()),
        (FACT_STATUS_UNKNOWN, "database_timestamp_invalid", ()),
        (FACT_STATUS_UNKNOWN, "database_status_invalid", ()),
    ]


def test_settling_fact_uses_only_row_deadline_progress_and_snapshot_window(tmp_path):
    database = tmp_path / "research.db"
    _create_fact_database(database)
    progress = NOW - timedelta(minutes=4)
    deadline = NOW - timedelta(minutes=3)
    _insert_active_component(database, progress=progress, deadline=deadline)
    snapshot = _snapshot(started_at=deadline + timedelta(microseconds=1))

    result = read_local_monitor_facts(
        database,
        snapshot_generations=(snapshot,),
        now=NOW,
    )

    settling = [
        item
        for item in result.candidates
        if item.reason_code == "stalled_composite_component"
    ]
    assert len(settling) == 2
    for candidate in settling:
        assert candidate.last_progress_at == progress
        assert candidate.execution_deadline_at == deadline
        assert candidate.snapshot_started_at == snapshot.request_started_at
        assert candidate.snapshot_started_at > deadline
        assert candidate.snapshot_started_at > progress


@pytest.mark.parametrize("missing", ["deadline", "progress"])
def test_normal_null_settling_authority_remains_unknown_without_guess(tmp_path, missing):
    database = tmp_path / "research.db"
    _create_fact_database(database)
    _insert_active_component(
        database,
        progress=None if missing == "progress" else NOW - timedelta(days=2),
        deadline=None if missing == "deadline" else NOW - timedelta(days=1),
    )

    result = read_local_monitor_facts(
        database,
        snapshot_generations=(_snapshot(),),
        now=NOW,
    )

    assert result.status == FACT_STATUS_COMPLETE
    settling = [
        item
        for item in result.candidates
        if item.reason_code == "stalled_composite_component"
    ]
    assert len(settling) == 2
    for candidate in settling:
        assert (
            candidate.execution_deadline_at is None
            if missing == "deadline"
            else candidate.last_progress_at is None
        )


def test_active_management_row_with_real_null_deadline_is_explicit_unknown_fact(
    tmp_path,
):
    database = tmp_path / "research.db"
    _create_fact_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO strategy_management_batches "
            "(id, status, reason_code, last_progress_at, execution_deadline_at, updated_at) "
            "VALUES (1, 'ready', NULL, ?, NULL, ?)",
            ((NOW - timedelta(minutes=10)).isoformat(), NOW.isoformat()),
        )

    result = read_local_monitor_facts(
        database,
        snapshot_generations=(_snapshot(),),
        now=NOW,
    )

    assert result.status == FACT_STATUS_COMPLETE
    settling = [
        item
        for item in result.candidates
        if item.reason_code == "stalled_composite_component"
    ]
    assert len(settling) == 1
    assert settling[0].execution_deadline_at is None


def test_durable_terminal_local_fact_does_not_require_exchange_snapshot(tmp_path):
    database = tmp_path / "research.db"
    _create_fact_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO strategy_management_batches "
            "(id, status, reason_code, last_progress_at, execution_deadline_at, updated_at) "
            "VALUES (1, 'succeeded', NULL, ?, ?, ?)",
            (
                (NOW - timedelta(minutes=2)).isoformat(),
                (NOW - timedelta(minutes=1)).isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO strategy_management_components "
            "(id, management_batch_id, status, last_progress_at, execution_deadline_at, updated_at, "
            "strategy_management_leg_id, component_kind, desired_json) "
            "VALUES (1, 1, 'confirmed', ?, ?, ?, NULL, 'consume_take_profit_stage', '{}')",
            (
                (NOW - timedelta(minutes=2)).isoformat(),
                (NOW - timedelta(minutes=1)).isoformat(),
                NOW.isoformat(),
            ),
        )

    result = read_local_monitor_facts(
        database,
        snapshot_generations=(),
        now=NOW,
    )

    assert len(result.candidates) == 3
    assert all(item.anomaly_present is False for item in result.candidates)
    assert all(item.durable_terminal_fact is True for item in result.candidates)
    assert all(item.evidence_complete is True for item in result.candidates)


def _readiness(**overrides) -> MonitorReadinessEvidence:
    values = {
        "service_generation": "b" * 64,
        "deepcoin_reconcile_first_success_at": NOW - timedelta(minutes=2),
        "deepcoin_reconcile_last_success_at": NOW - timedelta(seconds=20),
        "management_worker_last_success_at": NOW - timedelta(seconds=10),
        "message_supervisor_last_success_at": NOW - timedelta(seconds=5),
        "message_supervisor_policy_status": "valid",
    }
    values.update(overrides)
    return MonitorReadinessEvidence(**values)


def test_readiness_projection_has_exact_closed_fields():
    payload = {
        "service_generation": "b" * 64,
        "deepcoin_reconcile_first_success_at": NOW.isoformat(),
        "deepcoin_reconcile_last_success_at": NOW.isoformat(),
        "management_worker_last_success_at": NOW.isoformat(),
        "message_supervisor_last_success_at": NOW.isoformat(),
        "message_supervisor_policy_status": "valid",
    }

    parsed = parse_monitor_readiness_projection(payload)

    assert parsed.service_generation == "b" * 64
    with pytest.raises(ValueError):
        parse_monitor_readiness_projection({**payload, "database_path": "/secret"})
    with pytest.raises(ValueError):
        parse_monitor_readiness_projection(
            {**payload, "message_supervisor_policy_status": "future"}
        )


def test_readiness_needs_successful_cycles_not_elapsed_startup_time():
    candidates = build_readiness_candidates(
        _readiness(
            deepcoin_reconcile_first_success_at=None,
            deepcoin_reconcile_last_success_at=None,
        ),
        now=NOW + timedelta(days=1),
    )

    assert [item.reason_code for item in candidates if item.anomaly_present] == [
        "service_starting"
    ]


def test_readiness_stale_future_restart_and_disabled_are_conservative():
    stale = build_readiness_candidates(
        _readiness(
            message_supervisor_last_success_at=NOW - timedelta(minutes=6)
        ),
        now=NOW,
    )
    future = build_readiness_candidates(
        _readiness(management_worker_last_success_at=NOW + timedelta(seconds=1)),
        now=NOW,
    )
    restarted = build_readiness_candidates(
        _readiness(
            service_generation="c" * 64,
            deepcoin_reconcile_first_success_at=None,
            deepcoin_reconcile_last_success_at=None,
            management_worker_last_success_at=None,
            message_supervisor_last_success_at=None,
        ),
        now=NOW,
    )
    disabled = build_readiness_candidates(
        _readiness(message_supervisor_policy_status="disabled"), now=NOW
    )

    assert [item.reason_code for item in stale if item.anomaly_present] == [
        "message_operation_supervisor_stale"
    ]
    assert [item.reason_code for item in future if item.anomaly_present] == ["readiness_unavailable"]
    assert [item.reason_code for item in restarted if item.anomaly_present] == ["service_starting"]
    assert [item.reason_code for item in disabled if item.anomaly_present] == [
        "message_operation_supervisor_policy_invalid"
    ]


def test_complete_readiness_emits_stable_clear_candidates():
    candidates = build_readiness_candidates(_readiness(), now=NOW)

    assert len(candidates) == 4
    assert all(item.anomaly_present is False for item in candidates)


def test_readiness_fingerprints_survive_process_generation_change():
    old = build_readiness_candidates(
        _readiness(deepcoin_reconcile_first_success_at=None), now=NOW
    )
    new = build_readiness_candidates(
        _readiness(
            service_generation="c" * 64,
            deepcoin_reconcile_first_success_at=None,
        ),
        now=NOW,
    )

    assert [item.fingerprint for item in old] == [item.fingerprint for item in new]


def test_starting_readiness_resolves_after_complete_success_evidence():
    first_observation = SentinelObservation(
        checked_at=NOW,
        candidates=build_readiness_candidates(
            _readiness(deepcoin_reconcile_first_success_at=None), now=NOW
        ),
        adapter_failures=(),
    )
    first, state = evaluate_sentinel_observation(
        observation=first_observation,
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )
    complete_at = NOW + timedelta(minutes=1)
    second_observation = SentinelObservation(
        checked_at=complete_at,
        candidates=build_readiness_candidates(
            _readiness(service_generation="c" * 64), now=complete_at
        ),
        adapter_failures=(),
    )
    second, _ = evaluate_sentinel_observation(
        observation=second_observation,
        previous_state=state,
        observation_generation=2,
    )

    assert first.observed_health == "UNKNOWN"
    assert second.observed_health == "HEALTHY"


def test_immediate_database_fact_has_stable_terminal_clear(tmp_path):
    database = tmp_path / "research.db"
    _create_fact_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO instruction_execution_contracts "
            "(id, state, last_progress_at, deadline_at, updated_at) "
            "VALUES (1, 'submit_unknown', ?, ?, ?)",
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
    bad = read_local_monitor_facts(database, snapshot_generations=(), now=NOW)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE instruction_execution_contracts "
            "SET state='verified', updated_at=? WHERE id=1",
            ((NOW + timedelta(seconds=1)).isoformat(),),
        )
    clear = read_local_monitor_facts(
        database, snapshot_generations=(), now=NOW + timedelta(seconds=1)
    )

    assert len(bad.candidates) == len(clear.candidates) == 1
    assert bad.candidates[0].fingerprint == clear.candidates[0].fingerprint
    assert bad.candidates[0].anomaly_present is True
    assert clear.candidates[0].anomaly_present is False
    assert clear.candidates[0].durable_terminal_fact is True
    bad_result, state = evaluate_sentinel_observation(
        observation=SentinelObservation(
            checked_at=NOW,
            candidates=bad.candidates,
            adapter_failures=(),
        ),
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )
    clear_result, _ = evaluate_sentinel_observation(
        observation=SentinelObservation(
            checked_at=NOW + timedelta(seconds=1),
            candidates=clear.candidates,
            adapter_failures=(),
        ),
        previous_state=state,
        observation_generation=2,
    )
    assert bad_result.observed_health == "UNHEALTHY"
    assert clear_result.observed_health == "HEALTHY"


def test_historical_or_active_fact_overflow_is_typed_unknown(
    tmp_path,
):
    database = tmp_path / "research.db"
    _create_fact_database(database)
    old = (NOW - timedelta(days=2)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO instruction_execution_contracts "
            "(id, state, last_progress_at, deadline_at, updated_at) "
            "VALUES (?, 'verified', ?, ?, ?)",
            [(row_id, old, old, old) for row_id in range(1, 130)],
        )
    pruned = read_local_monitor_facts(database, snapshot_generations=(), now=NOW)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO deepcoin_execution_operations "
            "(id, state, updated_at, deadline_at) VALUES (?, 'planned', ?, ?)",
            [(row_id, NOW.isoformat(), NOW.isoformat()) for row_id in range(1, 130)],
        )
    overflow = read_local_monitor_facts(database, snapshot_generations=(), now=NOW)

    assert pruned.status == FACT_STATUS_COMPLETE
    assert pruned.candidates == ()
    assert overflow.status == FACT_STATUS_UNKNOWN
    assert overflow.unknown_code == "database_row_limit_exceeded"


def test_terminal_clear_remains_available_after_long_sentinel_downtime(tmp_path):
    database = tmp_path / "research.db"
    _create_fact_database(database)
    first_at = NOW - timedelta(hours=3)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO instruction_execution_contracts "
            "(id, state, last_progress_at, deadline_at, updated_at) "
            "VALUES (1, 'submit_unknown', ?, ?, ?)",
            (first_at.isoformat(), first_at.isoformat(), first_at.isoformat()),
        )
    bad = read_local_monitor_facts(database, snapshot_generations=(), now=first_at)
    _, state = evaluate_sentinel_observation(
        observation=SentinelObservation(
            checked_at=first_at, candidates=bad.candidates, adapter_failures=()
        ),
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )
    terminal_at = NOW - timedelta(hours=2)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE instruction_execution_contracts "
            "SET state='verified', updated_at=? WHERE id=1",
            (terminal_at.isoformat(),),
        )
    clear = read_local_monitor_facts(database, snapshot_generations=(), now=NOW)
    result, _ = evaluate_sentinel_observation(
        observation=SentinelObservation(
            checked_at=NOW, candidates=clear.candidates, adapter_failures=()
        ),
        previous_state=state,
        observation_generation=2,
    )

    assert clear.status == FACT_STATUS_COMPLETE
    assert clear.candidates == ()
    assert result.observed_health == "HEALTHY"


def test_pending_entry_preamble_without_row_deadline_is_unknown_settling_fact(tmp_path):
    database = tmp_path / "research.db"
    _create_fact_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO entry_preambles (id, status, created_at, updated_at) "
            "VALUES (1, 'pending', ?, ?)",
            ((NOW - timedelta(hours=7)).isoformat(), NOW.isoformat()),
        )

    result = read_local_monitor_facts(database, snapshot_generations=(), now=NOW)

    assert len(result.candidates) == 1
    assert result.candidates[0].reason_code == "stale_entry_preamble_unresolved"
    assert result.candidates[0].execution_deadline_at is None


def test_snapshot_evidence_rejects_newer_failure_and_stale_success():
    success = _snapshot(started_at=NOW - timedelta(minutes=1))
    failure = SnapshotGeneration(
        generation=8,
        outcome="FAILED",
        request_started_at=NOW - timedelta(seconds=10),
        request_completed_at=NOW - timedelta(seconds=9),
        uid_scope_hash="a" * 64,
        collections=(),
        failure_code="exchange_timeout",
    )
    failed_latest = SnapshotManifest(
        uid_scope_hash="a" * 64,
        generations=(success,),
        latest_attempt=failure,
        last_success=success,
    )
    stale = _snapshot(started_at=NOW - timedelta(minutes=10))
    stale_manifest = SnapshotManifest(
        uid_scope_hash="a" * 64,
        generations=(stale,),
        latest_attempt=stale,
        last_success=stale,
    )

    assert select_usable_snapshot_generations(failed_latest, now=NOW) == ()
    assert select_usable_snapshot_generations(stale_manifest, now=NOW) == ()


@pytest.mark.parametrize("duration", [timedelta(0), timedelta(seconds=46)])
def test_snapshot_evidence_rejects_zero_or_overlong_capture(duration):
    started = NOW - timedelta(seconds=46)
    generation = _snapshot(started_at=started)
    generation = SnapshotGeneration(
        generation=generation.generation,
        outcome=generation.outcome,
        request_started_at=started,
        request_completed_at=started + duration,
        uid_scope_hash=generation.uid_scope_hash,
        collections=generation.collections,
    )
    manifest = SnapshotManifest(
        uid_scope_hash="a" * 64,
        generations=(generation,),
        latest_attempt=generation,
        last_success=generation,
    )

    assert select_usable_snapshot_generations(manifest, now=NOW) == ()


def test_converge_partial_close_compares_exact_position_identity_and_decimal(tmp_path):
    database = tmp_path / "research.db"
    _create_fact_database(database)
    _insert_active_component(
        database,
        component_kind="converge_partial_close",
        desired={"pos_id": "exact-pos", "target_remaining_size": "2.500"},
        pos_id="exact-pos",
    )
    coherent = _snapshot(
        started_at=NOW - timedelta(seconds=2),
        positions=({"posId": "exact-pos", "pos": "2.5"},),
    )
    mismatch = _snapshot(
        started_at=NOW - timedelta(seconds=2),
        positions=({"posId": "exact-pos", "pos": "2.6"},),
    )

    same = read_local_monitor_facts(
        database, snapshot_generations=(coherent,), now=NOW
    )
    different = read_local_monitor_facts(
        database, snapshot_generations=(mismatch,), now=NOW
    )
    # Select by the only component candidate: batch stays evidence-incomplete.
    same_component = next(
        item
        for item in same.candidates
        if item.reason_code == "stalled_composite_component"
        and item.evidence_complete
    )
    different_component = next(
        item
        for item in different.candidates
        if item.reason_code == "stalled_composite_component"
        and item.evidence_complete
    )

    assert same_component.anomaly_present is False
    assert different_component.anomaly_present is True
    assert same_component.fingerprint == different_component.fingerprint


@pytest.mark.parametrize("desired_pos_id", [None, "other-pos"])
def test_converge_partial_close_missing_or_mismatched_desired_identity_is_unknown(
    tmp_path, desired_pos_id
):
    database = tmp_path / "research.db"
    _create_fact_database(database)
    desired = {"target_remaining_size": "2.5"}
    if desired_pos_id is not None:
        desired["pos_id"] = desired_pos_id
    _insert_active_component(
        database,
        component_kind="converge_partial_close",
        desired=desired,
        pos_id="exact-pos",
    )
    snapshot = _snapshot(
        positions=({"posId": "exact-pos", "pos": "2.6"},),
    )

    result = read_local_monitor_facts(
        database, snapshot_generations=(snapshot,), now=NOW
    )
    settling = [
        item
        for item in result.candidates
        if item.reason_code == "stalled_composite_component"
    ]

    assert len(settling) == 2
    assert all(item.anomaly_present is True for item in settling)
    assert all(item.evidence_complete is False for item in settling)


def test_recent_component_with_old_terminal_parent_is_coherent_not_schema_unknown(tmp_path):
    database = tmp_path / "research.db"
    _create_fact_database(database)
    old = NOW - timedelta(hours=2)
    _insert_active_component(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE strategy_management_batches SET status='succeeded', updated_at=? WHERE id=1",
            (old.isoformat(),),
        )
        connection.execute(
            "UPDATE strategy_management_components SET status='confirmed', updated_at=? WHERE id=1",
            (NOW.isoformat(),),
        )

    result = read_local_monitor_facts(database, snapshot_generations=(), now=NOW)

    assert result.status == FACT_STATUS_COMPLETE


def test_structured_adapters_are_closed_and_generic_error_is_not_an_incident(tmp_path):
    settings = build_settings_candidates(
        {
            "auto_trade_enabled": True,
            "max_concurrent_positions": 4,
            "management_execution_mode": "live",
            "entry_preamble_mode": "live",
        },
        observed_at=NOW,
        expected_auto_trade_enabled=True,
        expected_max_concurrent_positions=4,
        expected_management_execution_mode="live",
        expected_entry_preamble_mode="live",
    )
    journal = tmp_path / "journal.log"
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service_generation": "b" * 64,
                "capture_started_at": (NOW - timedelta(seconds=2)).isoformat(),
                "capture_completed_at": (NOW - timedelta(seconds=1)).isoformat(),
                "complete": True,
                "markers": ["reviewed_sha_drift"],
                "resolved_markers": [],
            }
        )
    )
    journal_candidates = read_journal_candidates(
        journal,
        observed_at=NOW,
        expected_service_generation="b" * 64,
    )

    assert all(item.anomaly_present is False for item in settings)
    assert [
        item.reason_code for item in journal_candidates if item.anomaly_present
    ] == ["reviewed_sha_drift"]
    assert journal_candidates[0].durable_terminal_fact is False

    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service_generation": "b" * 64,
                "capture_started_at": (NOW - timedelta(seconds=2)).isoformat(),
                "capture_completed_at": (NOW - timedelta(seconds=1)).isoformat(),
                "complete": True,
                "markers": [],
                "resolved_markers": ["reviewed_sha_drift"],
            }
        )
    )
    resolved = read_journal_candidates(
        journal,
        observed_at=NOW,
        expected_service_generation="b" * 64,
    )
    assert resolved[0].durable_terminal_fact is True
    assert resolved[0].fingerprint == journal_candidates[0].fingerprint


def test_journal_rejects_stale_or_wrong_generation_evidence(tmp_path):
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service_generation": "b" * 64,
                "capture_started_at": (NOW - timedelta(minutes=10)).isoformat(),
                "capture_completed_at": (NOW - timedelta(minutes=9)).isoformat(),
                "complete": True,
                "markers": [],
                "resolved_markers": [],
            }
        )
    )

    with pytest.raises(ValueError):
        read_journal_candidates(
            journal,
            observed_at=NOW,
            expected_service_generation="b" * 64,
        )
    with pytest.raises(ValueError):
        read_journal_candidates(
            journal,
            observed_at=NOW - timedelta(minutes=9),
            expected_service_generation="c" * 64,
        )


def test_journal_rejects_capture_window_that_is_too_long(tmp_path):
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service_generation": "b" * 64,
                "capture_started_at": (NOW - timedelta(days=1)).isoformat(),
                "capture_completed_at": NOW.isoformat(),
                "complete": True,
                "markers": ["reviewed_sha_drift"],
                "resolved_markers": [],
            }
        )
    )

    with pytest.raises(ValueError):
        read_journal_candidates(
            journal,
            observed_at=NOW,
            expected_service_generation="b" * 64,
        )


def test_coverage_disabled_is_conservative_not_ready():
    counts = {
        field: 0
        for field in (
            "executable_messages_total contracts_created_total contracts_verified_total "
            "contracts_violated_total executable_without_contract_total "
            "violations_without_stage1_total stage1_pending stage1_delivered stage1_failed "
            "agent_pending agent_diagnosed agent_failed agent_timed_out "
            "incidents_without_terminal_stage2_total handoffs_persisted_total stage2_pending "
            "stage2_delivered stage2_failed oldest_nonterminal_age_seconds "
            "instruction_execution_contradictions_total"
        ).split()
    }
    payload = {
        **counts,
        "schema_version": 1,
        "coverage_enabled": False,
        "scan_truncated": False,
        "supervisor_policy_status": "disabled",
        "supervisor_last_success_at": None,
        "instruction_execution_scan_truncated": False,
        "instruction_execution_facts": [],
    }

    candidates = build_coverage_candidates(payload, now=NOW)

    assert "message_operation_coverage_incomplete" in {
        item.reason_code for item in candidates if item.anomaly_present
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "instruction_execution_contradictions_total": 1,
            "instruction_execution_facts": [None],
        },
        {
            "contracts_violated_total": 0,
            "violations_without_stage1_total": 1,
        },
        {"handoffs_persisted_total": 0, "stage2_delivered": 1},
    ],
)
def test_coverage_rejects_malformed_facts_and_broken_count_invariants(overrides):
    count_names = (
        "executable_messages_total contracts_created_total contracts_verified_total "
        "contracts_violated_total executable_without_contract_total "
        "violations_without_stage1_total stage1_pending stage1_delivered stage1_failed "
        "agent_pending agent_diagnosed agent_failed agent_timed_out "
        "incidents_without_terminal_stage2_total handoffs_persisted_total stage2_pending "
        "stage2_delivered stage2_failed oldest_nonterminal_age_seconds "
        "instruction_execution_contradictions_total"
    ).split()
    payload = {
        **{name: 0 for name in count_names},
        "schema_version": 1,
        "coverage_enabled": True,
        "scan_truncated": False,
        "supervisor_policy_status": "valid",
        "supervisor_last_success_at": NOW.isoformat(),
        "instruction_execution_scan_truncated": False,
        "instruction_execution_facts": [],
        **overrides,
    }

    with pytest.raises(ValueError):
        build_coverage_candidates(payload, now=NOW)


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_coverage_and_journal_reject_non_integer_schema_version(
    tmp_path, schema_version
):
    count_names = (
        "executable_messages_total contracts_created_total contracts_verified_total "
        "contracts_violated_total executable_without_contract_total "
        "violations_without_stage1_total stage1_pending stage1_delivered stage1_failed "
        "agent_pending agent_diagnosed agent_failed agent_timed_out "
        "incidents_without_terminal_stage2_total handoffs_persisted_total stage2_pending "
        "stage2_delivered stage2_failed oldest_nonterminal_age_seconds "
        "instruction_execution_contradictions_total"
    ).split()
    coverage = {
        **{name: 0 for name in count_names},
        "schema_version": schema_version,
        "coverage_enabled": True,
        "scan_truncated": False,
        "supervisor_policy_status": "valid",
        "supervisor_last_success_at": NOW.isoformat(),
        "instruction_execution_scan_truncated": False,
        "instruction_execution_facts": [],
    }
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "service_generation": "b" * 64,
                "capture_started_at": (NOW - timedelta(seconds=2)).isoformat(),
                "capture_completed_at": (NOW - timedelta(seconds=1)).isoformat(),
                "complete": True,
                "markers": [],
                "resolved_markers": [],
            }
        )
    )

    with pytest.raises(ValueError):
        build_coverage_candidates(coverage, now=NOW)
    with pytest.raises(ValueError):
        read_journal_candidates(
            journal,
            observed_at=NOW,
            expected_service_generation="b" * 64,
        )


def test_fact_reader_has_no_legacy_cache_or_database_write_surface():
    source_path = (
        Path(__file__).parents[1]
        / "src"
        / "telegram_kol_research"
        / "production_monitor_facts.py"
    )
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert not any("production_safety_monitor" in name for name in imported_modules)
    assert not any("live_position_snapshot" in name for name in imported_modules)
    assert "INSERT " not in source
    assert "UPDATE " not in source
    assert "DELETE " not in source

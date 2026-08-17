from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import sqlite3

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deployment_preflight import (
    build_deployment_preflight_artifact,
    collect_deployment_preflight_facts,
)
from telegram_kol_research.deployment_work_evidence import (
    DeploymentWorkEvidenceError,
    classify_deployment_work,
    collect_work_evidence,
)


NOW = datetime(2026, 8, 17, 1, 20, tzinfo=UTC)


@pytest.mark.parametrize("change_class", ["code", "schema_compatible"])
def test_restart_safe_wait_warns_for_non_writer_changes(change_class):
    result = classify_deployment_work(
        counts={"restart_safe_wait": {"management_batches": 1}},
        change_class=change_class,
    )

    assert result.blocking_reason_codes == ()
    assert result.warning_reason_codes == ("deployment_restart_safe_wait",)


@pytest.mark.parametrize("change_class", ["execution_writer", "live_promotion"])
def test_restart_safe_wait_blocks_writer_changes(change_class):
    result = classify_deployment_work(
        counts={"restart_safe_wait": {"management_batches": 1}},
        change_class=change_class,
    )

    assert result.blocking_reason_codes == ("deployment_restart_safe_wait",)
    assert result.warning_reason_codes == ()


@pytest.mark.parametrize(
    ("classification", "reason"),
    [
        ("in_flight_write", "deployment_in_flight_write"),
        ("unknown_outcome", "deployment_unknown_outcome"),
        ("malformed", "deployment_evidence_malformed"),
    ],
)
def test_hard_safety_facts_always_block(classification, reason):
    result = classify_deployment_work(
        counts={classification: {"execution_order_legs": 1}},
        change_class="code",
    )

    assert result.blocking_reason_codes == (reason,)
    assert result.warning_reason_codes == ()


def test_terminal_only_work_adds_no_reason_code():
    result = classify_deployment_work(
        counts={"terminal": {"execution_order_legs": 3}},
        change_class="live_promotion",
    )

    assert result.blocking_reason_codes == ()
    assert result.warning_reason_codes == ()


@pytest.mark.parametrize(
    "counts",
    [
        {"unexpected": {"execution_order_legs": 1}},
        {"in_flight_write": {"execution_order_legs": -1}},
        {"in_flight_write": {"execution_order_legs": True}},
        {"in_flight_write": {"execution_order_legs": "1"}},
    ],
)
def test_malformed_work_counts_are_rejected(counts):
    with pytest.raises(DeploymentWorkEvidenceError, match="deployment_evidence_malformed"):
        classify_deployment_work(counts=counts, change_class="code")


def test_management_heartbeat_is_historical_evidence_not_fresh_write(tmp_path):
    database = tmp_path / "research.db"
    create_session_factory(database)
    old = (NOW - timedelta(days=4)).replace(tzinfo=None).isoformat(" ")
    heartbeat_one = (
        (NOW - timedelta(seconds=10)).replace(tzinfo=None).isoformat(" ")
    )
    heartbeat_two = (
        (NOW - timedelta(seconds=1)).replace(tzinfo=None).isoformat(" ")
    )
    connection = sqlite3.connect(database)
    connection.execute(
        """INSERT INTO strategy_management_batches (
            id, idempotency_fingerprint, raw_message_id,
            recognition_decision_id, recognition_generation,
            target_lifecycle_id, strategy_instance_id, execution_binding_id,
            intent, effective_action, execution_mode, partial_round_before,
            status, reason_code, target_fingerprint, target_snapshot_json,
            planned_at, created_at, updated_at
        ) VALUES (
            119, ?, 1, 1, 'generation-1', 1, 'strategy-1', 1,
            'partial_take_profit', 'partial_then_break_even', 'legacy', 0,
            'reconciling', 'management_close_pending_exchange_confirmation',
            ?, '{}', ?, ?, ?
        )""",
        ("a" * 64, "b" * 64, old, old, heartbeat_one),
    )
    connection.execute(
        """INSERT INTO strategy_management_legs (
            id, management_batch_id, execution_order_leg_id, pos_id,
            leg_index, status, client_order_id, exchange_order_id,
            request_json, response_json, created_at, updated_at
        ) VALUES (103, 119, 1, 'redacted-position', 0, 'submitted',
                  NULL, NULL, NULL, NULL, ?, ?)""",
        (old, heartbeat_one),
    )
    connection.commit()
    connection.close()

    first = collect_deployment_preflight_facts(
        database_path=database,
        change_class="code",
        now=NOW,
    )
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE strategy_management_batches SET updated_at = ? WHERE id = 119",
        (heartbeat_two,),
    )
    connection.execute(
        "UPDATE strategy_management_legs SET updated_at = ? WHERE id = 103",
        (heartbeat_two,),
    )
    connection.commit()
    connection.close()
    second = collect_deployment_preflight_facts(
        database_path=database,
        change_class="code",
        now=NOW,
    )

    expected = {"management_batches": 1, "management_legs": 1}
    assert first.work_classification_counts["historical_residue"] == expected
    assert second.work_classification_counts["historical_residue"] == expected
    assert first.work_evidence_fingerprint == second.work_evidence_fingerprint
    serialized = json.dumps(first.to_json(), sort_keys=True)
    assert "management_close_pending_exchange_confirmation" not in serialized
    assert "redacted-position" not in serialized

    schema_facts = replace(
        first,
        schema_backup_valid=True,
        schema_migration_dry_run_valid=True,
    )
    schema_artifact = build_deployment_preflight_artifact(
        expected_commit="a" * 40,
        change_class="schema_compatible",
        facts=schema_facts,
        now=NOW,
    )
    writer_artifact = build_deployment_preflight_artifact(
        expected_commit="a" * 40,
        change_class="execution_writer",
        facts=first,
        now=NOW,
    )
    assert schema_artifact["decision"] == "WARN"
    assert "deployment_historical_residue" in schema_artifact["reason_codes"]
    assert writer_artifact["decision"] == "BLOCK"
    assert "deployment_historical_residue" in writer_artifact["reason_codes"]


def test_unrecognized_durable_state_is_malformed():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE execution_order_legs (id INTEGER PRIMARY KEY, status TEXT)"
    )
    connection.execute(
        "INSERT INTO execution_order_legs (id, status) VALUES (1, 'new_unreviewed_state')"
    )

    result = collect_work_evidence(
        connection,
        available_tables={"execution_order_legs"},
        now=NOW,
    )

    assert result.counts["malformed"] == {"execution_order_legs": 1}
    decision = classify_deployment_work(counts=result.counts, change_class="code")
    assert decision.blocking_reason_codes == ("deployment_evidence_malformed",)

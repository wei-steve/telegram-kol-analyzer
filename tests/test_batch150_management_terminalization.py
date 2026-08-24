from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from telegram_kol_research.batch150_management_terminalization import (
    Batch150TerminalizationRefused,
    apply_batch150_terminalization_plan,
    build_batch150_terminalization_plan,
    load_batch150_terminalization_plan,
    main,
    render_batch150_rollback_sql,
    rollback_batch150_terminalization_plan,
    write_batch150_terminalization_plan,
)


REPAIR_TS = datetime(2026, 8, 24, 22, 30, tzinfo=UTC)
OLD_TIME = "2026-08-24 21:51:46.161081"
STRATEGY_ID = "deepcoin:-1003048800035:4384:BTC:short"
TARGET_POS_ID = "1001124956792734"
SIBLING_POS_ID = "1001124961572300"
PARENT_TRIGGER_ID = "1001124956792983"
TARGET_STOP_ID = "1001124956792870"
SIBLING_STOP_ID = "1001124961572299"
CLOSE_MILLIS = "1787574676000"
CLOSE_SECONDS = "1787574676"
PARENT_TRIGGER_MILLIS = "1787567625000"
PARENT_TRIGGER_SECONDS = "1787567625"


def _seed_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE strategy_management_batches (
          id INTEGER PRIMARY KEY, status TEXT, reason_code TEXT,
          reconciled_at TEXT, completed_at TEXT, updated_at TEXT,
          idempotency_fingerprint TEXT, management_contract_fingerprint TEXT,
          target_fingerprint TEXT, recognition_generation TEXT,
          contract_version INTEGER, target_lifecycle_id INTEGER,
          execution_binding_id INTEGER, raw_message_id INTEGER,
          recognition_decision_id INTEGER, strategy_instance_id TEXT
        );
        CREATE TABLE strategy_management_legs (
          id INTEGER PRIMARY KEY, management_batch_id INTEGER,
          execution_order_leg_id INTEGER, pos_id TEXT, leg_index INTEGER,
          status TEXT, preflight_size TEXT, planned_close_size TEXT,
          last_error TEXT, updated_at TEXT
        );
        CREATE TABLE strategy_management_components (
          id INTEGER PRIMARY KEY, management_batch_id INTEGER,
          strategy_management_leg_id INTEGER, component_kind TEXT,
          sequence INTEGER, status TEXT, reason_code TEXT, attempt_count INTEGER,
          idempotency_key TEXT, desired_json TEXT, evidence_json TEXT,
          last_progress_at TEXT, completed_at TEXT, updated_at TEXT
        );
        CREATE TABLE execution_bindings (
          id INTEGER PRIMARY KEY, strategy_instance_id TEXT, symbol TEXT,
          side TEXT, status TEXT, pos_id TEXT, last_exchange_status TEXT,
          recovered_at TEXT, updated_at TEXT
        );
        CREATE TABLE execution_order_legs (
          id INTEGER PRIMARY KEY, execution_binding_id INTEGER,
          strategy_instance_id TEXT, leg_index INTEGER, purpose TEXT,
          order_kind TEXT, order_id TEXT, client_order_id TEXT, pos_id TEXT,
          attribution_status TEXT, terminal_reason TEXT,
          last_verified_at TEXT, status TEXT, updated_at TEXT
        );
        CREATE TABLE strategy_lifecycles (
          id INTEGER PRIMARY KEY, execution_binding_id INTEGER,
          lifecycle_status TEXT, exit_reason TEXT, entered_at TEXT,
          exited_at TEXT, filled_tp_index INTEGER, management_action TEXT,
          updated_at TEXT
        );
        CREATE TABLE position_mutation_intents (
          id INTEGER PRIMARY KEY, execution_binding_id INTEGER,
          execution_order_leg_id INTEGER, operation TEXT, status TEXT,
          pos_id TEXT, order_id TEXT, updated_at TEXT
        );
        CREATE TABLE bound_position_close_reservations (
          id INTEGER PRIMARY KEY, pos_id TEXT, execution_binding_id INTEGER,
          status TEXT, last_error TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE position_protection_ledger (id INTEGER PRIMARY KEY);
        CREATE TABLE position_take_profit_orders (id INTEGER PRIMARY KEY);
        CREATE TABLE execution_events (id INTEGER PRIMARY KEY);
        CREATE TABLE raw_messages (id INTEGER PRIMARY KEY, message_id INTEGER);
        CREATE TABLE recognition_decisions (id INTEGER PRIMARY KEY);
        """
    )
    connection.execute(
        "INSERT INTO strategy_management_batches VALUES "
        "(149,'resolved','historical_position_fully_closed',NULL,NULL,?,"
        "'old-idempotency','old-contract','old-target','old-generation',2,"
        "951,319,12700,12699,'old-strategy')",
        (OLD_TIME,),
    )
    connection.execute(
        "INSERT INTO strategy_management_batches VALUES "
        "(150,'recovery_required','take_profit_cancel_retry_exhausted',"
        "NULL,NULL,?,'723b6ce5b01d2efef22dd35a11ace2f91cb35a12a9fd1cb4ec00e2953c01ac36',"
        "'ad8515aec6aac95b51b5edd0de6fe8aab9f6d89fa54ba23aeedb07af71a06006',"
        "'353682c2d65ce005f9b214ac4cd4433d0b0da141d49fe3456866af861d8c2e73',"
        "'eb1b4c395455496fbdedd6c267d66443',2,952,320,12780,12779,?)",
        (OLD_TIME, STRATEGY_ID),
    )
    connection.execute(
        "INSERT INTO strategy_management_legs VALUES "
        "(133,150,553,?,0,'planned','6','3',NULL,?)",
        (TARGET_POS_ID, OLD_TIME),
    )
    component_rows = (
        (22, "consume_take_profit_stage", 0, "operator_required", "take_profit_cancel_retry_exhausted", 3, OLD_TIME),
        (23, "converge_partial_close", 1, "pending", None, 0, None),
        (24, "replace_remaining_protection", 2, "pending", None, 0, None),
    )
    for component_id, kind, sequence, status, reason, attempts, completed in component_rows:
        connection.execute(
            "INSERT INTO strategy_management_components VALUES "
            "(?,150,133,?,?,?,?,?,?,'{}','[]',?,?,?)",
            (
                component_id, kind, sequence, status, reason, attempts,
                f"component-{component_id}", OLD_TIME, completed, OLD_TIME,
            ),
        )
    connection.execute(
        "INSERT INTO execution_bindings VALUES "
        "(320,?,'BTC','short','active',?,"
        "'position_attribution_evidence_unavailable',?,?)",
        (STRATEGY_ID, TARGET_POS_ID, OLD_TIME, OLD_TIME),
    )
    connection.execute(
        "INSERT INTO execution_order_legs VALUES "
        "(553,320,?,1,'entry','market',?,'TKDBK4384E1',?,"
        "'verified',NULL,?,'filled',?)",
        (STRATEGY_ID, TARGET_POS_ID, TARGET_POS_ID, OLD_TIME, OLD_TIME),
    )
    connection.execute(
        "INSERT INTO execution_order_legs VALUES "
        "(554,320,?,2,'entry','trigger_limit',?,'TKDBK4384E2',?,"
        "'verified',NULL,?,'active',?)",
        (STRATEGY_ID, PARENT_TRIGGER_ID, SIBLING_POS_ID, OLD_TIME, OLD_TIME),
    )
    connection.execute(
        "INSERT INTO strategy_lifecycles VALUES "
        "(952,320,'entered',NULL,'2026-08-24 02:56:58.821013',NULL,-1,"
        "'expiry_review_continued',?)",
        (OLD_TIME,),
    )
    for intent_id, order_id in enumerate(
        (TARGET_STOP_ID, "1001124956794112", "1001124956794638", "1001124956794989", "1001124956795290"),
        start=559,
    ):
        connection.execute(
            "INSERT INTO position_mutation_intents VALUES "
            "(?,320,553,'set_position_sltp','confirmed',?,?,?)",
            (intent_id, TARGET_POS_ID, order_id, OLD_TIME),
        )
    connection.execute("INSERT INTO position_protection_ledger VALUES (587)")
    connection.execute("INSERT INTO position_take_profit_orders VALUES (170)")
    connection.execute("INSERT INTO execution_events VALUES (3712)")
    connection.execute("INSERT INTO raw_messages VALUES (12780,4385)")
    connection.execute("INSERT INTO recognition_decisions VALUES (12779)")
    connection.commit()
    connection.close()


def _complete_exchange_evidence():
    return {
        "snapshot_complete": True,
        "snapshot_errors": {},
        "exchange_write_count": 0,
        "positions": [],
        "open_orders": [],
        "pending_trigger_orders": [],
        "target_position_history": [{
            "instId": "BTC-USDT-SWAP",
            "posId": TARGET_POS_ID,
            "posSide": "short",
            "pos": "11",
            "closePos": "11",
            "avgPx": "77471.8",
            "closeAvgPx": "77736.5",
            "uTime": CLOSE_MILLIS,
        }],
        "sibling_position_history": [{
            "instId": "BTC-USDT-SWAP",
            "posId": SIBLING_POS_ID,
            "posSide": "short",
            "pos": "11",
            "closePos": "11",
            "avgPx": "78013.7",
            "closeAvgPx": "78603.9",
            "uTime": CLOSE_MILLIS,
        }],
        "target_stop": {
            "ordId": TARGET_STOP_ID,
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "slTriggerPrice": "78600",
            "triggerTime": CLOSE_SECONDS,
            "uTime": CLOSE_MILLIS,
        },
        "sibling_stop": {
            "ordId": SIBLING_STOP_ID,
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "slTriggerPrice": "78600",
            "triggerTime": CLOSE_SECONDS,
            "uTime": CLOSE_MILLIS,
        },
        "parent_child_chain": {
            "parent_trigger_order_id": PARENT_TRIGGER_ID,
            "parent_trigger_time": PARENT_TRIGGER_SECONDS,
            "parent_instrument_id": "BTC-USDT-SWAP",
            "parent_side": "short",
            "parent_size": "11",
            "unique_child_regular_order_id": SIBLING_POS_ID,
            "child_pos_id": SIBLING_POS_ID,
            "child_created_at": PARENT_TRIGGER_MILLIS,
            "child_instrument_id": "BTC-USDT-SWAP",
            "child_side": "short",
            "child_state": "filled",
            "child_size": "11",
            "child_fill_size": "11",
        },
    }


def _build_plan(tmp_path, *, evidence=None):
    database_path = tmp_path / "copy.db"
    _seed_database(database_path)
    return database_path, build_batch150_terminalization_plan(
        database_path,
        exchange_evidence=evidence or _complete_exchange_evidence(),
        repair_ts=REPAIR_TS,
        code_sha="f" * 40,
    )


def test_build_plan_has_exact_eight_action_matrix(tmp_path):
    _database_path, plan = _build_plan(tmp_path)

    assert plan.action_count == 8
    assert plan.exchange_write_count == 0
    assert len(plan.database_fingerprint) == 64
    assert len(plan.exchange_fingerprint) == 64
    assert len(plan.action_fingerprint) == 64
    assert len(plan.rollback_fingerprint) == 64
    assert len(plan.plan_fingerprint) == 64
    assert [(action.table, action.pk) for action in plan.actions] == [
        ("strategy_management_components", 23),
        ("strategy_management_components", 24),
        ("strategy_management_legs", 133),
        ("strategy_management_batches", 150),
        ("execution_order_legs", 553),
        ("execution_order_legs", 554),
        ("execution_bindings", 320),
        ("strategy_lifecycles", 952),
    ]
    assert all(action.pk != 22 for action in plan.actions)
    sibling = next(action for action in plan.actions if action.pk == 554)
    assert sibling.after["status"] == "closed"
    assert sibling.after["pos_id"] == SIBLING_POS_ID
    binding = next(action for action in plan.actions if action.table == "execution_bindings")
    assert binding.after["status"] == "closed"
    assert binding.after["pos_id"] is None


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda value: value.update(snapshot_complete=False), "exchange_snapshot_incomplete"),
        (lambda value: value["positions"].append({"posId": TARGET_POS_ID}), "target_position_not_terminal"),
        (lambda value: value["open_orders"].append({"posId": SIBLING_POS_ID}), "target_position_not_terminal"),
        (lambda value: value["pending_trigger_orders"].append({"ordId": SIBLING_STOP_ID}), "target_position_not_terminal"),
        (lambda value: value.update(target_position_history=[]), "target_history_not_unique"),
        (lambda value: value["target_position_history"].append(dict(value["target_position_history"][0])), "target_history_not_unique"),
        (lambda value: value["sibling_position_history"][0].update(closePos="10"), "sibling_full_close_unproven"),
        (lambda value: value["target_stop"].update(triggerTime="0"), "target_stop_close_unproven"),
        (lambda value: value["sibling_stop"].update(ordId="wrong"), "sibling_stop_close_unproven"),
        (lambda value: value["parent_child_chain"].update(unique_child_regular_order_id="wrong"), "parent_child_chain_unproven"),
        (lambda value: value["parent_child_chain"].update(child_state="cancelled"), "parent_child_chain_unproven"),
    ],
)
def test_plan_refuses_incomplete_or_mismatched_exchange_evidence(
    tmp_path, mutate, reason
):
    database_path = tmp_path / "copy.db"
    _seed_database(database_path)
    evidence = copy.deepcopy(_complete_exchange_evidence())
    mutate(evidence)

    with pytest.raises(Batch150TerminalizationRefused, match=reason):
        build_batch150_terminalization_plan(
            database_path,
            exchange_evidence=evidence,
            repair_ts=REPAIR_TS,
            code_sha="f" * 40,
        )


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        (
            "INSERT INTO execution_order_legs VALUES "
            "(555,320,'deepcoin:-1003048800035:4384:BTC:short',3,'entry',"
            "'market','extra','extra','extra','verified',NULL,NULL,'active',"
            "'2026-08-24 21:51:46.161081')",
            "binding_leg_set_changed",
        ),
        (
            "UPDATE position_mutation_intents SET status='submitted' WHERE id=559",
            "mutation_intent_unconfirmed",
        ),
        (
            "INSERT INTO bound_position_close_reservations VALUES "
            "(1,'1001124956792734',320,'submitted',NULL,"
            "'2026-08-24 21:51:46.161081','2026-08-24 21:51:46.161081')",
            "close_reservation_present",
        ),
        (
            "UPDATE strategy_management_components SET status='running' WHERE id=23",
            "component_changed",
        ),
        (
            "UPDATE strategy_management_batches SET status='recovery_required' WHERE id=149",
            "target_set_changed",
        ),
    ],
)
def test_plan_refuses_database_identity_or_safety_drift(tmp_path, sql, reason):
    database_path = tmp_path / "copy.db"
    _seed_database(database_path)
    connection = sqlite3.connect(database_path)
    connection.execute(sql)
    connection.commit()
    connection.close()

    with pytest.raises(Batch150TerminalizationRefused, match=reason):
        build_batch150_terminalization_plan(
            database_path,
            exchange_evidence=_complete_exchange_evidence(),
            repair_ts=REPAIR_TS,
            code_sha="f" * 40,
        )


def test_plan_round_trip_and_rollback_sql_are_exact(tmp_path):
    _database_path, plan = _build_plan(tmp_path)
    plan_path = tmp_path / "plan.json"

    write_batch150_terminalization_plan(plan_path, plan)
    loaded = load_batch150_terminalization_plan(plan_path)
    rollback_sql = render_batch150_rollback_sql(loaded)

    assert loaded == plan
    assert plan_path.stat().st_mode & 0o777 == 0o600
    assert rollback_sql.startswith("BEGIN IMMEDIATE;\n")
    assert rollback_sql.rstrip().endswith("COMMIT;")
    assert rollback_sql.count("UPDATE ") == 8
    assert plan.rollback_fingerprint in rollback_sql


def _database_digest(path):
    connection = sqlite3.connect(path)
    try:
        rows = []
        for table in (
            "strategy_management_components",
            "strategy_management_legs",
            "strategy_management_batches",
            "execution_order_legs",
            "execution_bindings",
            "strategy_lifecycles",
            "position_mutation_intents",
            "bound_position_close_reservations",
            "position_protection_ledger",
            "position_take_profit_orders",
            "execution_events",
            "raw_messages",
            "recognition_decisions",
        ):
            columns = [
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            ]
            values = connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            rows.append((table, columns, values))
        return hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    finally:
        connection.close()


def _apply(database_path, plan, **overrides):
    values = {
        "expected_plan_fingerprint": plan.plan_fingerprint,
        "expected_action_count": 8,
        "expected_repair_ts_utc": plan.repair_ts_utc,
        "confirmation_token": plan.confirmation_token,
    }
    values.update(overrides)
    return apply_batch150_terminalization_plan(
        database_path, plan=plan, **values
    )


def _rollback(database_path, plan, **overrides):
    values = {
        "expected_rollback_fingerprint": plan.rollback_fingerprint,
        "expected_action_count": 8,
        "confirmation_token": plan.confirmation_token,
    }
    values.update(overrides)
    return rollback_batch150_terminalization_plan(
        database_path, plan=plan, **values
    )


def test_apply_is_cas_idempotent_and_rollback_restores_exact_rows(tmp_path):
    database_path, plan = _build_plan(tmp_path)
    original_digest = _database_digest(database_path)

    applied = _apply(database_path, plan)
    assert applied.status == "applied"
    assert applied.changed_row_count == 8
    assert applied.quick_check == "ok"
    assert applied.table_counts_before == applied.table_counts_after

    second = _apply(database_path, plan)
    assert second.status == "already_applied"
    assert second.changed_row_count == 0

    rolled_back = _rollback(database_path, plan)
    assert rolled_back.status == "rolled_back"
    assert rolled_back.changed_row_count == 8
    assert rolled_back.quick_check == "ok"
    assert rolled_back.table_counts_before == rolled_back.table_counts_after
    assert _database_digest(database_path) == original_digest


def test_apply_tolerates_only_leg_553_updated_at_drift(tmp_path):
    database_path, plan = _build_plan(tmp_path)
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE execution_order_legs SET updated_at='runtime-refresh-before-apply' "
        "WHERE id=553"
    )
    connection.commit()
    connection.close()

    result = _apply(database_path, plan)

    assert result.status == "applied"
    assert result.changed_row_count == 8
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT * FROM execution_order_legs WHERE id=553"
    ).fetchone()
    columns = [
        value[1]
        for value in connection.execute("PRAGMA table_info(execution_order_legs)")
    ]
    connection.close()
    action = next(value for value in plan.actions if value.pk == 553)
    assert dict(zip(columns, row, strict=True)) == dict(action.after)


def test_reapply_and_rollback_tolerate_leg_553_updated_at_drift(tmp_path):
    database_path, plan = _build_plan(tmp_path)
    original_digest = _database_digest(database_path)
    _apply(database_path, plan)

    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE execution_order_legs SET updated_at='runtime-refresh-after-apply' "
        "WHERE id=553"
    )
    connection.commit()
    connection.close()
    second = _apply(database_path, plan)
    assert second.status == "already_applied"
    assert second.changed_row_count == 0

    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE execution_order_legs SET updated_at='runtime-refresh-before-rollback' "
        "WHERE id=553"
    )
    connection.commit()
    connection.close()
    rolled_back = _rollback(database_path, plan)
    assert rolled_back.status == "rolled_back"
    assert rolled_back.changed_row_count == 8
    assert _database_digest(database_path) == original_digest


@pytest.mark.parametrize(
    ("table", "pk", "column", "value"),
    [
        ("execution_order_legs", 553, "status", "cancelled"),
        ("execution_order_legs", 553, "last_verified_at", "drift"),
        ("execution_order_legs", 554, "updated_at", "drift"),
        ("execution_bindings", 320, "updated_at", "drift"),
    ],
)
def test_apply_refuses_every_nonapproved_drift(
    tmp_path, table, pk, column, value
):
    database_path, plan = _build_plan(tmp_path)
    connection = sqlite3.connect(database_path)
    connection.execute(
        f"UPDATE {table} SET {column}=? WHERE id=?", (value, pk)
    )
    connection.commit()
    connection.close()
    drift_digest = _database_digest(database_path)

    with pytest.raises(Batch150TerminalizationRefused, match="database_state_mixed"):
        _apply(database_path, plan)
    assert _database_digest(database_path) == drift_digest


def test_plan_fingerprints_the_single_volatile_cas_coordinate(tmp_path):
    _database_path, plan = _build_plan(tmp_path)

    assert plan.schema_version == 2
    assert plan.cas_policy == {
        "execution_order_legs:553": {
            "ignored_before_fields": ("updated_at",),
        }
    }

    tampered = replace(plan, cas_policy={})
    with pytest.raises(Batch150TerminalizationRefused, match="plan_integrity_invalid"):
        _apply(_database_path, tampered)


def test_rollback_fingerprint_and_sql_bind_the_cas_policy(tmp_path):
    _database_path, plan = _build_plan(tmp_path)
    legacy_actions_only = [
        {
            "table": action.table,
            "pk": action.pk,
            "before": dict(action.after),
            "after": dict(action.before),
        }
        for action in reversed(plan.actions)
    ]
    legacy_fingerprint = hashlib.sha256(
        json.dumps(
            legacy_actions_only,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    assert plan.rollback_fingerprint != legacy_fingerprint
    rollback_sql = render_batch150_rollback_sql(plan)
    assert plan.plan_fingerprint in rollback_sql
    assert "execution_order_legs:553 updated_at" in rollback_sql


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"expected_plan_fingerprint": "0" * 64}, "plan_fingerprint_mismatch"),
        ({"expected_action_count": 7}, "action_count_mismatch"),
        ({"expected_repair_ts_utc": "2026-08-24T22:30:00Z"}, "repair_timestamp_mismatch"),
        ({"confirmation_token": "wrong"}, "confirmation_token_mismatch"),
    ],
)
def test_apply_refuses_wrong_exact_authorization_before_write(
    tmp_path, overrides, reason
):
    database_path, plan = _build_plan(tmp_path)
    original_digest = _database_digest(database_path)

    with pytest.raises(Batch150TerminalizationRefused, match=reason):
        _apply(database_path, plan, **overrides)
    assert _database_digest(database_path) == original_digest


def test_apply_refuses_database_path_or_table_count_drift(tmp_path):
    database_path, plan = _build_plan(tmp_path)
    other_path = tmp_path / "other.db"
    _seed_database(other_path)
    other_digest = _database_digest(other_path)

    with pytest.raises(
        Batch150TerminalizationRefused, match="database_path_mismatch"
    ):
        _apply(other_path, plan)
    assert _database_digest(other_path) == other_digest

    connection = sqlite3.connect(database_path)
    connection.execute("INSERT INTO execution_events VALUES (3713)")
    connection.commit()
    connection.close()
    drift_digest = _database_digest(database_path)
    with pytest.raises(Batch150TerminalizationRefused, match="table_counts_changed"):
        _apply(database_path, plan)
    assert _database_digest(database_path) == drift_digest


def test_apply_and_rollback_refuse_mixed_row_state_without_partial_write(tmp_path):
    database_path, plan = _build_plan(tmp_path)
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE strategy_management_components SET attempt_count=99 WHERE id=23"
    )
    connection.commit()
    connection.close()
    drift_digest = _database_digest(database_path)

    with pytest.raises(Batch150TerminalizationRefused, match="database_state_mixed"):
        _apply(database_path, plan)
    assert _database_digest(database_path) == drift_digest

    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE strategy_management_components SET attempt_count=0 WHERE id=23"
    )
    connection.commit()
    connection.close()
    _apply(database_path, plan)
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE execution_bindings SET last_exchange_status='entry_legs_terminal' "
        "WHERE id=320"
    )
    connection.commit()
    connection.close()
    canonicalized_digest = _database_digest(database_path)

    with pytest.raises(Batch150TerminalizationRefused, match="database_state_mixed"):
        _rollback(database_path, plan)
    assert _database_digest(database_path) == canonicalized_digest


def test_rollback_refuses_wrong_exact_authorization(tmp_path):
    database_path, plan = _build_plan(tmp_path)
    _apply(database_path, plan)
    applied_digest = _database_digest(database_path)

    with pytest.raises(
        Batch150TerminalizationRefused, match="rollback_fingerprint_mismatch"
    ):
        _rollback(
            database_path,
            plan,
            expected_rollback_fingerprint="0" * 64,
        )
    assert _database_digest(database_path) == applied_digest


def test_rendered_rollback_sql_restores_exact_rows(tmp_path):
    database_path, plan = _build_plan(tmp_path)
    original_digest = _database_digest(database_path)
    _apply(database_path, plan)

    connection = sqlite3.connect(database_path)
    connection.executescript(render_batch150_rollback_sql(plan))
    connection.close()
    assert _database_digest(database_path) == original_digest


def test_rendered_rollback_sql_tolerates_only_leg_553_updated_at_drift(tmp_path):
    database_path, plan = _build_plan(tmp_path)
    original_digest = _database_digest(database_path)
    _apply(database_path, plan)
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE execution_order_legs SET updated_at='runtime-refresh-before-sql' "
        "WHERE id=553"
    )
    connection.commit()

    connection.executescript(render_batch150_rollback_sql(plan))
    connection.close()

    assert _database_digest(database_path) == original_digest


def test_plan_integrity_rejects_tampered_action_before_mutation(tmp_path):
    database_path, plan = _build_plan(tmp_path)
    action = plan.actions[0]
    changed_before = dict(action.before)
    changed_before["attempt_count"] = 99
    tampered_action = replace(action, before=changed_before)
    tampered_plan = replace(plan, actions=(tampered_action, *plan.actions[1:]))
    original_digest = _database_digest(database_path)

    with pytest.raises(Batch150TerminalizationRefused, match="action_fingerprint_invalid"):
        _apply(database_path, tampered_plan)
    assert _database_digest(database_path) == original_digest


def test_cli_plan_apply_idempotent_and_rollback(tmp_path, capsys):
    database_path = tmp_path / "copy.db"
    evidence_path = tmp_path / "exchange.json"
    plan_path = tmp_path / "plan.json"
    rollback_sql_path = tmp_path / "rollback.sql"
    _seed_database(database_path)
    evidence_path.write_text(json.dumps(_complete_exchange_evidence()), encoding="utf-8")

    assert main([
        "plan",
        "--database-path", str(database_path),
        "--exchange-evidence", str(evidence_path),
        "--repair-ts-utc", REPAIR_TS.isoformat(),
        "--code-sha", "f" * 40,
        "--plan-path", str(plan_path),
        "--rollback-sql-path", str(rollback_sql_path),
    ]) == 0
    planned_output = json.loads(capsys.readouterr().out)
    plan = load_batch150_terminalization_plan(plan_path)
    assert planned_output["status"] == "planned"
    assert planned_output["action_count"] == 8
    assert rollback_sql_path.stat().st_mode & 0o777 == 0o600

    apply_args = [
        "apply",
        "--database-path", str(database_path),
        "--plan-path", str(plan_path),
        "--expected-plan-fingerprint", plan.plan_fingerprint,
        "--expected-action-count", "8",
        "--expected-repair-ts-utc", plan.repair_ts_utc,
        "--confirmation-token", plan.confirmation_token,
    ]
    assert main(apply_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    assert main(apply_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_applied"

    assert main([
        "rollback",
        "--database-path", str(database_path),
        "--plan-path", str(plan_path),
        "--expected-rollback-fingerprint", plan.rollback_fingerprint,
        "--expected-action-count", "8",
        "--confirmation-token", plan.confirmation_token,
    ]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "rolled_back"

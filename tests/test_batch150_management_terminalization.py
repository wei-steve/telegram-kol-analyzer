from __future__ import annotations

import copy
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from telegram_kol_research.batch150_management_terminalization import (
    Batch150TerminalizationRefused,
    build_batch150_terminalization_plan,
    load_batch150_terminalization_plan,
    render_batch150_rollback_sql,
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

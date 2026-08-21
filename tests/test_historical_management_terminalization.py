from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from datetime import UTC, datetime

import pytest


REPAIR_TS = datetime(2026, 8, 21, 18, 0, tzinfo=UTC)

TARGETS = {
    123: {
        "batch_reason": "management_reconciliation_identity_mismatch",
        "batch_fp": "d08672fd18fab476a9b7ed70d195d9ff2ccf27eb6d570a78c3b163ba58f6be7c",
        "contract_fp": "cbddc9b6dd2ec5fc26c49c5211ef0dcd73aaafdb2c1aea3faa796b10805b5192",
        "target_fp": "1b46a80258e1ccfea24451b1c59cb1724328b4563e9c303ff61651c5b03dbf90",
        "raw": 10696,
        "source_message": 4250,
        "source_text": "大镖客·Andy\n第一止盈位已到，注意锁定利润，及时移动止损！\n@Tarderfengge QQ:158241758",
        "decision": 10527,
        "lifecycle": 819,
        "binding": 283,
        "execution_leg": 497,
        "management_leg": 107,
        "components": ((4, "recovery_required"), (5, "pending"), (6, "pending")),
        "pos_id": "1001124765619311",
        "size": "2.3",
        "instrument": "ETH-USDT-SWAP",
        "side": "long",
        "execution_status": "filled",
        "management_status": "submitted",
    },
    127: {
        "batch_reason": "take_profit_cancel_retry_exhausted",
        "batch_fp": "265f3c080298324bf0e2e1277a1205c5ddba0979e841e09549a69c604f89f95b",
        "contract_fp": "5329927c8a9a8ce17a4e912d64bac83c6d53f7162382ce16d009e1ab2aec8624",
        "target_fp": "38df032b721c7b25585c8a6f23c384c4dd5ed1754ec2317bb14ca29fbbb80c04",
        "raw": 10747,
        "source_message": 10009,
        "source_text": "",
        "decision": 10578,
        "lifecycle": 816,
        "binding": 282,
        "execution_leg": 496,
        "management_leg": 110,
        "components": ((7, "operator_required"), (8, "pending"), (9, "pending")),
        "pos_id": "1001124765261315",
        "size": "12",
        "instrument": "BTC-USDT-SWAP",
        "side": "long",
        "execution_status": "filled",
        "management_status": "planned",
    },
    129: {
        "batch_reason": "take_profit_order_identity_conflict",
        "batch_fp": "4502bcd6b647a17724beb9c4ab150005a402435da0ea366bbf9c175f08744c71",
        "contract_fp": "117bf9461ae417610429f4f341660e7325ddd7294945ad0e206e46115e423c54",
        "target_fp": "90144990d71b1058815b0e9f116ee4c0f71fc0fd73e714a1456c4d10d7b1f640",
        "raw": 10839,
        "source_message": 4255,
        "source_text": "大镖客·Andy\n第一止盈位已到，注意锁定利润，及时移动止损！\n@Tarderfengge QQ:158241758",
        "decision": 10670,
        "lifecycle": 834,
        "binding": 287,
        "execution_leg": 503,
        "management_leg": 112,
        "components": ((10, "pending"), (11, "pending"), (12, "pending")),
        "pos_id": "1001124787260932",
        "size": "2.3",
        "instrument": "ETH-USDT-SWAP",
        "side": "short",
        "execution_status": "active",
        "management_status": "planned",
    },
    133: {
        "batch_reason": "management_reconciliation_identity_mismatch",
        "batch_fp": "d7ca719ce01bfb29923a2cdfdc7e08f086922386322480724768569c81d973c5",
        "contract_fp": "6680b2b5865195c909c086fc614da34e4eefdeae845fe6d738993145f80203e2",
        "target_fp": "9b397543737394df0be4a9bb682bb330b872d1781cd768ced430e9df32c6e59e",
        "raw": 11279,
        "source_message": 4275,
        "source_text": "大镖客·Andy\n第一止盈位已到，注意锁定利润，及时移动止损！\n@Tarderfengge QQ:158241758",
        "decision": 11110,
        "lifecycle": 859,
        "binding": 292,
        "execution_leg": 511,
        "management_leg": 117,
        "components": ((13, "recovery_required"), (14, "pending"), (15, "pending")),
        "pos_id": "1001124837556751",
        "size": "15",
        "instrument": "BTC-USDT-SWAP",
        "side": "long",
        "execution_status": "filled",
        "management_status": "submitted",
    },
    144: {
        "batch_reason": "management_reconciliation_identity_mismatch",
        "batch_fp": "d3caa0f8209c445f181dbf06725b9dd7f2e60a222297160baa98dd31eca45d25",
        "contract_fp": "e0b5a2a31c10792bb64d0654dfe055064e2d46477d1836dfdf710bc5454bf8bd",
        "target_fp": "54241e51585e92532c62d92e15251ec49aee809e0b3f839a7a43c5f921222fa5",
        "raw": 11892,
        "source_message": 4332,
        "source_text": "大镖客·Andy\n大饼空第一止盈位已到，注意锁定利润，及时移动止损！\n@Tarderfengge QQ:158241758",
        "decision": 11891,
        "lifecycle": 910,
        "binding": 307,
        "execution_leg": 530,
        "management_leg": 125,
        "components": ((16, "recovery_required"), (17, "pending"), (18, "pending")),
        "pos_id": "1001124898122909",
        "size": "8",
        "instrument": "BTC-USDT-SWAP",
        "side": "short",
        "execution_status": "filled",
        "management_status": "submitted",
    },
    146: {
        "batch_reason": "take_profit_cancel_retry_exhausted",
        "batch_fp": "c934ceffc43062fb4d63bd3d7210c2f15661121d60b51453973fa5dfb0f31d40",
        "contract_fp": "3e3ec28e10a34d6afbc487f0f6a463a4fd5d5aa2e39e1c4aafdbf6619c04d0e6",
        "target_fp": "40a60800f3e56adf425d3b14b6bf7d64cfaf6d5b758ae82a4aa34da6509f64b4",
        "raw": 12068,
        "source_message": 8823,
        "source_text": "现价2390附近止盈70%，剩下继续持有，设置成本价止损。\n@Tarderfengge QQ:158241758",
        "decision": 12066,
        "lifecycle": 921,
        "binding": 313,
        "execution_leg": 540,
        "management_leg": 127,
        "components": ((19, "operator_required"), (20, "pending"), (21, "pending")),
        "pos_id": "1001124908211764",
        "size": "2.2",
        "instrument": "ETH-USDT-SWAP",
        "side": "long",
        "execution_status": "filled",
        "management_status": "planned",
    },
}


def _seed_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE strategy_management_batches (
          id INTEGER PRIMARY KEY, status TEXT, reason_code TEXT,
          reconciled_at TEXT, completed_at TEXT, updated_at TEXT,
          idempotency_fingerprint TEXT, management_contract_fingerprint TEXT,
          target_fingerprint TEXT, target_lifecycle_id INTEGER,
          execution_binding_id INTEGER, raw_message_id INTEGER,
          recognition_decision_id INTEGER, strategy_instance_id TEXT
        );
        CREATE TABLE strategy_management_legs (
          id INTEGER PRIMARY KEY, management_batch_id INTEGER,
          execution_order_leg_id INTEGER, pos_id TEXT, status TEXT,
          last_error TEXT, updated_at TEXT
        );
        CREATE TABLE strategy_management_components (
          id INTEGER PRIMARY KEY, management_batch_id INTEGER,
          strategy_management_leg_id INTEGER, component_kind TEXT,
          sequence INTEGER, status TEXT, reason_code TEXT, attempt_count INTEGER,
          idempotency_key TEXT, desired_json TEXT, evidence_json TEXT,
          last_progress_at TEXT, completed_at TEXT, updated_at TEXT
        );
        CREATE TABLE execution_order_legs (
          id INTEGER PRIMARY KEY, execution_binding_id INTEGER,
          strategy_instance_id TEXT, purpose TEXT, status TEXT,
          terminal_reason TEXT, attribution_status TEXT, pos_id TEXT,
          order_id TEXT, client_order_id TEXT, attribution_evidence_json TEXT,
          last_verified_at TEXT, updated_at TEXT
        );
        CREATE TABLE execution_bindings (
          id INTEGER PRIMARY KEY, strategy_instance_id TEXT, status TEXT,
          pos_id TEXT, last_exchange_status TEXT, recovered_at TEXT,
          updated_at TEXT
        );
        CREATE TABLE strategy_lifecycles (
          id INTEGER PRIMARY KEY, execution_binding_id INTEGER,
          lifecycle_status TEXT, exit_reason TEXT, exited_at TEXT,
          management_action TEXT, trade_idea_id INTEGER, updated_at TEXT
        );
        CREATE TABLE position_mutation_intents (
          id INTEGER PRIMARY KEY, execution_binding_id INTEGER,
          execution_order_leg_id INTEGER, operation TEXT, status TEXT,
          pos_id TEXT, authority_fingerprint TEXT, request_fingerprint TEXT
        );
        CREATE TABLE execution_events (id INTEGER PRIMARY KEY);
        CREATE TABLE raw_messages (
          id INTEGER PRIMARY KEY, message_id INTEGER, text TEXT
        );
        CREATE TABLE recognition_decisions (id INTEGER PRIMARY KEY);
        CREATE TABLE position_protection_ledger (id INTEGER PRIMARY KEY);
        CREATE TABLE position_protection_incidents (id INTEGER PRIMARY KEY);
        """
    )
    old_time = "2026-08-21 17:30:00.000000"
    for batch_id, target in TARGETS.items():
        strategy = f"strategy-{batch_id}"
        connection.execute(
            "INSERT INTO strategy_management_batches VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                batch_id, "recovery_required", target["batch_reason"], None,
                None, old_time, target["batch_fp"], target["contract_fp"],
                target["target_fp"], target["lifecycle"], target["binding"],
                target["raw"], target["decision"], strategy,
            ),
        )
        connection.execute(
            "INSERT INTO raw_messages VALUES (?,?,?)",
            (target["raw"], target["source_message"], target["source_text"]),
        )
        connection.execute(
            "INSERT INTO strategy_management_legs VALUES (?,?,?,?,?,?,?)",
            (
                target["management_leg"], batch_id, target["execution_leg"],
                target["pos_id"], target["management_status"],
                '{"reason":"management_close_order_not_found"}'
                if target["management_status"] == "submitted" else None,
                old_time,
            ),
        )
        for sequence, (component_id, status) in enumerate(target["components"]):
            connection.execute(
                "INSERT INTO strategy_management_components VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    component_id, batch_id, target["management_leg"],
                    ("consume_take_profit_stage", "converge_partial_close",
                     "replace_remaining_protection")[sequence],
                    sequence, status,
                    "historical-original-reason" if status != "pending" else None,
                    3 if status == "operator_required" else 0,
                    f"component-key-{component_id}", "{}", "[]", old_time,
                    old_time if status == "operator_required" else None, old_time,
                ),
            )
        connection.execute(
            "INSERT INTO execution_bindings VALUES (?,?,?,?,?,?,?)",
            (
                target["binding"], strategy, "active", target["pos_id"],
                "position_attribution_evidence_unavailable", old_time, old_time,
            ),
        )
        connection.execute(
            "INSERT INTO execution_order_legs VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                target["execution_leg"], target["binding"], strategy, "entry",
                target["execution_status"], None, "verified", target["pos_id"],
                f"order-{batch_id}", f"client-{batch_id}",
                '{"policy_version":2}', old_time, old_time,
            ),
        )
        connection.execute(
            "INSERT INTO strategy_lifecycles VALUES (?,?,?,?,?,?,?,?)",
            (
                target["lifecycle"], target["binding"], "entered", None, None,
                None, None, old_time,
            ),
        )
        connection.execute(
            "INSERT INTO position_mutation_intents VALUES (?,?,?,?,?,?,?,?)",
            (
                1000 + batch_id, target["binding"], target["execution_leg"],
                "set_position_sltp", "confirmed", target["pos_id"],
                f"authority-{batch_id}", f"request-{batch_id}",
            ),
        )
    connection.execute(
        "INSERT INTO execution_order_legs VALUES "
        "(531,307,'strategy-144','entry','active',NULL,'verified',"
        "'1001124899621086','sibling-order','sibling-client',"
        "'{\"policy_version\":2}','2026-08-21 17:30:00.000000',"
        "'2026-08-21 17:30:00.000000')"
    )
    connection.commit()
    connection.close()


def _complete_exchange_evidence():
    batches = {}
    for batch_id, target in TARGETS.items():
        batches[str(batch_id)] = {
            "classification": "historical_terminal/informational",
            "position_history": [{
                "instId": target["instrument"],
                "posId": target["pos_id"],
                "posSide": target["side"],
                "pos": target["size"],
                "closePos": target["size"],
                "uTime": "1787300000000",
            }],
            "positions": [],
            "open_orders": [],
            "pending_trigger_orders": [],
            "position_history_error": None,
        }
    return {
        "snapshot_complete": True,
        "snapshot_errors": {},
        "exchange_write_count": 0,
        "tpsl_conflicts": [],
        "unowned_pending_order_ids": [],
        "batches": batches,
        "sibling": {
            "snapshot_complete": True,
            "binding_id": 307,
            "execution_order_leg_id": 531,
            "pos_id": "1001124899621086",
            "attribution_status": "verified",
            "leg_status": "active",
            "live_position_match_count": 1,
            "protection_complete": True,
            "ownership_conflicts": [],
        },
    }


def test_build_plan_has_exact_45_action_matrix(tmp_path):
    try:
        from telegram_kol_research.historical_management_terminalization import (
            build_terminalization_plan,
        )
    except ImportError as exc:  # expected RED before implementation exists
        pytest.fail(f"terminalization module is missing: {exc}")

    database_path = tmp_path / "copy.db"
    _seed_database(database_path)

    plan = build_terminalization_plan(
        database_path,
        exchange_evidence=_complete_exchange_evidence(),
        repair_ts=REPAIR_TS,
        code_sha="f" * 40,
    )

    assert plan.action_count == 45
    assert len(plan.plan_fingerprint) == 64
    assert len(plan.database_fingerprint) == 64
    assert len(plan.exchange_fingerprint) == 64
    assert len(plan.rollback_fingerprint) == 64
    assert plan.exchange_write_count == 0
    assert [action.table for action in plan.actions].count(
        "strategy_management_components"
    ) == 16
    binding_307 = next(
        action for action in plan.actions
        if action.table == "execution_bindings" and action.pk == 307
    )
    assert binding_307.after["status"] == "active"
    assert binding_307.after["pos_id"] == "1001124899621086"
    assert not any(
        action.table == "strategy_lifecycles" and action.pk == 910
        for action in plan.actions
    )


def test_plan_refuses_wrong_exact_exchange_instrument(tmp_path):
    from telegram_kol_research.historical_management_terminalization import (
        HistoricalManagementTerminalizationRefused,
        build_terminalization_plan,
    )

    database_path = tmp_path / "copy.db"
    _seed_database(database_path)
    evidence = copy.deepcopy(_complete_exchange_evidence())
    evidence["batches"]["127"]["position_history"][0]["instId"] = (
        "ETH-USDT-SWAP"
    )

    with pytest.raises(
        HistoricalManagementTerminalizationRefused,
        match="position_history_full_close_unproven",
    ):
        build_terminalization_plan(
            database_path,
            exchange_evidence=evidence,
            repair_ts=REPAIR_TS,
            code_sha="f" * 40,
        )


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
        ):
            columns = [
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            ]
            rows.append(
                (table, columns, connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall())
            )
        return hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    finally:
        connection.close()


def test_apply_is_cas_idempotent_and_rollback_restores_exact_rows(tmp_path):
    from telegram_kol_research.historical_management_terminalization import (
        apply_terminalization_plan,
        build_terminalization_plan,
        rollback_terminalization_plan,
    )

    database_path = tmp_path / "copy.db"
    _seed_database(database_path)
    original_digest = _database_digest(database_path)
    plan = build_terminalization_plan(
        database_path,
        exchange_evidence=_complete_exchange_evidence(),
        repair_ts=REPAIR_TS,
        code_sha="f" * 40,
    )

    applied = apply_terminalization_plan(
        database_path,
        plan=plan,
        expected_plan_fingerprint=plan.plan_fingerprint,
        expected_action_count=45,
        expected_repair_ts_utc=plan.repair_ts_utc,
        confirmation_token=plan.confirmation_token,
    )
    assert applied.status == "applied"
    assert applied.changed_row_count == 45
    assert applied.quick_check == "ok"
    assert applied.table_counts_before == applied.table_counts_after

    second = apply_terminalization_plan(
        database_path,
        plan=plan,
        expected_plan_fingerprint=plan.plan_fingerprint,
        expected_action_count=45,
        expected_repair_ts_utc=plan.repair_ts_utc,
        confirmation_token=plan.confirmation_token,
    )
    assert second.status == "already_applied"
    assert second.changed_row_count == 0

    rolled_back = rollback_terminalization_plan(
        database_path,
        plan=plan,
        expected_rollback_fingerprint=plan.rollback_fingerprint,
        expected_action_count=45,
        confirmation_token=plan.confirmation_token,
    )
    assert rolled_back.status == "rolled_back"
    assert rolled_back.changed_row_count == 45
    assert rolled_back.quick_check == "ok"
    assert _database_digest(database_path) == original_digest


def test_apply_refuses_drift_before_any_write(tmp_path):
    from telegram_kol_research.historical_management_terminalization import (
        HistoricalManagementTerminalizationRefused,
        apply_terminalization_plan,
        build_terminalization_plan,
    )

    database_path = tmp_path / "copy.db"
    _seed_database(database_path)
    plan = build_terminalization_plan(
        database_path,
        exchange_evidence=_complete_exchange_evidence(),
        repair_ts=REPAIR_TS,
        code_sha="f" * 40,
    )
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE strategy_management_components SET attempt_count=99 WHERE id=4"
    )
    connection.commit()
    connection.close()
    drift_digest = _database_digest(database_path)

    with pytest.raises(
        HistoricalManagementTerminalizationRefused, match="database_state_mixed"
    ):
        apply_terminalization_plan(
            database_path,
            plan=plan,
            expected_plan_fingerprint=plan.plan_fingerprint,
            expected_action_count=45,
            expected_repair_ts_utc=plan.repair_ts_utc,
            confirmation_token=plan.confirmation_token,
        )
    assert _database_digest(database_path) == drift_digest


def test_plan_json_round_trip_and_rollback_sql_are_exact(tmp_path):
    from telegram_kol_research.historical_management_terminalization import (
        apply_terminalization_plan,
        build_terminalization_plan,
        load_terminalization_plan,
        render_rollback_sql,
        write_terminalization_plan,
    )

    database_path = tmp_path / "copy.db"
    plan_path = tmp_path / "plan.json"
    _seed_database(database_path)
    original_digest = _database_digest(database_path)
    plan = build_terminalization_plan(
        database_path,
        exchange_evidence=_complete_exchange_evidence(),
        repair_ts=REPAIR_TS,
        code_sha="f" * 40,
    )

    write_terminalization_plan(plan_path, plan)
    loaded = load_terminalization_plan(plan_path)
    rollback_sql = render_rollback_sql(loaded)

    assert loaded == plan
    assert plan_path.stat().st_mode & 0o777 == 0o600
    assert rollback_sql.startswith("BEGIN IMMEDIATE;\n")
    assert rollback_sql.rstrip().endswith("COMMIT;")
    assert rollback_sql.count("UPDATE ") == 45
    assert loaded.rollback_fingerprint in rollback_sql

    apply_terminalization_plan(
        database_path,
        plan=loaded,
        expected_plan_fingerprint=loaded.plan_fingerprint,
        expected_action_count=45,
        expected_repair_ts_utc=loaded.repair_ts_utc,
        confirmation_token=loaded.confirmation_token,
    )
    connection = sqlite3.connect(database_path)
    connection.executescript(rollback_sql)
    connection.close()
    assert _database_digest(database_path) == original_digest


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda value: value.update(snapshot_complete=False), "exchange_snapshot_incomplete"),
        (
            lambda value: value["batches"]["123"]["positions"].append(
                {"posId": TARGETS[123]["pos_id"]}
            ),
            "target_position_not_terminal",
        ),
        (lambda value: value.pop("sibling"), "batch_144_sibling_missing"),
        (
            lambda value: value["batches"].update({"999": {}}),
            "exchange_target_set_changed",
        ),
    ],
)
def test_plan_refuses_incomplete_or_changed_external_state(tmp_path, mutate, reason):
    from telegram_kol_research.historical_management_terminalization import (
        HistoricalManagementTerminalizationRefused,
        build_terminalization_plan,
    )

    database_path = tmp_path / "copy.db"
    _seed_database(database_path)
    evidence = copy.deepcopy(_complete_exchange_evidence())
    mutate(evidence)
    with pytest.raises(HistoricalManagementTerminalizationRefused, match=reason):
        build_terminalization_plan(
            database_path,
            exchange_evidence=evidence,
            repair_ts=REPAIR_TS,
            code_sha="f" * 40,
        )


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        (
            "UPDATE strategy_management_batches SET status='resolved' WHERE id=123",
            "target_set_changed",
        ),
        (
            "INSERT INTO strategy_management_batches "
            "(id,status) VALUES (999,'recovery_required')",
            "target_set_changed",
        ),
        (
            "UPDATE strategy_management_batches SET idempotency_fingerprint='drift' "
            "WHERE id=123",
            "batch_changed",
        ),
        ("DELETE FROM execution_order_legs WHERE id=531", "batch_144_sibling_missing"),
    ],
)
def test_plan_refuses_local_target_or_identity_drift(tmp_path, statement, reason):
    from telegram_kol_research.historical_management_terminalization import (
        HistoricalManagementTerminalizationRefused,
        build_terminalization_plan,
    )

    database_path = tmp_path / "copy.db"
    _seed_database(database_path)
    connection = sqlite3.connect(database_path)
    connection.execute(statement)
    connection.commit()
    connection.close()
    drift_digest = _database_digest(database_path)

    with pytest.raises(HistoricalManagementTerminalizationRefused, match=reason):
        build_terminalization_plan(
            database_path,
            exchange_evidence=_complete_exchange_evidence(),
            repair_ts=REPAIR_TS,
            code_sha="f" * 40,
        )
    assert _database_digest(database_path) == drift_digest

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_kol_research.frozen_exchange_empty_state_alignment import (
    AlignmentRefused,
    apply_alignment,
    inspect_alignment,
)


ENTERED_IDS = (
    444, 536, 547, 558, 607, 611, 623, 698, 707, 713, 724, 736, 763,
    767, 772, 777, 804, 807, 985, 1012, 1023, 1026, 1034, 1035, 1036,
)
PENDING_LIFECYCLE_IDS = (423, 426, 447, 452, 460, 469, 508, 509, 510, 839)
POSITION_BINDING_IDS = (
    2, 3, 5, 6, 10, 15, 17, 18, 22, 24, 26, 27, 39, 114, 120,
)
ORDER_BINDING_IDS = (
    4, 16, 19, 21, 25, 28, 31, 34, 36, 41, 43, 50, 54, 70, 80, 86,
    94, 98, 101, 102, 105, 108, 116, 118, 119, 121, 128, 145, 146, 147,
    289,
)
TARGET_BINDING_IDS = tuple(sorted(POSITION_BINDING_IDS + ORDER_BINDING_IDS))
RETAINED_BINDING_IDS = (
    1, 7, 8, 12, 13, 20, 30, 32, 35, 37, 38, 40, 42, 45, 47, 49, 51,
    52, 53, 55, 56, 57, 59, 60, 61, 63, 64, 65, 69, 74, 75, 77, 78,
    81, 83, 84, 85, 87, 89, 90, 93, 97, 99, 103, 106, 107, 109, 113,
    115, 117, 122, 123,
)
BOUND = {
    6: 297, 15: 313, 16: 314, 17: 315, 18: 317, 19: 318, 21: 320,
    22: 323, 24: 326, 25: 327, 26: 329, 27: 331, 28: 332, 31: 335,
    34: 338, 36: 342, 39: 345, 41: 348, 43: 351, 50: 363, 54: 368,
    70: 387, 80: 398, 86: 405, 94: 416, 98: 423, 101: 426, 102: 427,
    105: 432, 108: 436, 114: 444, 116: 447, 118: 449, 119: 452,
    120: 457, 121: 460, 128: 469, 145: 508, 146: 509, 147: 510,
    289: 839,
}
POSITION_LEG_IDS = (2, 3, 6, 7, 12, 17, 20, 21, 28, 32, 36, 37, 59, 222, 232)
ORDER_LEG_IDS = (
    4, 5, 18, 19, 22, 23, 26, 27, 29, 33, 34, 35, 38, 39, 40, 44,
    45, 50, 51, 54, 55, 61, 62, 64, 65, 77, 78, 85, 86, 87, 88, 89,
    90, 142, 143, 161, 162, 184, 185, 192, 193, 198, 199, 200, 201,
    206, 207, 212, 213, 223, 225, 226, 228, 229, 230, 231, 233, 234,
    235, 248, 249, 279, 280, 281, 506,
)
TERMINAL_GUARD_LEG_IDS = (171, 172, 507)

LEG_TO_BINDING = {
    2: 2, 3: 3, 4: 4, 5: 4, 6: 5, 7: 6, 12: 10, 17: 15,
    18: 16, 19: 16, 20: 17, 21: 18, 22: 19, 23: 19, 26: 21,
    27: 21, 28: 22, 29: 22, 32: 24, 33: 24, 34: 25, 35: 25,
    36: 26, 37: 27, 38: 27, 39: 28, 40: 28, 44: 31, 45: 31,
    50: 34, 51: 34, 54: 36, 55: 36, 59: 39, 61: 41, 62: 41,
    64: 43, 65: 43, 77: 50, 78: 50, 85: 54, 86: 54, 87: 54,
    88: 54, 89: 54, 90: 54, 142: 70, 143: 70, 161: 80, 162: 80,
    171: 86, 172: 86, 184: 94, 185: 94, 192: 98, 193: 98,
    198: 101, 199: 101, 200: 102, 201: 102, 206: 105, 207: 105,
    212: 108, 213: 108, 222: 114, 223: 114, 225: 116, 226: 116,
    228: 118, 229: 118, 230: 119, 231: 119, 232: 120, 233: 120,
    234: 121, 235: 121, 248: 128, 249: 128, 279: 145, 280: 146,
    281: 147, 506: 289, 507: 289,
}


def _make_database(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE strategy_lifecycles (id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,execution_binding_id INTEGER,lifecycle_status TEXT NOT NULL,exit_reason TEXT,exited_at TEXT,management_action TEXT,management_note TEXT,expiry_review_next_at TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE execution_bindings (id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,strategy_instance_id TEXT NOT NULL,venue TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,order_id TEXT,client_order_id TEXT,status TEXT NOT NULL,pos_id TEXT,last_exchange_status TEXT,recovered_at TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE execution_order_legs (id INTEGER PRIMARY KEY,execution_binding_id INTEGER NOT NULL,strategy_instance_id TEXT NOT NULL,purpose TEXT NOT NULL,order_kind TEXT NOT NULL,venue TEXT NOT NULL,order_id TEXT,client_order_id TEXT,pos_id TEXT,attribution_status TEXT NOT NULL,status TEXT NOT NULL,terminal_reason TEXT,last_verified_at TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE trigger_protection_intents (id INTEGER PRIMARY KEY,execution_binding_id INTEGER NOT NULL,execution_order_leg_id INTEGER NOT NULL,venue TEXT NOT NULL,parent_trigger_order_id TEXT,adopted_order_id TEXT,recovery_state TEXT NOT NULL,recovery_disposition TEXT,last_reason_code TEXT,next_attempt_at TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE position_protection_legs (id INTEGER PRIMARY KEY,execution_binding_id INTEGER NOT NULL,execution_order_leg_id INTEGER NOT NULL,venue TEXT NOT NULL,pos_id TEXT,exchange_order_id TEXT,status TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE trigger_take_profit_convergences (id INTEGER PRIMARY KEY,execution_binding_id INTEGER NOT NULL,execution_order_leg_id INTEGER NOT NULL,venue TEXT NOT NULL,pos_id TEXT,status TEXT NOT NULL,reason_code TEXT,completed_at TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE execution_events (id INTEGER PRIMARY KEY AUTOINCREMENT,execution_binding_id INTEGER,strategy_instance_id TEXT,venue TEXT NOT NULL,action TEXT NOT NULL,status TEXT NOT NULL,chat_id INTEGER,message_id INTEGER,symbol TEXT,side TEXT,order_id TEXT,reason TEXT,after_json TEXT,response_json TEXT,created_at TEXT NOT NULL,notification_status TEXT,notification_fingerprint TEXT UNIQUE,notification_attempts INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE position_attribution_audits (id INTEGER PRIMARY KEY AUTOINCREMENT,execution_binding_id INTEGER,execution_order_leg_id INTEGER,venue TEXT NOT NULL,pos_id TEXT,event_type TEXT NOT NULL,prior_state TEXT,new_state TEXT NOT NULL,fingerprint TEXT NOT NULL UNIQUE,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE raw_messages (id INTEGER PRIMARY KEY,text TEXT,posted_at TEXT);
        """
    )
    ts = "2026-08-31 06:00:00.000000"
    for lifecycle_id in ENTERED_IDS:
        db.execute(
            "INSERT INTO strategy_lifecycles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                lifecycle_id, -1000 - lifecycle_id, lifecycle_id, "BTC", "long",
                114 if lifecycle_id == 444 else None, "entered",
                None, None, None, None, None, ts,
            ),
        )
    for binding_id, lifecycle_id in BOUND.items():
        if lifecycle_id not in ENTERED_IDS:
            db.execute(
                "INSERT INTO strategy_lifecycles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    lifecycle_id, -2000 - lifecycle_id, lifecycle_id, "BTC", "short",
                    binding_id,
                    "pending_entry" if lifecycle_id in PENDING_LIFECYCLE_IDS else "exited",
                    None, None, None, None, None, ts,
                ),
            )
    open_bindings = {86, 119, 121, 128, 145, 146, 147, 289}
    stale_bindings = set(ORDER_BINDING_IDS) - open_bindings
    for binding_id in TARGET_BINDING_IDS:
        lifecycle_id = BOUND.get(binding_id, 10_000 + binding_id)
        status = (
            "open" if binding_id in open_bindings
            else "stale" if binding_id in stale_bindings
            else "unknown"
        )
        exchange = (
            "position_attribution_conflict" if binding_id == 114
            else "position_ownership_unassigned" if status == "stale"
            else "entry_order_pending"
        )
        db.execute(
            "INSERT INTO execution_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                binding_id, -2000 - lifecycle_id, lifecycle_id, f"s-{binding_id}",
                "deepcoin", "BTC", "short", f"binding-order-{binding_id}",
                f"binding-client-{binding_id}", status, None, exchange, None, ts,
            ),
        )
    for leg_id, binding_id in LEG_TO_BINDING.items():
        if leg_id in POSITION_LEG_IDS:
            status = "filled" if leg_id == 232 else "active"
            attribution = "attribution_conflict"
            pos_id = "1001124072502100" if leg_id == 222 else f"pos-{leg_id}"
        elif leg_id in TERMINAL_GUARD_LEG_IDS:
            status = "cancelled" if leg_id == 507 else "manually_closed"
            attribution = "unassigned"
            pos_id = None
        else:
            status = "pending" if binding_id in open_bindings else "unknown"
            attribution = "unassigned"
            pos_id = None
        db.execute(
            "INSERT INTO execution_order_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                leg_id, binding_id, f"s-{binding_id}", "entry", "trigger_limit",
                "deepcoin", f"order-{leg_id}", f"client-{leg_id}", pos_id,
                attribution, status, None, None, ts,
            ),
        )
    for binding_id in RETAINED_BINDING_IDS:
        status = "stale" if binding_id % 2 == 0 else "unknown"
        db.execute(
            "INSERT INTO execution_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                binding_id, -3000 - binding_id, 20_000 + binding_id,
                f"retained-{binding_id}", "deepcoin", "BTC", "long",
                f"historical-order-{binding_id}", f"historical-client-{binding_id}",
                status, None, "historical_terminal_leg_only", None, ts,
            ),
        )
        if binding_id != 8:
            leg_id = 10_000 + binding_id
            db.execute(
                "INSERT INTO execution_order_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    leg_id, binding_id, f"retained-{binding_id}", "entry",
                    "trigger_limit", "deepcoin", f"historical-order-{binding_id}",
                    f"historical-client-{binding_id}", None, "unassigned",
                    "manually_closed", "historical_exchange_position_closed",
                    ts, ts,
                ),
            )
    for intent_id, leg_id in ((128, 506), (129, 507)):
        db.execute(
            "INSERT INTO trigger_protection_intents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                intent_id, 289, leg_id, "deepcoin", f"parent-{intent_id}", None,
                "pending", None, None, None, ts,
            ),
        )
    for row_id, leg_id in zip(
        range(545, 553), (506, 506, 506, 506, 507, 507, 507, 507), strict=True
    ):
        db.execute(
            "INSERT INTO position_protection_legs VALUES (?,?,?,?,?,?,?,?)",
            (row_id, 289, leg_id, "deepcoin", None, None, "planned", ts),
        )
    for row_id, leg_id in ((149, 506), (150, 507)):
        db.execute(
            "INSERT INTO trigger_take_profit_convergences VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row_id, 289, leg_id, "deepcoin", None, "waiting_backup_stop",
                "convergence_waiting_backup_stop", None, ts,
            ),
        )
    db.execute("INSERT INTO raw_messages VALUES (1,'preserved','2026-08-01')")
    db.commit()
    db.close()


def _evidence(**changes):
    value = {
        "captured_at": "2026-08-31T06:10:00Z",
        "snapshot_complete": True,
        "snapshot_errors": {},
        "positions": [],
        "open_orders": [],
        "pending_trigger_orders": [],
        "pending_trigger_orders_by_instrument": {
            "BTC-USDT-SWAP": {"complete": True, "error": None, "orders": []},
            "ETH-USDT-SWAP": {"complete": True, "error": None, "orders": []},
            "SOL-USDT-SWAP": {"complete": True, "error": None, "orders": []},
        },
        "exchange_write_count": 0,
    }
    value.update(changes)
    return value


def _inspect(path: Path):
    return inspect_alignment(
        path,
        exchange_evidence=_evidence(),
        observed_at=datetime(2026, 8, 31, 6, 11, tzinfo=UTC),
        code_sha="a" * 40,
    )


def test_inspection_derives_the_reviewed_claim_cohort_and_not_all_nonterminal_rows(
    tmp_path,
):
    path = tmp_path / "db.sqlite"
    _make_database(path)

    inspection = _inspect(path)

    assert inspection.action_count == 173
    assert inspection.target_counts == {
        "entered_lifecycles": 25,
        "pending_lifecycles": 10,
        "execution_bindings": 46,
        "execution_order_legs": 80,
        "trigger_protection_intents": 2,
        "position_protection_legs": 8,
        "trigger_take_profit_convergences": 2,
    }
    assert inspection.guard_counts == {
        "strategy_lifecycles": 65,
        "execution_bindings": 46,
        "execution_order_legs": 83,
        "trigger_protection_intents": 2,
        "position_protection_legs": 8,
        "trigger_take_profit_convergences": 2,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"snapshot_complete": False},
        {"snapshot_errors": {"positions": "timeout"}},
        {"positions": [{}]},
        {"open_orders": [{}]},
        {"pending_trigger_orders": [{}]},
    ],
)
def test_inspection_refuses_unknown_or_nonflat_exchange(tmp_path, change):
    path = tmp_path / "db.sqlite"
    _make_database(path)
    with pytest.raises(AlignmentRefused):
        inspect_alignment(
            path,
            exchange_evidence=_evidence(**change),
            observed_at=datetime(2026, 8, 31, 6, 11, tzinfo=UTC),
            code_sha="a" * 40,
        )


@pytest.mark.parametrize(
    "instrument_rows",
    [
        {},
        {
            "BTC-USDT-SWAP": {"complete": True, "error": None, "orders": []},
            "ETH-USDT-SWAP": {"complete": True, "error": None, "orders": []},
        },
        {
            "BTC-USDT-SWAP": {"complete": True, "error": None, "orders": []},
            "ETH-USDT-SWAP": {"complete": False, "error": "timeout", "orders": []},
            "SOL-USDT-SWAP": {"complete": True, "error": None, "orders": []},
        },
        {
            "BTC-USDT-SWAP": {"complete": True, "error": None, "orders": [{}]},
            "ETH-USDT-SWAP": {"complete": True, "error": None, "orders": []},
            "SOL-USDT-SWAP": {"complete": True, "error": None, "orders": []},
        },
    ],
)
def test_inspection_requires_complete_zero_trigger_proof_for_each_instrument(
    tmp_path, instrument_rows
):
    path = tmp_path / "db.sqlite"
    _make_database(path)
    with pytest.raises(AlignmentRefused):
        inspect_alignment(
            path,
            exchange_evidence=_evidence(
                pending_trigger_orders_by_instrument=instrument_rows
            ),
            observed_at=datetime(2026, 8, 31, 6, 11, tzinfo=UTC),
            code_sha="a" * 40,
        )


def test_non_entry_leg_does_not_expand_the_reviewed_claim_target(tmp_path):
    path = tmp_path / "db.sqlite"
    _make_database(path)
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO execution_order_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            999, 8, "retained-8", "management", "market", "deepcoin",
            "historical-management", "historical-management-client", "pos-old",
            "attribution_conflict", "active", None, None, "now",
        ),
    )
    db.commit()
    db.close()

    inspection = _inspect(path)

    assert inspection.target_counts["execution_bindings"] == 46


def test_apply_uses_existing_terminal_semantics_and_preserves_raw_messages(tmp_path):
    path = tmp_path / "db.sqlite"
    _make_database(path)
    inspection = _inspect(path)
    before = sqlite3.connect(path)
    raw_before = before.execute("SELECT * FROM raw_messages").fetchall()
    retained_before = before.execute(
        "SELECT * FROM execution_bindings WHERE id IN ("
        + ",".join("?" for _ in RETAINED_BINDING_IDS)
        + ") ORDER BY id",
        RETAINED_BINDING_IDS,
    ).fetchall()
    before.close()
    result = apply_alignment(
        path,
        exchange_evidence=_evidence(),
        expected_fingerprint=inspection.fingerprint,
        repair_ts=datetime(2026, 8, 31, 6, 11, tzinfo=UTC),
        applied_at=datetime(2026, 8, 31, 6, 11, 30, tzinfo=UTC),
        code_sha="a" * 40,
    )
    assert result.changed_rows_by_table == {
        "execution_bindings": 46,
        "execution_order_legs": 80,
        "position_protection_legs": 8,
        "strategy_lifecycles": 35,
        "trigger_protection_intents": 2,
        "trigger_take_profit_convergences": 2,
    }
    db = sqlite3.connect(path)
    assert db.execute(
        "SELECT COUNT(*) FROM strategy_lifecycles WHERE lifecycle_status='entered'"
    ).fetchone() == (0,)
    assert db.execute(
        "SELECT lifecycle_status,exit_reason FROM strategy_lifecycles WHERE id=444"
    ).fetchone() == ("exited", "exchange_closed")
    assert db.execute(
        "SELECT status,last_exchange_status FROM execution_bindings WHERE id=114"
    ).fetchone() == ("closed", "historical_cleanup_terminal")
    assert db.execute(
        "SELECT status,terminal_reason,pos_id,attribution_status "
        "FROM execution_order_legs WHERE id=222"
    ).fetchone() == (
        "closed", "historical_exchange_position_closed",
        "1001124072502100", "attribution_conflict",
    )
    assert db.execute(
        "SELECT COUNT(*) FROM execution_bindings WHERE status='closed'"
    ).fetchone() == (15,)
    assert db.execute(
        "SELECT COUNT(*) FROM execution_bindings WHERE status='cancelled'"
    ).fetchone() == (31,)
    assert db.execute(
        "SELECT COUNT(*) FROM strategy_lifecycles WHERE lifecycle_status='expired'"
    ).fetchone() == (10,)
    assert db.execute("SELECT COUNT(*) FROM execution_events").fetchone() == (67,)
    assert db.execute(
        "SELECT COUNT(*) FROM position_attribution_audits"
    ).fetchone() == (55,)
    assert db.execute("SELECT * FROM raw_messages").fetchall() == raw_before
    assert db.execute(
        "SELECT * FROM execution_bindings WHERE id IN ("
        + ",".join("?" for _ in RETAINED_BINDING_IDS)
        + ") ORDER BY id",
        RETAINED_BINDING_IDS,
    ).fetchall() == retained_before
    db.close()


def test_timestamp_drift_is_ignored_but_substantive_drift_is_refused(tmp_path):
    path = tmp_path / "db.sqlite"
    _make_database(path)
    inspection = _inspect(path)
    db = sqlite3.connect(path)
    db.execute(
        "UPDATE execution_bindings SET updated_at='later',recovered_at='later' WHERE id=98"
    )
    db.commit()
    db.close()
    apply_alignment(
        path,
        exchange_evidence=_evidence(),
        expected_fingerprint=inspection.fingerprint,
        repair_ts=datetime(2026, 8, 31, 6, 11, tzinfo=UTC),
        applied_at=datetime(2026, 8, 31, 6, 11, 30, tzinfo=UTC),
        code_sha="a" * 40,
    )
    other = tmp_path / "other.sqlite"
    _make_database(other)
    other_inspection = _inspect(other)
    db = sqlite3.connect(other)
    db.execute("UPDATE execution_bindings SET status='closed' WHERE id=98")
    db.commit()
    db.close()
    with pytest.raises(AlignmentRefused):
        apply_alignment(
            other,
            exchange_evidence=_evidence(),
            expected_fingerprint=other_inspection.fingerprint,
            repair_ts=datetime(2026, 8, 31, 6, 11, tzinfo=UTC),
            applied_at=datetime(2026, 8, 31, 6, 11, 30, tzinfo=UTC),
            code_sha="a" * 40,
        )


def test_mid_transaction_failure_rolls_back_updates_and_audits(tmp_path):
    path = tmp_path / "db.sqlite"
    _make_database(path)
    inspection = _inspect(path)
    with pytest.raises(RuntimeError, match="injected"):
        apply_alignment(
            path,
            exchange_evidence=_evidence(),
            expected_fingerprint=inspection.fingerprint,
            repair_ts=datetime(2026, 8, 31, 6, 11, tzinfo=UTC),
            applied_at=datetime(2026, 8, 31, 6, 11, 30, tzinfo=UTC),
            code_sha="a" * 40,
            fail_after_step=3,
        )
    db = sqlite3.connect(path)
    assert db.execute(
        "SELECT COUNT(*) FROM strategy_lifecycles WHERE lifecycle_status='entered'"
    ).fetchone() == (25,)
    assert db.execute("SELECT COUNT(*) FROM execution_events").fetchone() == (0,)
    assert db.execute(
        "SELECT COUNT(*) FROM position_attribution_audits"
    ).fetchone() == (0,)
    db.close()


def test_extra_related_leg_and_stale_exchange_evidence_are_refused(tmp_path):
    path = tmp_path / "db.sqlite"
    _make_database(path)
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO execution_order_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            999, 98, "s-98", "entry", "trigger_limit", "deepcoin",
            "o", "c", None, "unassigned", "pending", None, None, "now",
        ),
    )
    db.commit()
    db.close()
    with pytest.raises(AlignmentRefused, match="entry_leg_guard_set_changed"):
        _inspect(path)
    clean = tmp_path / "clean.sqlite"
    _make_database(clean)
    with pytest.raises(AlignmentRefused, match="exchange_snapshot_not_fresh"):
        inspect_alignment(
            clean,
            exchange_evidence=_evidence(),
            observed_at=datetime(2026, 8, 31, 6, 13, tzinfo=UTC),
            code_sha="a" * 40,
        )

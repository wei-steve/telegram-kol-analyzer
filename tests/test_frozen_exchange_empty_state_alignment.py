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
    444, 547, 558, 623, 707, 713, 724, 736, 763, 767,
    772, 777, 804, 807, 985, 1012, 1023, 1026, 1034, 1035,
)
BOUND = {
    98: 423, 101: 426, 114: 444, 116: 447, 119: 452,
    121: 460, 128: 469, 145: 508, 146: 509, 147: 510, 289: 839,
}
LEGS = (
    (192, 98, "unknown", "unassigned", None),
    (193, 98, "unknown", "unassigned", None),
    (198, 101, "unknown", "unassigned", None),
    (199, 101, "unknown", "unassigned", None),
    (222, 114, "active", "attribution_conflict", "1001124072502100"),
    (223, 114, "unknown", "unassigned", None),
    (225, 116, "unknown", "unassigned", None),
    (226, 116, "unknown", "unassigned", None),
    (230, 119, "pending", "unassigned", None),
    (231, 119, "pending", "unassigned", None),
    (234, 121, "pending", "unassigned", None),
    (235, 121, "pending", "unassigned", None),
    (248, 128, "pending", "unassigned", None),
    (249, 128, "pending", "unassigned", None),
    (279, 145, "pending", "unassigned", None),
    (280, 146, "pending", "unassigned", None),
    (281, 147, "pending", "unassigned", None),
    (506, 289, "pending", "unassigned", None),
    (507, 289, "cancelled", "unassigned", None),
)


def _make_database(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE strategy_lifecycles (id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,execution_binding_id INTEGER,lifecycle_status TEXT NOT NULL,exit_reason TEXT,exited_at TEXT,management_action TEXT,management_note TEXT,expiry_review_next_at TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE execution_bindings (id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,strategy_instance_id TEXT NOT NULL,venue TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,status TEXT NOT NULL,pos_id TEXT,last_exchange_status TEXT,recovered_at TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE execution_order_legs (id INTEGER PRIMARY KEY,execution_binding_id INTEGER NOT NULL,strategy_instance_id TEXT NOT NULL,purpose TEXT NOT NULL,order_kind TEXT NOT NULL,venue TEXT NOT NULL,order_id TEXT,client_order_id TEXT,pos_id TEXT,attribution_status TEXT NOT NULL,status TEXT NOT NULL,terminal_reason TEXT,last_verified_at TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE trigger_protection_intents (id INTEGER PRIMARY KEY,execution_binding_id INTEGER NOT NULL,execution_order_leg_id INTEGER NOT NULL,venue TEXT NOT NULL,recovery_state TEXT NOT NULL,recovery_disposition TEXT,last_reason_code TEXT,next_attempt_at TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE position_protection_legs (id INTEGER PRIMARY KEY,execution_binding_id INTEGER NOT NULL,execution_order_leg_id INTEGER NOT NULL,venue TEXT NOT NULL,status TEXT NOT NULL,updated_at TEXT NOT NULL);
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
        if lifecycle_id != 444:
            db.execute(
                "INSERT INTO strategy_lifecycles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    lifecycle_id, -2000 - lifecycle_id, lifecycle_id, "BTC", "short",
                    binding_id, "pending_entry", None, None, None, None, None, ts,
                ),
            )
        status = {98: "stale", 101: "stale", 114: "unknown", 116: "stale"}.get(
            binding_id, "open"
        )
        exchange = (
            "position_attribution_conflict" if binding_id == 114
            else "position_ownership_unassigned" if status == "stale"
            else "entry_order_pending"
        )
        db.execute(
            "INSERT INTO execution_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                binding_id, -2000 - lifecycle_id, lifecycle_id, f"s-{binding_id}",
                "deepcoin", "BTC", "short", status, None, exchange, None, ts,
            ),
        )
    for leg_id, binding_id, status, attribution, pos_id in LEGS:
        db.execute(
            "INSERT INTO execution_order_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                leg_id, binding_id, f"s-{binding_id}", "entry", "trigger_limit",
                "deepcoin", f"order-{leg_id}", f"client-{leg_id}", pos_id,
                attribution, status, None, None, ts,
            ),
        )
    for intent_id, leg_id in ((128, 506), (129, 507)):
        db.execute(
            "INSERT INTO trigger_protection_intents VALUES (?,?,?,?,?,?,?,?,?)",
            (intent_id, 289, leg_id, "deepcoin", "pending", None, None, None, ts),
        )
    for row_id, leg_id in zip(
        range(545, 553), (506, 506, 506, 506, 507, 507, 507, 507), strict=True
    ):
        db.execute(
            "INSERT INTO position_protection_legs VALUES (?,?,?,?,?,?)",
            (row_id, 289, leg_id, "deepcoin", "planned", ts),
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


def test_apply_uses_existing_terminal_semantics_and_preserves_raw_messages(tmp_path):
    path = tmp_path / "db.sqlite"
    _make_database(path)
    inspection = _inspect(path)
    raw_before = sqlite3.connect(path).execute("SELECT * FROM raw_messages").fetchall()
    result = apply_alignment(
        path,
        exchange_evidence=_evidence(),
        expected_fingerprint=inspection.fingerprint,
        repair_ts=datetime(2026, 8, 31, 6, 11, tzinfo=UTC),
        applied_at=datetime(2026, 8, 31, 6, 11, 30, tzinfo=UTC),
        code_sha="a" * 40,
    )
    assert sum(result.changed_rows_by_table.values()) == 72
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
        "SELECT COUNT(*) FROM execution_bindings WHERE status='cancelled'"
    ).fetchone() == (10,)
    assert db.execute(
        "SELECT COUNT(*) FROM strategy_lifecycles WHERE lifecycle_status='expired'"
    ).fetchone() == (10,)
    assert db.execute("SELECT COUNT(*) FROM execution_events").fetchone() == (18,)
    assert db.execute(
        "SELECT COUNT(*) FROM position_attribution_audits"
    ).fetchone() == (4,)
    assert db.execute("SELECT * FROM raw_messages").fetchall() == raw_before
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
    ).fetchone() == (20,)
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
    with pytest.raises(AlignmentRefused, match="entry_leg_set_changed"):
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

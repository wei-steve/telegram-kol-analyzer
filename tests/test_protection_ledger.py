import sqlite3
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import PositionBackupStopOrder
from telegram_kol_research.models import PositionProtectionIncident, PositionProtectionLedger
from telegram_kol_research.protection_ledger import (
    build_account_protection_ownership,
    list_verified_ledger_rows_for_positions,
    load_account_protection_ownership,
    upsert_protection_ledger_row,
    retained_take_profit_total,
)


def test_bootstrap_creates_position_protection_ledger_table(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(position_protection_ledger)"
        ).fetchall()
    }
    indexes = {
        row[1]
        for row in conn.execute(
            "PRAGMA index_list(position_protection_ledger)"
        ).fetchall()
    }
    conn.close()

    assert "position_protection_ledger" in tables
    assert {
        "venue",
        "execution_binding_id",
        "execution_order_leg_id",
        "strategy_instance_id",
        "pos_id",
        "instrument_id",
        "side",
        "order_id",
        "purpose",
        "trigger_price",
        "size_text",
        "status",
        "evidence_source",
        "evidence_json",
        "first_seen_at",
        "last_seen_at",
        "last_verified_at",
    } <= columns
    assert "uq_position_protection_ledger_venue_order" in indexes


def test_upsert_protection_ledger_row_updates_by_order_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    seen_at = datetime(2026, 7, 17, 9, 0)

    with session_factory() as session:
        created = upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=144,
            execution_order_leg_id=277,
            strategy_instance_id="deepcoin:-1003825498321:499:BTC:long",
            pos_id="1001124164749504",
            instrument_id="BTC-USDT-SWAP",
            side="long",
            order_id="1001124164749550",
            purpose="stop_loss",
            trigger_price="61500",
            size_text="0",
            status="verified",
            evidence_source="entry_protection_response",
            evidence={"match": "exact_written_order"},
            seen_at=seen_at,
        )
        session.commit()
        first_id = created.id

    with session_factory() as session:
        updated = upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=144,
            execution_order_leg_id=277,
            strategy_instance_id="deepcoin:-1003825498321:499:BTC:long",
            pos_id="1001124164749504",
            instrument_id="BTC-USDT-SWAP",
            side="long",
            order_id="1001124164749550",
            purpose="stop_loss",
            trigger_price="62600",
            size_text="0",
            status="verified",
            evidence_source="management_tpsl_replacement",
            evidence={"match": "replacement_confirmed"},
            seen_at=datetime(2026, 7, 17, 9, 5),
        )
        session.commit()
        assert updated.id == first_id

    with session_factory() as session:
        rows = list_verified_ledger_rows_for_positions(
            session, ["1001124164749504"]
        )

    assert len(rows) == 1
    assert rows[0].id == first_id
    assert rows[0].trigger_price == "62600"
    assert rows[0].evidence_source == "management_tpsl_replacement"


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("execution_binding_id", 145),
        ("execution_order_leg_id", 278),
        ("strategy_instance_id", "deepcoin:other"),
        ("pos_id", "other-pos"),
        ("instrument_id", "ETH-USDT-SWAP"),
        ("side", "short"),
    ],
)
def test_upsert_protection_ledger_row_refuses_owner_conflict(
    tmp_path,
    field,
    changed,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    owner = {
        "venue": "deepcoin",
        "execution_binding_id": 144,
        "execution_order_leg_id": 277,
        "strategy_instance_id": "deepcoin:strategy-1",
        "pos_id": "pos-1",
        "instrument_id": "BTC-USDT-SWAP",
        "side": "long",
        "order_id": "order-1",
    }
    with session_factory() as session:
        upsert_protection_ledger_row(
            session,
            **owner,
            purpose="stop_loss",
            trigger_price="61500",
            size_text="1",
            status="verified",
            evidence_source="initial",
            evidence={"version": 1},
        )
        session.commit()

    conflicting = {**owner, field: changed}
    with session_factory() as session:
        with pytest.raises(ValueError, match="protection_ledger_owner_conflict"):
            upsert_protection_ledger_row(
                session,
                **conflicting,
                purpose="stop_loss",
                trigger_price="62000",
                size_text="1",
                status="verified",
                evidence_source="conflict",
                evidence={"version": 2},
            )
        session.rollback()

    with session_factory() as session:
        persisted = session.query(PositionProtectionLedger).one()
        assert persisted.execution_binding_id == owner["execution_binding_id"]
        assert persisted.execution_order_leg_id == owner["execution_order_leg_id"]
        assert persisted.strategy_instance_id == owner["strategy_instance_id"]
        assert persisted.pos_id == owner["pos_id"]
        assert persisted.instrument_id == owner["instrument_id"]
        assert persisted.side == owner["side"]
        assert persisted.trigger_price == "61500"
        assert persisted.evidence_source == "initial"


def test_upsert_protection_ledger_row_skips_empty_order_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        row = upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=144,
            execution_order_leg_id=277,
            strategy_instance_id="deepcoin:-1003825498321:499:BTC:long",
            pos_id="1001124164749504",
            instrument_id="BTC-USDT-SWAP",
            side="long",
            order_id="",
            purpose="stop_loss",
            trigger_price="61500",
            size_text="0",
            status="verified",
            evidence_source="entry_protection_response",
            evidence={"match": "missing_order_id"},
            seen_at=datetime(2026, 7, 17, 9, 0),
        )
        session.commit()

    assert row is None
    with session_factory() as session:
        assert list_verified_ledger_rows_for_positions(
            session, ["1001124164749504"]
        ) == []


def test_retained_take_profit_total_requires_exact_owner_and_position_bound():
    rows = [
        {
            "order_id": "tp-2",
            "purpose": "take_profit",
            "status": "verified",
            "execution_binding_id": 1,
            "execution_order_leg_id": 2,
            "pos_id": "pos-1",
            "size_text": "3",
        },
        {
            "order_id": "tp-3",
            "purpose": "take_profit",
            "status": "verified",
            "execution_binding_id": 1,
            "execution_order_leg_id": 2,
            "pos_id": "pos-1",
            "size_text": "2",
        },
    ]

    assert retained_take_profit_total(
        rows,
        execution_binding_id=1,
        execution_order_leg_id=2,
        pos_id="pos-1",
        live_position_size="5",
    ) == "5"

    with pytest.raises(ValueError, match="retained_take_profit_exceeds_position"):
        retained_take_profit_total(
            rows,
            execution_binding_id=1,
            execution_order_leg_id=2,
            pos_id="pos-1",
            live_position_size="4",
        )


def test_load_account_protection_ownership_indexes_exact_active_rows(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        for order_id, pos_id, purpose in (
            ("ord-a", "pos-a", "stop_loss"),
            ("ord-b", "pos-a", "take_profit"),
            ("ord-c", "pos-b", "stop_loss"),
        ):
            upsert_protection_ledger_row(
                session,
                venue="deepcoin",
                execution_binding_id=1,
                execution_order_leg_id=2,
                strategy_instance_id="strategy-1",
                pos_id=pos_id,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id=order_id,
                purpose=purpose,
                trigger_price="60000",
                size_text="0",
                status="verified",
                evidence_source="test",
                evidence={},
            )
        session.commit()

    with session_factory() as session:
        index = load_account_protection_ownership(
            session,
            venue="deepcoin",
            live_pos_ids={"pos-a", "pos-b"},
        )

    assert index.owner_for_order("ord-a").pos_id == "pos-a"
    assert index.orders_for_position("pos-a") == ("ord-a", "ord-b")
    assert index.orders_for_position("pos-b") == ("ord-c",)
    assert index.owner_for_order("unknown") is None
    assert index.conflicts == ()
    assert index.stale_order_ids == ()


def test_account_protection_ownership_excludes_terminal_and_marks_stale(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        active = upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=1,
            execution_order_leg_id=2,
            strategy_instance_id="strategy-1",
            pos_id="closed-pos",
            instrument_id="BTC-USDT-SWAP",
            side="long",
            order_id="stale-active",
            purpose="stop_loss",
            trigger_price="60000",
            size_text="0",
            status="verified",
            evidence_source="test",
            evidence={},
        )
        terminal = upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=1,
            execution_order_leg_id=2,
            strategy_instance_id="strategy-1",
            pos_id="pos-a",
            instrument_id="BTC-USDT-SWAP",
            side="long",
            order_id="cancelled-1",
            purpose="stop_loss",
            trigger_price="60000",
            size_text="0",
            status="cancelled",
            evidence_source="test",
            evidence={},
        )
        session.commit()
        assert active is not None
        assert terminal is not None

    with session_factory() as session:
        index = load_account_protection_ownership(
            session,
            venue="deepcoin",
            live_pos_ids={"pos-a"},
        )

    assert index.owner_for_order("cancelled-1") is None
    assert index.owner_for_order("stale-active").pos_id == "closed-pos"
    assert index.stale_order_ids == ("stale-active",)


def test_account_protection_ownership_reports_duplicate_owner_conflict():
    rows = [
        {
            "venue": "deepcoin",
            "order_id": "same-order",
            "pos_id": "pos-a",
            "status": "verified",
            "purpose": "stop_loss",
        },
        {
            "venue": "deepcoin",
            "order_id": "same-order",
            "pos_id": "pos-b",
            "status": "verified",
            "purpose": "stop_loss",
        },
    ]

    index = build_account_protection_ownership(
        rows,
        venue="deepcoin",
        live_pos_ids={"pos-a", "pos-b"},
    )

    assert index.owner_for_order("same-order") is None
    assert index.conflicts[0].order_id == "same-order"
    assert index.conflicts[0].pos_ids == ("pos-a", "pos-b")


def test_bootstrap_creates_backup_stop_and_protection_incident_tables(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    backup_indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(position_backup_stop_orders)")
    }
    conn.close()

    assert {"position_backup_stop_orders", "position_protection_incidents"} <= tables
    assert "uq_position_backup_stop_orders_active_position" in backup_indexes
    assert "uq_position_backup_stop_orders_venue_order" in backup_indexes


def test_backup_stop_allows_only_one_active_order_per_exact_position_and_unique_order_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        session.add(_backup_stop(order_id="backup-1"))
        session.commit()

    with session_factory() as session:
        session.add(_backup_stop(order_id="backup-2"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.add(_backup_stop(order_id="backup-1", pos_id="pos-2", status="cancelled"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_protection_incident_fingerprint_is_unique(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        session.add(_protection_incident())
        session.commit()

    with session_factory() as session:
        session.add(_protection_incident())
        with pytest.raises(IntegrityError):
            session.commit()


def _backup_stop(*, order_id: str, pos_id: str = "pos-1", status: str = "active") -> PositionBackupStopOrder:
    return PositionBackupStopOrder(
        venue="deepcoin",
        execution_binding_id=1,
        execution_order_leg_id=2,
        pos_id=pos_id,
        instrument_id="ETH-USDT-SWAP",
        side="long",
        trigger_price="1909.4",
        order_id=order_id,
        client_order_id=f"client-{order_id}",
        status=status,
        request_json="{}",
    )


def _protection_incident() -> PositionProtectionIncident:
    return PositionProtectionIncident(
        venue="deepcoin",
        execution_binding_id=1,
        execution_order_leg_id=2,
        pos_id="pos-1",
        incident_type="stop_trigger_failed",
        fingerprint="abc123",
        evidence_json="{}",
        delivery_status="pending",
    )

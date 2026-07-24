import sqlite3
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import PositionBackupStopOrder
from telegram_kol_research.models import PositionProtectionIncident
from telegram_kol_research.protection_ledger import (
    list_verified_ledger_rows_for_positions,
    upsert_protection_ledger_row,
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

import sqlite3
from datetime import datetime

from telegram_kol_research.db import create_session_factory
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

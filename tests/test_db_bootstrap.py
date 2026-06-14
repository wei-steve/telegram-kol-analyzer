import sqlite3

from sqlalchemy import text

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import Base


def test_database_bootstrap_creates_tables(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    engine = session_factory.kw["bind"]
    tables = set(Base.metadata.tables)
    assert "raw_messages" in tables
    assert "signal_candidates" in tables
    assert "trade_ideas" in tables
    assert engine is not None


def test_database_bootstrap_backfills_missing_sqlite_columns(tmp_path):
    database_path = tmp_path / "research.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE signal_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_message_id INTEGER,
            symbol VARCHAR(64),
            side VARCHAR(16),
            entry_text VARCHAR(255),
            stop_loss_text VARCHAR(255),
            take_profit_text TEXT,
            leverage_text VARCHAR(64),
            parse_source VARCHAR(32),
            confidence FLOAT,
            review_status VARCHAR(32),
            created_at DATETIME
        )
        """
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(signal_candidates)").fetchall()
    }
    conn.close()

    assert "source_id" in columns
    assert "event_type" in columns
    assert "review_note" in columns


def test_database_bootstrap_backfills_missing_execution_binding_columns(tmp_path):
    database_path = tmp_path / "research.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE execution_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kol_id VARCHAR(255),
            chat_id INTEGER,
            message_id INTEGER,
            symbol VARCHAR(64),
            side VARCHAR(16),
            venue VARCHAR(64),
            order_id VARCHAR(255),
            created_at DATETIME
        )
        """
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(execution_bindings)").fetchall()
    }
    conn.close()

    assert "pos_id" in columns
    assert "status" in columns
    assert "updated_at" in columns


def test_database_bootstrap_backfills_missing_recovery_decision_columns(tmp_path):
    database_path = tmp_path / "research.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE recovery_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kol_id VARCHAR(255),
            chat_id INTEGER,
            message_id INTEGER,
            symbol VARCHAR(64),
            side VARCHAR(16),
            action VARCHAR(64),
            created_at DATETIME
        )
        """
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(recovery_decisions)").fetchall()
    }
    conn.close()

    assert "reason_codes_json" in columns
    assert "entry_range_text" in columns
    assert "max_loss_usdt" in columns
    assert "run_at" in columns
    assert "updated_at" in columns


def test_database_bootstrap_enables_sqlite_busy_timeout(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        busy_timeout = session.execute(text("PRAGMA busy_timeout")).scalar_one()

    assert busy_timeout >= 30000

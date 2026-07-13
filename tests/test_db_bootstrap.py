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
    assert "recognition_experiments" in tables
    assert engine is not None


def test_database_bootstrap_creates_prompt_registry_tables(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    assert {
        "ai_prompt_definitions",
        "ai_prompt_versions",
        "ai_prompt_test_runs",
        "ai_prompt_invocations",
    } <= names


def test_database_bootstrap_creates_recognition_decisions_table(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(recognition_decisions)").fetchall()
    }
    conn.close()

    assert {
        "raw_message_id",
        "input_kind",
        "authoritative_model",
        "authoritative_status",
        "authoritative_payload_json",
        "auxiliary_model",
        "auxiliary_status",
        "auxiliary_payload_json",
        "agreement_status",
        "differences_json",
        "automation_status",
        "automation_reason",
        "notification_status",
        "notification_error",
        "created_at",
        "updated_at",
    }.issubset(columns)


def test_database_bootstrap_creates_trading_settings_table(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    engine = session_factory.kw["bind"]

    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(trading_settings)")).fetchall()
        }

    assert {"key", "value_json", "updated_at"}.issubset(columns)


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


def test_database_bootstrap_creates_trade_signals_table(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(trade_signals)").fetchall()
    }
    conn.close()

    assert "signal_uid" in columns
    assert "strategy_instance_id" in columns
    assert "payload_json" in columns
    assert "result_json" in columns
    assert "processed_at" in columns
    assert "updated_at" in columns


def test_database_bootstrap_creates_execution_events_table(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(execution_events)").fetchall()
    }
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(execution_events)").fetchall()
    }
    conn.close()

    assert {
        "strategy_instance_id",
        "execution_binding_id",
        "trade_signal_id",
        "action",
        "status",
        "order_id",
        "pos_id",
        "before_json",
        "after_json",
        "request_json",
        "response_json",
        "exchange_event_time",
    }.issubset(columns)
    assert "ix_execution_events_strategy_created" in indexes
    assert "ix_execution_events_order" in indexes


def test_database_bootstrap_creates_unique_bound_position_close_reservations(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(bound_position_close_reservations)").fetchall()
    }
    unique_indexes = [
        row[1]
        for row in conn.execute("PRAGMA index_list(bound_position_close_reservations)").fetchall()
        if row[2]
    ]
    conn.close()

    assert {"pos_id", "execution_binding_id", "status", "last_error"}.issubset(columns)
    assert unique_indexes


def test_database_bootstrap_creates_execution_order_legs_table(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(execution_order_legs)").fetchall()
    }
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(execution_order_legs)").fetchall()
    }
    conn.close()

    assert {
        "execution_binding_id",
        "strategy_instance_id",
        "leg_index",
        "purpose",
        "order_kind",
        "order_id",
        "client_order_id",
        "pos_id",
        "status",
        "request_json",
        "response_json",
    }.issubset(columns)
    assert "ix_execution_order_legs_binding" in indexes
    assert "ix_execution_order_legs_pos" in indexes


def test_database_bootstrap_enables_sqlite_busy_timeout(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        busy_timeout = session.execute(text("PRAGMA busy_timeout")).scalar_one()

    assert busy_timeout >= 30000


def test_database_bootstrap_backfills_web_performance_indexes(tmp_path):
    database_path = tmp_path / "research.db"

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    raw_message_indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(raw_messages)").fetchall()
    }
    lifecycle_indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(strategy_lifecycles)").fetchall()
    }
    conn.close()

    assert "ix_raw_messages_chat_posted_message" in raw_message_indexes
    assert "ix_strategy_lifecycles_chat_status_signal" in lifecycle_indexes
    assert "ix_strategy_lifecycles_chat_status_entered" in lifecycle_indexes
    assert "ix_strategy_lifecycles_chat_status_exited" in lifecycle_indexes

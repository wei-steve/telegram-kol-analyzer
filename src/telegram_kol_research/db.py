"""Database bootstrap helpers for the local research app."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import Base


SQLITE_COMPAT_COLUMNS: dict[str, dict[str, str]] = {
    "raw_messages": {
        "sender_name": "ALTER TABLE raw_messages ADD COLUMN sender_name VARCHAR(255)",
        "archived_target_group": "ALTER TABLE raw_messages ADD COLUMN archived_target_group BOOLEAN NOT NULL DEFAULT 0",
        "edit_date": "ALTER TABLE raw_messages ADD COLUMN edit_date DATETIME",
    },
    "media_assets": {
        "ocr_text": "ALTER TABLE media_assets ADD COLUMN ocr_text TEXT",
    },
    "signal_candidates": {
        "source_id": "ALTER TABLE signal_candidates ADD COLUMN source_id INTEGER",
        "event_type": "ALTER TABLE signal_candidates ADD COLUMN event_type VARCHAR(64) NOT NULL DEFAULT 'entry_signal'",
        "review_note": "ALTER TABLE signal_candidates ADD COLUMN review_note TEXT",
    },
    "trade_ideas": {
        "source_id": "ALTER TABLE trade_ideas ADD COLUMN source_id INTEGER",
    },
    "strategy_alerts": {
        "raw_message_id": "ALTER TABLE strategy_alerts ADD COLUMN raw_message_id INTEGER",
        "sender_name": "ALTER TABLE strategy_alerts ADD COLUMN sender_name VARCHAR(255)",
        "original_text": "ALTER TABLE strategy_alerts ADD COLUMN original_text TEXT",
        "is_strategy": "ALTER TABLE strategy_alerts ADD COLUMN is_strategy BOOLEAN",
        "strategy_kind": "ALTER TABLE strategy_alerts ADD COLUMN strategy_kind VARCHAR(32)",
        "ai_confidence": "ALTER TABLE strategy_alerts ADD COLUMN ai_confidence FLOAT",
        "kol_label": "ALTER TABLE strategy_alerts ADD COLUMN kol_label VARCHAR(255)",
        "reason_short": "ALTER TABLE strategy_alerts ADD COLUMN reason_short TEXT",
        "error_message": "ALTER TABLE strategy_alerts ADD COLUMN error_message TEXT",
        "forwarded_at": "ALTER TABLE strategy_alerts ADD COLUMN forwarded_at DATETIME",
        "updated_at": "ALTER TABLE strategy_alerts ADD COLUMN updated_at DATETIME",
    },
    "execution_bindings": {
        "pos_id": "ALTER TABLE execution_bindings ADD COLUMN pos_id VARCHAR(255)",
        "status": "ALTER TABLE execution_bindings ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'open'",
        "updated_at": "ALTER TABLE execution_bindings ADD COLUMN updated_at DATETIME",
    },
    "recovery_decisions": {
        "reason_codes_json": "ALTER TABLE recovery_decisions ADD COLUMN reason_codes_json TEXT NOT NULL DEFAULT '[]'",
        "entry_range_text": "ALTER TABLE recovery_decisions ADD COLUMN entry_range_text VARCHAR(255)",
        "stop_loss_text": "ALTER TABLE recovery_decisions ADD COLUMN stop_loss_text VARCHAR(255)",
        "max_loss_usdt": "ALTER TABLE recovery_decisions ADD COLUMN max_loss_usdt FLOAT NOT NULL DEFAULT 100.0",
        "review_status": "ALTER TABLE recovery_decisions ADD COLUMN review_status VARCHAR(32) NOT NULL DEFAULT 'pending'",
        "reviewed_at": "ALTER TABLE recovery_decisions ADD COLUMN reviewed_at DATETIME",
        "review_note": "ALTER TABLE recovery_decisions ADD COLUMN review_note TEXT",
        "run_at": "ALTER TABLE recovery_decisions ADD COLUMN run_at DATETIME",
        "updated_at": "ALTER TABLE recovery_decisions ADD COLUMN updated_at DATETIME",
    },
    "strategy_lifecycles": {
        "entry_signal_message_id": "ALTER TABLE strategy_lifecycles ADD COLUMN entry_signal_message_id INTEGER",
        "management_signal_message_id": "ALTER TABLE strategy_lifecycles ADD COLUMN management_signal_message_id INTEGER",
        "management_action": "ALTER TABLE strategy_lifecycles ADD COLUMN management_action VARCHAR(64)",
        "management_note": "ALTER TABLE strategy_lifecycles ADD COLUMN management_note TEXT",
    },
    "recognition_experiments": {
        "updated_at": "ALTER TABLE recognition_experiments ADD COLUMN updated_at DATETIME",
    },
}

SQLITE_COMPAT_INDEXES: dict[str, str] = {
    "ix_raw_messages_chat_posted_message": (
        "CREATE INDEX IF NOT EXISTS ix_raw_messages_chat_posted_message "
        "ON raw_messages (chat_id, posted_at, message_id)"
    ),
    "ix_strategy_lifecycles_chat_status_signal": (
        "CREATE INDEX IF NOT EXISTS ix_strategy_lifecycles_chat_status_signal "
        "ON strategy_lifecycles (chat_id, lifecycle_status, signal_at)"
    ),
    "ix_strategy_lifecycles_chat_status_entered": (
        "CREATE INDEX IF NOT EXISTS ix_strategy_lifecycles_chat_status_entered "
        "ON strategy_lifecycles (chat_id, lifecycle_status, entered_at)"
    ),
    "ix_strategy_lifecycles_chat_status_exited": (
        "CREATE INDEX IF NOT EXISTS ix_strategy_lifecycles_chat_status_exited "
        "ON strategy_lifecycles (chat_id, lifecycle_status, exited_at)"
    ),
}


def init_db(engine: Engine) -> None:
    """Create all database tables if they do not already exist."""

    _configure_sqlite(engine)
    Base.metadata.create_all(engine)
    _backfill_sqlite_columns(engine)
    _backfill_sqlite_indexes(engine)


def _configure_sqlite(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL"))
        connection.execute(text("PRAGMA busy_timeout=30000"))


def _backfill_sqlite_columns(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        for table_name, required_columns in SQLITE_COMPAT_COLUMNS.items():
            existing_tables = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).fetchall()
            }
            if table_name not in existing_tables:
                continue

            existing_columns = {
                row[1]
                for row in connection.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
            }
            for column_name, alter_sql in required_columns.items():
                if column_name not in existing_columns:
                    connection.execute(text(alter_sql))


def _backfill_sqlite_indexes(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        existing_tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        for create_index_sql in SQLITE_COMPAT_INDEXES.values():
            table_name = create_index_sql.rsplit(" ON ", 1)[1].split(" ", 1)[0]
            if table_name in existing_tables:
                connection.execute(text(create_index_sql))


def create_session_factory(database_path: str | Path) -> sessionmaker:
    """Create a SQLite session factory and initialize core tables."""

    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30},
        future=True,
    )
    init_db(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

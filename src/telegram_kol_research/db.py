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
}


def init_db(engine: Engine) -> None:
    """Create all database tables if they do not already exist."""

    _configure_sqlite(engine)
    Base.metadata.create_all(engine)
    _backfill_sqlite_columns(engine)


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

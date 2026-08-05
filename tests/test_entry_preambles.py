import sqlite3

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research import models


def test_fresh_database_starts_with_empty_entry_preamble_tables(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    assert hasattr(models, "EntryPreamble")
    assert hasattr(models, "EntryStrategyAssembly")
    with session_factory() as session:
        assert session.query(models.EntryPreamble).count() == 0
        assert session.query(models.EntryStrategyAssembly).count() == 0


def test_entry_preamble_status_constraint_rejects_unknown_state(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO entry_preambles (
                    raw_message_id, chat_id, message_id, symbol, side,
                    risk_multiplier, evidence_version_id,
                    recognition_generation, fingerprint, status, reason,
                    created_at, updated_at
                ) VALUES (1, 2, 3, 'BTC', 'short', '0.5', 4, 'g1', 'fp',
                          'unknown', 'test', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )


def test_entry_preamble_lookup_index_covers_chat_status_and_created_at(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(entry_preambles)"
            ).fetchall()
        }

    assert "ix_entry_preambles_chat_status_created" in indexes

import sqlite3

from telegram_kol_research.db import create_session_factory


def test_management_batch_has_persistent_visibility_retry_fields(tmp_path):
    path = tmp_path / "research.db"
    create_session_factory(path)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(strategy_management_batches)")}
    assert {"visibility_first_failed_at", "visibility_retry_attempts", "visibility_next_attempt_at"} <= columns

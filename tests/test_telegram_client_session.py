from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from telethon.sessions import SQLiteSession
from telethon.tl.types import User
from telethon.tl.types.updates import State

from telegram_kol_research.telegram_client import (
    TelegramAuthConfig,
    create_telegram_client,
)


def _create_client(tmp_path):
    return create_telegram_client(
        TelegramAuthConfig(
            api_id=12345,
            api_hash="test-api-hash",
            session_path=tmp_path / "telegram.session",
        ),
        connect_settings={},
    )


def test_factory_disables_blocking_sqlite_entity_persistence(tmp_path):
    client = _create_client(tmp_path)
    lock_connection = sqlite3.connect(tmp_path / "telegram.session")
    try:
        lock_connection.execute("BEGIN EXCLUSIVE")
        started_at = time.monotonic()
        client.session.process_entities(
            [
                User(
                    id=123,
                    access_hash=456,
                    first_name="Synthetic",
                    username="synthetic_user",
                )
            ]
        )
        elapsed_seconds = time.monotonic() - started_at

        assert elapsed_seconds < 0.5
        assert client.session.save_entities is False
    finally:
        lock_connection.rollback()
        lock_connection.close()
        client.session.close()


def test_factory_preserves_sqlite_update_state_durability(tmp_path):
    session_path = tmp_path / "telegram.session"
    client = _create_client(tmp_path)
    expected = State(
        pts=11,
        qts=22,
        date=datetime(2026, 8, 26, 19, 0, tzinfo=timezone.utc),
        seq=33,
        unread_count=44,
    )
    try:
        client.session.set_update_state(0, expected)
    finally:
        client.session.close()

    reopened = SQLiteSession(str(session_path))
    try:
        actual = reopened.get_update_state(0)
        assert actual is not None
        assert (actual.pts, actual.qts, actual.date, actual.seq) == (
            expected.pts,
            expected.qts,
            expected.date,
            expected.seq,
        )
    finally:
        reopened.close()

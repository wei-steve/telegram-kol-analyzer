from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, SyncCheckpoint
from telegram_kol_research.raw_ingest import repair_history_checkpoints


def test_repair_history_checkpoints_moves_history_cursor_to_latest_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=9001,
                    message_id=77,
                    posted_at=datetime(2026, 4, 10, 8, 30, tzinfo=UTC),
                ),
                RawMessage(
                    chat_id=9001,
                    message_id=78,
                    posted_at=datetime(2026, 4, 10, 8, 45, tzinfo=UTC),
                ),
                SyncCheckpoint(
                    chat_id=9001,
                    sync_kind="history",
                    last_message_id=70,
                    last_message_at=datetime(2026, 4, 10, 8, 0),
                ),
            ]
        )
        session.commit()

    stats = repair_history_checkpoints(session_factory)

    with session_factory() as session:
        checkpoint = (
            session.query(SyncCheckpoint)
            .filter(
                SyncCheckpoint.chat_id == 9001,
                SyncCheckpoint.sync_kind == "history",
            )
            .one()
        )

    assert stats["repaired_checkpoints"] == 1
    assert checkpoint.last_message_id == 78
    assert checkpoint.last_message_at == datetime(2026, 4, 10, 8, 45)

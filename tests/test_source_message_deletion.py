from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    RawMessage,
    SourceMessageDeletionExit,
    TelegramSourceMessageEvent,
)
from telegram_kol_research.source_message_deletion import (
    record_source_message_deleted,
)


def test_record_source_message_deleted_persists_bound_event_and_exit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=-100123,
            message_id=3428,
            text="ETH long SL 1695",
            archived_target_group=True,
        )
        session.add(raw)
        session.commit()
        raw_id = raw.id

    result = record_source_message_deleted(
        session_factory,
        chat_id=-100123,
        message_id=3428,
        deleted_at=datetime(2026, 7, 31, 14, 35, tzinfo=UTC),
        telegram_event={"deleted_ids": [3428]},
    )

    assert result.binding_state == "bound"
    assert result.raw_message_id == raw_id
    assert result.exit_state == "pending"
    with session_factory() as session:
        assert session.query(TelegramSourceMessageEvent).count() == 1
        assert session.query(SourceMessageDeletionExit).count() == 1


def test_record_source_message_deleted_is_idempotent_and_fingerprint_is_stable(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    first = record_source_message_deleted(
        session_factory,
        chat_id=123,
        message_id=7,
        deleted_at=datetime(2026, 8, 2, 1, 0, tzinfo=UTC),
    )
    second = record_source_message_deleted(
        session_factory,
        chat_id=123,
        message_id=7,
        deleted_at=datetime(2026, 8, 2, 2, 0, tzinfo=UTC),
    )

    assert second.event_id == first.event_id
    assert second.event_fingerprint == first.event_fingerprint
    assert second.deleted_at == first.deleted_at
    with session_factory() as session:
        assert session.query(TelegramSourceMessageEvent).count() == 1
        assert session.query(SourceMessageDeletionExit).count() == 1


def test_record_source_message_deleted_marks_raw_without_changing_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    original_text = "ETH long entry 1806-1826 stop 1695"
    with session_factory() as session:
        raw = RawMessage(chat_id=55, message_id=8, text=original_text)
        session.add(raw)
        session.commit()
        raw_id = raw.id

    result = record_source_message_deleted(
        session_factory,
        chat_id=55,
        message_id=8,
        deleted_at=datetime(2026, 8, 2, 3, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        raw = session.get(RawMessage, raw_id)
        assert raw.text == original_text
        assert raw.source_status == "deleted"
        assert raw.deleted_at == datetime(2026, 8, 2, 3, 0)
        assert raw.deletion_event_fingerprint == result.event_fingerprint


def test_record_source_message_deleted_keeps_missing_raw_event_unbound(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    result = record_source_message_deleted(
        session_factory,
        chat_id=900,
        message_id=901,
        deleted_at=datetime(2026, 8, 2, 4, 0, tzinfo=UTC),
    )

    assert result.binding_state == "unbound"
    assert result.raw_message_id is None
    assert result.exit_state == "unbound"
    with session_factory() as session:
        event = session.query(TelegramSourceMessageEvent).one()
        deletion_exit = session.query(SourceMessageDeletionExit).one()
        assert event.raw_message_id is None
        assert deletion_exit.raw_message_id is None
        assert deletion_exit.state == "unbound"

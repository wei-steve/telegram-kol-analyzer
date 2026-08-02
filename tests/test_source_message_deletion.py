from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine, inspect, text

from telegram_kol_research.db import create_session_factory, init_db
from telegram_kol_research.models import (
    RawMessage,
    SignalCandidate,
    SourceMessageDeletionExit,
    TelegramSourceMessageEvent,
)
from telegram_kol_research.source_message_deletion import (
    record_source_message_deleted,
    source_execution_barrier,
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
        raw = RawMessage(
            chat_id=55,
            message_id=8,
            text=original_text,
            archived_target_group=True,
        )
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


def test_source_execution_barrier_blocks_the_exact_deleted_source(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=10,
            message_id=20,
            text="BTC long",
            archived_target_group=True,
        )
        session.add(raw)
        session.commit()
        raw_id = raw.id
    record_source_message_deleted(
        session_factory,
        chat_id=10,
        message_id=20,
    )

    decision = source_execution_barrier(
        session_factory,
        raw_message_id=raw_id,
    )

    assert decision.status == "block"
    assert decision.reason == "source_message_deleted"


def test_source_execution_barrier_holds_overlapping_repost_until_old_exit_is_done(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        old = RawMessage(
            chat_id=10,
            message_id=20,
            text="BTC long old",
            archived_target_group=True,
        )
        repost = RawMessage(
            chat_id=10,
            message_id=21,
            text="BTC long repost",
            archived_target_group=True,
        )
        session.add_all([old, repost])
        session.flush()
        session.add_all(
            [
                SignalCandidate(
                    raw_message_id=old.id,
                    symbol="BTC",
                    side="long",
                    review_status="approved",
                ),
                SignalCandidate(
                    raw_message_id=repost.id,
                    symbol="BTC",
                    side="long",
                    review_status="approved",
                ),
            ]
        )
        session.commit()
        repost_id = repost.id
    record_source_message_deleted(
        session_factory,
        chat_id=10,
        message_id=20,
    )

    waiting = source_execution_barrier(
        session_factory,
        raw_message_id=repost_id,
    )
    with session_factory() as session:
        deletion_exit = session.query(SourceMessageDeletionExit).one()
        deletion_exit.state = "succeeded"
        session.commit()
    released = source_execution_barrier(
        session_factory,
        raw_message_id=repost_id,
    )

    assert waiting.status == "hold"
    assert waiting.reason == "waiting_source_deletion_exit"
    assert released.status == "allow"
    assert released.reason is None


def test_source_execution_barrier_does_not_hold_unrelated_strategy(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        old = RawMessage(
            chat_id=10,
            message_id=20,
            text="BTC long old",
            archived_target_group=True,
        )
        unrelated = RawMessage(
            chat_id=10,
            message_id=21,
            text="ETH long",
            archived_target_group=True,
        )
        session.add_all([old, unrelated])
        session.flush()
        session.add_all(
            [
                SignalCandidate(raw_message_id=old.id, symbol="BTC", side="long"),
                SignalCandidate(raw_message_id=unrelated.id, symbol="ETH", side="long"),
            ]
        )
        session.commit()
        unrelated_id = unrelated.id
    record_source_message_deleted(
        session_factory,
        chat_id=10,
        message_id=20,
    )

    decision = source_execution_barrier(
        session_factory,
        raw_message_id=unrelated_id,
    )

    assert decision.status == "allow"


def test_unbound_deletion_rebinds_when_target_raw_message_arrives(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    original = record_source_message_deleted(
        session_factory,
        chat_id=77,
        message_id=88,
    )
    assert original.binding_state == "unbound"
    with session_factory() as session:
        raw = RawMessage(
            chat_id=77,
            message_id=88,
            text="late evidence",
            archived_target_group=True,
        )
        session.add(raw)
        session.commit()
        raw_id = raw.id

    decision = source_execution_barrier(
        session_factory,
        raw_message_id=raw_id,
    )

    assert decision.status == "block"
    with session_factory() as session:
        raw = session.get(RawMessage, raw_id)
        event = session.query(TelegramSourceMessageEvent).one()
        deletion_exit = session.query(SourceMessageDeletionExit).one()
        assert raw.source_status == "deleted"
        assert event.binding_state == "bound"
        assert event.raw_message_id == raw_id
        assert deletion_exit.raw_message_id == raw_id
        assert deletion_exit.state == "pending"


def test_deletion_does_not_bind_non_target_raw_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=77,
            message_id=88,
            text="outside configured targets",
            archived_target_group=False,
        )
        session.add(raw)
        session.commit()
        raw_id = raw.id

    result = record_source_message_deleted(
        session_factory,
        chat_id=77,
        message_id=88,
    )

    assert result.binding_state == "unbound"
    assert result.raw_message_id is None
    with session_factory() as session:
        assert session.get(RawMessage, raw_id).source_status == "active"


def test_concurrent_duplicate_deletions_return_one_durable_event(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=12,
                message_id=34,
                text="target",
                archived_target_group=True,
            )
        )
        session.commit()

    def record_once(_index):
        return record_source_message_deleted(
            session_factory,
            chat_id=12,
            message_id=34,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(record_once, range(8)))

    assert len({result.event_id for result in results}) == 1
    assert len({result.exit_id for result in results}) == 1
    with session_factory() as session:
        assert session.query(TelegramSourceMessageEvent).count() == 1
        assert session.query(SourceMessageDeletionExit).count() == 1


def test_init_db_backfills_deleted_source_indexes_on_legacy_raw_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE raw_messages ("
                "id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, "
                "message_id INTEGER NOT NULL, posted_at DATETIME, "
                "created_at DATETIME NOT NULL)"
            )
        )

    init_db(engine)

    index_names = {row["name"] for row in inspect(engine).get_indexes("raw_messages")}
    assert "ix_raw_messages_source_status" in index_names
    assert "ix_raw_messages_deletion_event_fingerprint" in index_names


def test_duplicate_deletion_never_reopens_completed_exit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=12,
                message_id=34,
                text="target",
                archived_target_group=True,
            )
        )
        session.commit()
    first = record_source_message_deleted(
        session_factory,
        chat_id=12,
        message_id=34,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, first.exit_id)
        original_fingerprint = deletion_exit.target_fingerprint
        deletion_exit.state = "succeeded"
        deletion_exit.completed_at = datetime(2026, 8, 2, 5, 0, tzinfo=UTC)
        session.commit()

    duplicate = record_source_message_deleted(
        session_factory,
        chat_id=12,
        message_id=34,
    )

    assert duplicate.exit_state == "succeeded"
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, first.exit_id)
        assert deletion_exit.state == "succeeded"
        assert deletion_exit.target_fingerprint == original_fingerprint
        assert deletion_exit.completed_at == datetime(2026, 8, 2, 5, 0)

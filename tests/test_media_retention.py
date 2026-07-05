from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.media_retention import cleanup_media_files
from telegram_kol_research.models import MediaAsset, RawMessage, SignalCandidate


def test_cleanup_media_files_deletes_old_unprotected_files_and_clears_path(tmp_path):
    media_root = tmp_path / "media"
    media_file = media_root / "9001" / "1.jpg"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"old-image")
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    now = datetime(2026, 6, 22, 12, 0, 0)

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=1,
            text="old chart",
            posted_at=now - timedelta(days=30),
            archived_target_group=True,
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="photo",
                local_path="9001/1.jpg",
                ocr_text="BTC long",
            )
        )
        session.commit()

    result = cleanup_media_files(
        session_factory,
        media_root=media_root,
        retain_days=14,
        dry_run=False,
        now=now,
    )

    assert result.deleted_files == 1
    assert result.cleared_local_paths == 1
    assert result.freed_bytes == len(b"old-image")
    assert not media_file.exists()
    with session_factory() as session:
        asset = session.query(MediaAsset).one()
        assert asset.local_path is None
        assert asset.ocr_text == "BTC long"


def test_cleanup_media_files_protects_signal_candidate_images(tmp_path):
    media_root = tmp_path / "media"
    media_file = media_root / "9001" / "2.jpg"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"signal-image")
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    now = datetime(2026, 6, 22, 12, 0, 0)

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=2,
            text="signal chart",
            posted_at=now - timedelta(days=30),
            archived_target_group=True,
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="photo",
                local_path="9001/2.jpg",
            )
        )
        session.add(
            SignalCandidate(
                raw_message_id=raw_message.id,
                symbol="BTC",
                side="long",
                confidence=0.9,
                review_status="confirmed",
            )
        )
        session.commit()

    result = cleanup_media_files(
        session_factory,
        media_root=media_root,
        retain_days=14,
        dry_run=False,
        now=now,
    )

    assert result.deleted_files == 0
    assert result.protected_assets == 1
    assert media_file.exists()
    with session_factory() as session:
        assert session.query(MediaAsset).one().local_path == "9001/2.jpg"


def test_cleanup_media_files_dry_run_leaves_files_and_db_unchanged(tmp_path):
    media_root = tmp_path / "media"
    media_file = media_root / "9001" / "3.jpg"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"dry-run-image")
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    now = datetime(2026, 6, 22, 12, 0, 0)

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=3,
            text="old chart",
            posted_at=now - timedelta(days=30),
            archived_target_group=True,
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="photo",
                local_path="9001/3.jpg",
            )
        )
        session.commit()

    result = cleanup_media_files(
        session_factory,
        media_root=media_root,
        retain_days=14,
        dry_run=True,
        now=now,
    )

    assert result.deleted_files == 1
    assert result.cleared_local_paths == 0
    assert media_file.exists()
    with session_factory() as session:
        assert session.query(MediaAsset).one().local_path == "9001/3.jpg"


def test_cleanup_media_files_deletes_orphan_files_not_referenced_by_database(tmp_path):
    media_root = tmp_path / "media"
    referenced = media_root / "9001" / "kept.jpg"
    orphan = media_root / "9001" / "orphan.jpg"
    referenced.parent.mkdir(parents=True)
    referenced.write_bytes(b"kept-image")
    orphan.write_bytes(b"orphan-image")
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=1,
            text="image",
            posted_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="photo",
                local_path="9001/kept.jpg",
            )
        )
        session.commit()

    result = cleanup_media_files(
        session_factory,
        media_root=media_root,
        retain_days=14,
        dry_run=False,
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert referenced.exists()
    assert not orphan.exists()
    assert result.deleted_files == 1

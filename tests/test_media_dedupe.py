from telegram_kol_research.db import create_session_factory
from telegram_kol_research.media_dedupe import dedupe_media_assets
from telegram_kol_research.models import MediaAsset, RawMessage


def test_dedupe_media_assets_dry_run_reports_duplicates_without_deleting(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=1, message_id=10, text="image")
        session.add(raw_message)
        session.flush()
        session.add_all(
            [
                MediaAsset(raw_message_id=raw_message.id, kind="photo"),
                MediaAsset(raw_message_id=raw_message.id, kind="photo"),
            ]
        )
        session.commit()

    result = dedupe_media_assets(session_factory, dry_run=True)

    with session_factory() as session:
        media_count = session.query(MediaAsset).count()

    assert result.duplicate_message_groups == 1
    assert result.deleted_assets == 1
    assert media_count == 2


def test_dedupe_media_assets_keeps_best_asset_when_applied(tmp_path):
    media_root = tmp_path / "media"
    media_file = media_root / "1" / "10.jpg"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"image")
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=1, message_id=10, text="image")
        session.add(raw_message)
        session.flush()
        session.add_all(
            [
                MediaAsset(raw_message_id=raw_message.id, kind="photo"),
                MediaAsset(
                    raw_message_id=raw_message.id,
                    kind="photo",
                    local_path="1/10.jpg",
                ),
                MediaAsset(
                    raw_message_id=raw_message.id,
                    kind="photo",
                    ocr_text="BTC long",
                ),
            ]
        )
        session.commit()

    result = dedupe_media_assets(session_factory, media_root=media_root, dry_run=False)

    with session_factory() as session:
        media_assets = session.query(MediaAsset).all()

    assert result.deleted_assets == 2
    assert len(media_assets) == 1
    assert media_assets[0].ocr_text == "BTC long"

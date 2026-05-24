from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import MediaAsset, RawMessage, SignalCandidate
from telegram_kol_research.candidates import persist_text_signal_candidates
from telegram_kol_research.raw_ingest import NormalizedMessageRecord
from telegram_kol_research.telegram_client import TelegramAuthConfig


def test_sync_command_parses_caption_without_ocr_into_signal_candidate(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    image_path = tmp_path / "media" / "77.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"fake-image")
    config_path.write_text("groups: []", encoding="utf-8")

    monkeypatch.setattr(
        "telegram_kol_research.cli.load_group_config",
        lambda path: GroupConfig(
            groups=[TargetGroupConfig(chat_title="VIP BTC Room", enabled=True)]
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=tmp_path / "telegram.session",
        ),
    )

    class FakeClient:
        def connect(self):
            return None

        def disconnect(self):
            return None

    monkeypatch.setattr(
        "telegram_kol_research.cli.create_telegram_client",
        lambda auth_config: FakeClient(),
    )

    async def fake_discover_dialogs(client):
        return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]

    async def fake_fetch_dialog_messages(client, dialog, limit):
        return [
            {
                "chat_id": 9001,
                "message_id": 77,
                "sender_id": 501,
                "sender_name": "Alice Trader",
                "text": "BTC long 68000-68200 TP 69000 SL 67500",
                "posted_at": "2026-04-07T00:00:00+00:00",
                "media": {"kind": "photo", "path": str(image_path)},
            }
        ]

    monkeypatch.setattr("telegram_kol_research.cli.discover_dialogs", fake_discover_dialogs)
    monkeypatch.setattr("telegram_kol_research.cli.fetch_dialog_messages", fake_fetch_dialog_messages)
    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
        ],
    )

    assert result.exit_code == 0

    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        media_asset = session.query(MediaAsset).one()

    assert candidate.parse_source == "text"
    assert candidate.symbol == "BTC"
    assert candidate.side == "long"
    assert media_asset.ocr_text is None


def test_sync_command_skips_image_only_candidate_parsing(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    image_path = tmp_path / "media" / "88.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"fake-image")
    config_path.write_text("groups: []", encoding="utf-8")

    monkeypatch.setattr(
        "telegram_kol_research.cli.load_group_config",
        lambda path: GroupConfig(
            groups=[TargetGroupConfig(chat_title="VIP BTC Room", enabled=True)]
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=tmp_path / "telegram.session",
        ),
    )

    class FakeClient:
        def connect(self):
            return None

        def disconnect(self):
            return None

    monkeypatch.setattr(
        "telegram_kol_research.cli.create_telegram_client",
        lambda auth_config: FakeClient(),
    )

    async def fake_discover_dialogs(client):
        return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]

    async def fake_fetch_dialog_messages(client, dialog, limit):
        return [
            {
                "chat_id": 9001,
                "message_id": 88,
                "sender_id": 502,
                "sender_name": "Bob Trader",
                "text": None,
                "posted_at": "2026-04-07T00:00:00+00:00",
                "media": {"kind": "photo", "path": str(image_path)},
            }
        ]

    monkeypatch.setattr("telegram_kol_research.cli.discover_dialogs", fake_discover_dialogs)
    monkeypatch.setattr("telegram_kol_research.cli.fetch_dialog_messages", fake_fetch_dialog_messages)
    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
        ],
    )

    assert result.exit_code == 0

    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        media_asset = session.query(MediaAsset).one()
        candidate_count = session.query(SignalCandidate).count()

    assert candidate_count == 0
    assert media_asset.ocr_text is None


def test_persist_text_signal_candidates_leaves_media_ocr_empty(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=99,
            sender_id=501,
            sender_name="Alice Trader",
            text="BTC long 68000-68200 TP 69000 SL 67500",
            raw_payload="{}",
            archived_target_group=True,
        )
        session.add(raw_message)
        session.flush()
        session.add_all(
            [
                MediaAsset(raw_message_id=raw_message.id, kind="photo", local_path="media/99-a.jpg"),
                MediaAsset(raw_message_id=raw_message.id, kind="photo", local_path="media/99-b.jpg"),
            ]
        )
        session.commit()

    stats = persist_text_signal_candidates(
        session_factory,
        [
            NormalizedMessageRecord(
                chat_id=9001,
                message_id=99,
                sender_id=501,
                sender_name="Alice Trader",
                text="BTC long 68000-68200 TP 69000 SL 67500",
                reply_to_message_id=None,
                media_kind="photo",
                media_path="media/99-b.jpg",
                media_payload={"kind": "photo", "path": "media/99-b.jpg"},
                archived_target_group=True,
                posted_at=None,
                edit_date=None,
                raw_payload="{}",
            )
        ],
    )

    assert stats["inserted_candidates"] == 1
    with session_factory() as session:
        media_assets = session.query(MediaAsset).order_by(MediaAsset.local_path).all()
        candidate = session.query(SignalCandidate).one()

    assert media_assets[0].ocr_text is None
    assert media_assets[1].ocr_text is None
    assert candidate.parse_source == "text"


def test_persist_text_signal_candidates_skips_ocr_and_parses_caption_only(
    monkeypatch, tmp_path
):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=101,
            sender_id=501,
            sender_name="Alice Trader",
            text="BTC long 68000-68200 TP 69000 SL 67500",
            raw_payload="{}",
            archived_target_group=True,
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="photo",
                local_path="media/101.jpg",
            )
        )
        session.commit()

    def fail_if_called(path):
        raise AssertionError("OCR should not be called for media messages")

    monkeypatch.setattr(
        "telegram_kol_research.parsing.ocr_parser.extract_text_from_image",
        fail_if_called,
    )

    stats = persist_text_signal_candidates(
        session_factory,
        [
            NormalizedMessageRecord(
                chat_id=9001,
                message_id=101,
                sender_id=501,
                sender_name="Alice Trader",
                text="BTC long 68000-68200 TP 69000 SL 67500",
                reply_to_message_id=None,
                media_kind="photo",
                media_path="media/101.jpg",
                media_payload={"kind": "photo", "path": "media/101.jpg"},
                archived_target_group=True,
                posted_at=None,
                edit_date=None,
                raw_payload="{}",
            )
        ],
    )

    assert stats["inserted_candidates"] == 1
    with session_factory() as session:
        media_asset = session.query(MediaAsset).one()
        candidate = session.query(SignalCandidate).one()

    assert media_asset.ocr_text is None
    assert candidate.parse_source == "text"


def test_persist_text_signal_candidates_skips_image_only_messages(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=102,
            sender_id=501,
            sender_name="Alice Trader",
            text=None,
            raw_payload="{}",
            archived_target_group=True,
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="photo",
                local_path="media/102.jpg",
            )
        )
        session.commit()

    stats = persist_text_signal_candidates(
        session_factory,
        [
            NormalizedMessageRecord(
                chat_id=9001,
                message_id=102,
                sender_id=501,
                sender_name="Alice Trader",
                text=None,
                reply_to_message_id=None,
                media_kind="photo",
                media_path="media/102.jpg",
                media_payload={"kind": "photo", "path": "media/102.jpg"},
                archived_target_group=True,
                posted_at=None,
                edit_date=None,
                raw_payload="{}",
            )
        ],
    )

    assert stats["inserted_candidates"] == 0
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0

from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import MediaAsset, RawMessage, SignalCandidate
from telegram_kol_research.candidates import persist_text_signal_candidates
from telegram_kol_research.raw_ingest import NormalizedMessageRecord
from telegram_kol_research.telegram_client import TelegramAuthConfig


def test_sync_command_parses_caption_plus_ocr_into_signal_candidate(monkeypatch, tmp_path):
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
                "text": "BTC long setup",
                "posted_at": "2026-04-07T00:00:00+00:00",
                "media": {"kind": "photo", "path": str(image_path)},
            }
        ]

    monkeypatch.setattr("telegram_kol_research.cli.discover_dialogs", fake_discover_dialogs)
    monkeypatch.setattr("telegram_kol_research.cli.fetch_dialog_messages", fake_fetch_dialog_messages)
    monkeypatch.setattr(
        "telegram_kol_research.candidates.extract_text_from_image",
        lambda path: "Entry 68000-68200 TP 69000 SL 67500",
    )

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

    assert candidate.parse_source == "text+ocr"
    assert candidate.symbol == "BTC"
    assert candidate.side == "long"
    assert media_asset.ocr_text == "Entry 68000-68200 TP 69000 SL 67500"


def test_sync_command_parses_image_only_ocr_into_signal_candidate(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        "telegram_kol_research.candidates.extract_text_from_image",
        lambda path: "BTC long 68000-68200 TP 69000 SL 67500",
    )

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

    assert candidate.parse_source == "ocr"
    assert candidate.symbol == "BTC"
    assert candidate.side == "long"
    assert media_asset.ocr_text == "BTC long 68000-68200 TP 69000 SL 67500"


def test_persist_text_signal_candidates_handles_multiple_media_assets_for_one_message(
    monkeypatch, tmp_path
):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=99,
            sender_id=501,
            sender_name="Alice Trader",
            text="BTC long setup",
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

    monkeypatch.setattr(
        "telegram_kol_research.candidates.extract_text_from_image",
        lambda path: "Entry 68000-68200 TP 69000 SL 67500",
    )

    stats = persist_text_signal_candidates(
        session_factory,
        [
            NormalizedMessageRecord(
                chat_id=9001,
                message_id=99,
                sender_id=501,
                sender_name="Alice Trader",
                text="BTC long setup",
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
    assert media_assets[1].ocr_text == "Entry 68000-68200 TP 69000 SL 67500"
    assert candidate.parse_source == "text+ocr"

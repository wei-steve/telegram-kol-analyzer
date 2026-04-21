from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import MediaAsset, RawMessage, SyncCheckpoint
from telegram_kol_research.telegram_client import TelegramAuthConfig


def test_sync_command_persists_raw_messages_and_checkpoint(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
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
        return [
            {"id": 9001, "title": "VIP BTC Room", "archived": True},
        ]

    async def fake_fetch_dialog_messages(client, dialog, limit):
        return [
            {
                "chat_id": 9001,
                "message_id": 77,
                "sender_id": 501,
                "text": "BTC long 68000-68200",
                "reply_to_msg_id": None,
                "posted_at": "2026-04-07T00:00:00+00:00",
                "media": {"kind": "photo", "path": "media/77.jpg"},
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
        raw_messages = session.query(RawMessage).all()
        media_assets = session.query(MediaAsset).all()
        checkpoints = session.query(SyncCheckpoint).all()

    assert len(raw_messages) == 1
    assert raw_messages[0].message_id == 77
    assert raw_messages[0].reply_to_message_id is None
    assert len(media_assets) == 1
    assert media_assets[0].kind == "photo"
    assert len(checkpoints) == 1
    assert checkpoints[0].chat_id == 9001
    assert checkpoints[0].last_message_id == 77


def test_sync_command_only_persists_messages_newer_than_history_checkpoint(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    config_path.write_text("groups: []", encoding="utf-8")

    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            SyncCheckpoint(
                chat_id=9001,
                sync_kind="history",
                last_message_id=77,
            )
        )
        session.commit()

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
                "message_id": 76,
                "sender_id": 501,
                "text": "already seen",
                "posted_at": "2026-04-06T00:00:00+00:00",
                "media": None,
            },
            {
                "chat_id": 9001,
                "message_id": 78,
                "sender_id": 501,
                "text": "fresh message",
                "posted_at": "2026-04-08T00:00:00+00:00",
                "media": None,
            },
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

    with session_factory() as session:
        raw_messages = session.query(RawMessage).order_by(RawMessage.message_id).all()
        checkpoint = session.query(SyncCheckpoint).filter(SyncCheckpoint.chat_id == 9001).one()

    assert [message.message_id for message in raw_messages] == [78]
    assert checkpoint.last_message_id == 78
